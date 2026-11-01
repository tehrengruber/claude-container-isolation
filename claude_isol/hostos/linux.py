#!/usr/bin/env python3
"""Linux: every mode, and the kernel features the rest of the tool leans on.

Besides the interface described in hostos/__init__.py, this holds what only
Linux has: PR_SET_PDEATHSIG for helpers that must die with the launcher, the
XDG runtime dir for per-session state, and the cgroup + eBPF egress filter
behind --no-lan.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import click

NAME = "linux"
LABEL = "Linux"
BACKENDS = {"container": "container", "local": "bwrap", "vm": "vm"}
UNSUPPORTED: dict[str, str] = {}
# Rootless podman runs the container on this kernel, under this user.
CONTAINER_SHARES_HOST = True

# --no-lan: the BPF egress filter (drops traffic to private ranges) is attached to
# a per-run cgroup by a small root-owned helper; DNS is pinned to a public resolver
# so it survives the LAN block. See bpf/lan_block.bpf.c and bpf/lan_block_load.c.
NOLAN_LOADER_PATHS = (
    Path("/usr/lib/claude-isol/lan_block_load"),
    Path(__file__).resolve().parent.parent / "bpf" / "lan_block_load",
)
# Set once we have re-exec'd into a delegated scope, to break the re-exec loop.
NOLAN_SCOPE_ENV = "CLAUDE_ISOL_NOLAN_SCOPE"

PR_SET_PDEATHSIG = 1
_LIBC = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)


def runtime_dir() -> Path:
    """`$XDG_RUNTIME_DIR/claude-isol`, checked rather than assumed.

    Private keys, credential copies and the --host-exec socket all live here, on
    the assumption that it is a directory we own that nobody else can enter. That
    holds for a logind session; it does not hold if XDG_RUNTIME_DIR is unset and
    shared, or points into /tmp, where another user could have pre-created the
    path. mkdir(exist_ok=True) would not notice -- it leaves an existing
    directory's mode alone and follows a symlink -- so check explicitly.
    """
    base = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
    d = base / "claude-isol"
    d.mkdir(parents=True, exist_ok=True)
    for p in (base, d):
        st = p.lstat()
        if stat.S_ISLNK(st.st_mode):
            raise click.UsageError(f"{p} is a symlink; refusing to put session "
                                   "state behind it")
        if st.st_uid != os.getuid():
            raise click.UsageError(f"{p} is not owned by you (uid {st.st_uid}); "
                                   "refusing to put session state there")
        if st.st_mode & 0o077:
            if p == d:
                p.chmod(0o700)   # ours to tighten
            else:
                raise click.UsageError(
                    f"{p} is accessible to other users (mode "
                    f"{stat.S_IMODE(st.st_mode):04o}); refusing to put session "
                    "state there")
    return d


def set_parent_death_signal(sig: int) -> None:
    if _LIBC.prctl(PR_SET_PDEATHSIG, sig, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_PDEATHSIG)")


def submounts_under(path: Path) -> list[str]:
    """Mountpoints nested strictly under `path` in the caller's mount table -- the
    foreign filesystems (LUKS volumes, nested binds) a recursive bind of `path` would
    drag into the sandbox."""
    root = str(path)
    out = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        fields = line.split(" ")
        if len(fields) > 4:
            mp = fields[4]  # mount point, may carry octal-escaped chars
            if mp != root and mp.startswith(root + "/"):
                out.append(mp)
    return out


def _current_cgroup_path() -> Path:
    """The launcher's own cgroup v2 directory under /sys/fs/cgroup."""
    for line in Path("/proc/self/cgroup").read_text().splitlines():
        if line.startswith("0::"):
            return Path("/sys/fs/cgroup") / line[3:].lstrip("/")
    raise SystemExit("--no-lan needs cgroup v2 (unified hierarchy)")


def _cgroup_is_delegated() -> bool:
    """True if we may create child cgroups in our current cgroup, i.e. the cgroup
    directory is delegated to us (writable). Terminals launched via the user systemd
    manager (e.g. GNOME Terminal's vte-spawn scope) are delegated; bare login session
    scopes (PyCharm's terminal, SSH, plain TTY) are root-owned and are not."""
    try:
        return os.access(_current_cgroup_path(), os.W_OK | os.X_OK)
    except OSError:
        return False


def reexec_in_delegated_scope() -> None:
    """--no-lan needs to create a per-run cgroup, which requires a delegated cgroup.
    When we were started in a non-delegated one (session-N.scope), re-exec the whole
    invocation under a transient, delegated user scope so our own systemd --user
    manager places us somewhere we can manage. No-op when already delegated, and
    guarded by an env var so the re-exec'd copy never loops."""
    if os.environ.get(NOLAN_SCOPE_ENV) or _cgroup_is_delegated():
        return
    systemd_run = shutil.which("systemd-run")
    if systemd_run is None:
        raise SystemExit(
            "--no-lan: this terminal's cgroup is not delegated and systemd-run is "
            "missing, so the egress filter's cgroup can't be created. Launch from a "
            "terminal started by your desktop's systemd user manager, or install "
            "systemd-run.")
    env = {**os.environ, NOLAN_SCOPE_ENV: "1"}
    # --scope keeps the command in the foreground on this TTY; Delegate=yes hands us
    # ownership of the scope's cgroup subtree so setup_nolan_cgroup() can mkdir in it.
    os.execvpe(systemd_run, [
        systemd_run, "--user", "--scope", "--quiet", "--property=Delegate=yes",
        "--", sys.executable, os.path.abspath(sys.argv[0]), *sys.argv[1:],
    ], env)


def _nolan_loader() -> Path:
    for p in NOLAN_LOADER_PATHS:
        if p.exists():
            return p
    raise SystemExit(
        "--no-lan: lan_block_load not found; build it with "
        "`make -C claude_isol/bpf` or install it under /usr/lib/claude-isol")


def setup_nolan_cgroup() -> Path:
    """Create a per-run cgroup and attach the LAN-block BPF filter to it via the
    capability-bearing loader. Processes placed in this cgroup -- or any
    descendant -- can reach the internet but not the local networks."""
    base = _current_cgroup_path()
    # We exec away after this, so we can't clean up our own cgroup on exit; sweep
    # empty leftovers from prior runs instead (rmdir only succeeds when unused).
    for stale in base.glob("claude-isol-nolan-*"):
        try:
            stale.rmdir()
        except OSError:
            pass
    run_cg = base / f"claude-isol-nolan-{os.getpid()}"
    run_cg.mkdir(exist_ok=True)
    # The loader carries file caps (cap_bpf,cap_net_admin) so no sudo is needed.
    # Its own stderr (success line, or a precise attach error) passes through.
    proc = subprocess.run([str(_nolan_loader()), str(run_cg)])
    if proc.returncode == 127:
        # Dynamic linker aborted before main(): the loader is built against
        # libbpf.so but the runtime library isn't installed.
        raise SystemExit(
            "--no-lan: the egress-filter loader could not start -- libbpf is not "
            "installed. Install it (Arch: pacman -S libbpf), or drop --no-lan.")
    if proc.returncode != 0:
        raise SystemExit(
            f"--no-lan: the egress-filter loader failed (exit {proc.returncode}); "
            "see its error above.")
    return run_cg
