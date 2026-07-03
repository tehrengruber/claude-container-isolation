#!/usr/bin/env python3
"""Run Claude Code in an isolated container, tunneling the JetBrains IDE link
through a filtering MCP proxy when one is detected for the current workspace.

The proxy is forked off as a child process so it can run alongside podman;
podman itself is then exec'd in place, which lets it inherit the controlling
terminal directly (subprocess wrappers garble SIGWINCH / fg-pgrp handling).
PR_SET_PDEATHSIG arranges for the proxy to be torn down when podman exits.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Optional

import click

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_IMAGE = os.environ.get("CLAUDE_ISO_IMAGE", "claude-isolation:latest")
HOME = Path(os.environ["HOME"])

# --no-lan: the BPF egress filter (drops traffic to private ranges) is attached to
# a per-run cgroup by a small root-owned helper; DNS is pinned to a public resolver
# so it survives the LAN block. See bpf/lan_block.bpf.c and bpf/lan_block_load.c.
PUBLIC_DNS = ("1.1.1.1", "1.0.0.1")  # Cloudflare
NOLAN_LOADER_PATHS = (
    Path("/usr/lib/claude-isol/lan_block_load"),
    SCRIPT_DIR / "bpf" / "lan_block_load",
)
# Set once we have re-exec'd into a delegated scope, to break the re-exec loop.
NOLAN_SCOPE_ENV = "CLAUDE_ISOL_NOLAN_SCOPE"

# Host-notification wiring (see notifyd.py / notify_client.py). The forwarder and
# socket are mounted at fixed container paths; the daemon's socket lives under
# the host's XDG runtime dir.
NOTIFY_SOCK_CONTAINER = "/run/claude-isol-notify.sock"
NOTIFY_CLIENT_CONTAINER = "/opt/claude-isol/notify_client.py"
NOTIFY_CLIENT_SRC = SCRIPT_DIR / "notify_client.py"

PR_SET_PDEATHSIG = 1
_LIBC = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)


def set_parent_death_signal(sig: int) -> None:
    if _LIBC.prctl(PR_SET_PDEATHSIG, sig, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_PDEATHSIG)")


def find_ide_lock(cwd: Path) -> Optional[Path]:
    ide_dir = HOME / ".claude" / "ide"
    if not ide_dir.is_dir():
        return None
    for lock in ide_dir.glob("*.lock"):
        try:
            data = json.loads(lock.read_text())
        except Exception:
            continue
        for folder in data.get("workspaceFolders", []):
            f = Path(folder)
            if cwd == f or f in cwd.parents:
                return lock
    return None


def notify_hooks_settings() -> dict:
    """Hook config injected via `claude --settings` so the in-container session
    reports its state to the host daemon. Every event runs the same forwarder;
    `async` keeps it off the critical path."""
    cmd = f"python3 {NOTIFY_CLIENT_CONTAINER}"

    def hook(matcher: Optional[str] = None) -> list:
        spec = {"hooks": [{"type": "command", "command": cmd, "async": True}]}
        if matcher is not None:
            spec["matcher"] = matcher
        return [spec]

    return {
        "hooks": {
            "SessionStart": hook(),
            "UserPromptSubmit": hook(),
            "PreToolUse": hook("*"),
            "Notification": hook(),
            "Stop": hook(),
            "SessionEnd": hook(),
        }
    }


def notify_wiring() -> tuple[list[str], list[str], list[str]]:
    """Return (podman_mounts, podman_env, claude_args) for host notifications,
    or empty lists when the notifyd socket isn't present (daemon not running)."""
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    sock = Path(runtime) / "claude-isol" / "notify.sock"
    if not sock.is_socket():
        return [], [], []
    mounts = [
        "-v", f"{sock}:{NOTIFY_SOCK_CONTAINER}",
        "-v", f"{NOTIFY_CLIENT_SRC}:{NOTIFY_CLIENT_CONTAINER}:ro",
    ]
    env = [
        "-e", f"CLAUDE_ISOL_NOTIFY_SOCK={NOTIFY_SOCK_CONTAINER}",
        "-e", f"CLAUDE_ISOL_INSTANCE={uuid.uuid4().hex}",
    ]
    claude_args = ["--settings", json.dumps(notify_hooks_settings())]
    return mounts, env, claude_args


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


def _submounts_under(path: Path) -> list[str]:
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


def confirm_cwd_submounts(cwd: Path, skip: bool) -> None:
    """The working directory is always bind-mounted recursively: rootless runtimes
    can't create a non-recursive bind of a subtree that contains mounts (the kernel
    locks inherited mounts in a user namespace and rejects detaching from them with
    EINVAL), and overmounting a submount to hide it is bypassable by container-root.
    So any filesystem mounted under cwd is genuinely exposed to the sandbox. List
    those and confirm before exec'ing -- a stderr note would vanish the moment
    claude's full-screen UI repaints. --mount-cwd-recursively skips the prompt."""
    if skip:
        return
    nested = _submounts_under(cwd)
    if not nested:
        return
    click.echo(
        "The working directory is bind-mounted recursively, so these filesystems "
        "mounted under it will be exposed (read-write) to the sandbox:", err=True)
    for mp in nested:
        click.echo(f"  - {mp}", err=True)
    click.confirm("Proceed and expose them?", default=False, abort=True, err=True)


def public_resolv() -> Path:
    """A resolv.conf pointing at the public resolver, bound into the sandbox/container
    so DNS still resolves once the LAN (and any LAN resolver) is blocked."""
    fd, path = tempfile.mkstemp(prefix="claude-isol-resolv-")
    with os.fdopen(fd, "w") as fh:
        fh.write("".join(f"nameserver {ns}\n" for ns in PUBLIC_DNS))
    return Path(path)


def _ca_trust_binds() -> list[str]:
    """Read-only binds so the CA trust store resolves inside the --local sandbox.

    /etc and /usr are already ro-bound, which covers the usual layouts (Debian and
    Arch keep certs under /etc + /usr; Fedora under /etc/pki). But some distros store
    the real certs elsewhere and only symlink into /etc -- notably openSUSE, whose
    /etc/ssl/certs/*.pem and /etc/ssl/ca-bundle.pem point into /var/lib/ca-certificates.
    A recursive /etc bind then carries those symlinks in but not their targets, so they
    dangle and TLS fails with "unable to get local issuer certificate". Resolve the
    well-known trust anchors -- and the symlinks inside the cert dir -- and bind any
    real location that lives outside the trees already bound above."""
    bound = ("/usr", "/etc", "/opt")
    targets: set[str] = set()

    def consider(path: str) -> None:
        if not os.path.lexists(path):
            return
        real = os.path.realpath(path)
        if not os.path.exists(real):  # broken symlink on the host: nothing to bind
            return
        d = real if os.path.isdir(real) else os.path.dirname(real)
        if not any(d == b or d.startswith(b + "/") for b in bound):
            targets.add(d)

    # Bundle files and the cert dir itself (any of these may be a symlink outward).
    for p in ("/etc/ssl/certs", "/etc/ssl/cert.pem", "/etc/ssl/ca-bundle.pem",
              "/etc/pki/tls/certs/ca-bundle.crt"):
        consider(p)
    # Entries inside the cert dir are commonly per-cert symlinks into an external
    # tree (openSUSE's /var/lib/ca-certificates/pem); follow each and collect it.
    certs_dir = Path("/etc/ssl/certs")
    if certs_dir.is_dir():
        for entry in certs_dir.iterdir():
            if entry.is_symlink():
                consider(str(entry))

    # Drop any target nested under another; binding the ancestor already covers it.
    minimal = [d for d in targets
               if not any(d != o and d.startswith(o + "/") for o in targets)]
    binds: list[str] = []
    for d in sorted(minimal):
        binds += ["--ro-bind", d, d]
    return binds


def build_bwrap_cmd(cwd: Path, volumes: list[str], inner: list[str],
                    resolv: Optional[Path] = None) -> list[str]:
    """Build the `bwrap` argv for --local mode: a host sandbox that exposes only the
    minimal system dirs read-only, a fresh ephemeral HOME with just the Claude config
    bound in, and the cwd as the single read-write tree. `inner` is the command run
    inside the sandbox (e.g. `["claude", ...]` or `["bash"]`).

    The system-dir layout is probed at runtime rather than hardcoded so the same logic
    works on Arch (where /bin /lib /sbin are symlinks into /usr) and on distros where
    they are real directories (some openSUSE layouts).
    """
    cmd = ["bwrap"]

    # Read-only system dirs that always live at a fixed path when present.
    for d in ("/usr", "/etc", "/opt", "/usr/local"):
        if Path(d).is_dir():
            cmd += ["--ro-bind", d, d]

    # The usr-merge compatibility entries: recreate symlinks as-is, bind real dirs.
    for d in ("/bin", "/sbin", "/lib", "/lib64"):
        p = Path(d)
        if p.is_symlink():
            cmd += ["--symlink", os.readlink(d), d]
        elif p.is_dir():
            cmd += ["--ro-bind", d, d]

    # Make sure the claude binary itself is reachable if it lives outside the
    # dirs bound above (e.g. an npm-global install under /usr/local or elsewhere).
    claude_bin = shutil.which("claude")
    if claude_bin:
        real = os.path.realpath(claude_bin)
        if not any(real.startswith(p + "/") for p in ("/usr", "/opt", "/bin", "/sbin")):
            d = os.path.dirname(real)
            cmd += ["--ro-bind", d, d]

    # CA trust store: bind any trust anchors that live outside the system trees
    # already bound above (see _ca_trust_binds).
    cmd += _ca_trust_binds()

    cmd += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]

    # Fresh ephemeral HOME (writable tmpfs so ~/.cache etc. work), with only the
    # Claude config and the scoped gh/git credentials bound in.
    home = str(HOME)
    cmd += [
        "--tmpfs", home,
        "--bind", f"{HOME}/.claude", f"{home}/.claude",
        "--bind", f"{HOME}/.claude.json", f"{home}/.claude.json",
        "--bind", f"{HOME}/.config/gh-claude", f"{home}/.config/gh",
        "--bind", f"{HOME}/.gitconfig-claude", f"{home}/.gitconfig",
    ]

    # The one read-write tree. Placed after the tmpfs home so a cwd under HOME
    # still wins by bwrap's last-wins ordering.
    cmd += ["--bind", str(cwd), str(cwd)]

    # User -v specs, translated from podman's src:dst[:opts] to bwrap binds.
    # Placed last so the user can override anything bound above.
    for spec in volumes:
        parts = spec.split(":")
        if len(parts) == 1:
            src = dst = parts[0]
            opts = ""
        else:
            src, dst = parts[0], parts[1]
            opts = parts[2] if len(parts) > 2 else ""
        flag = "--ro-bind" if "ro" in opts.split(",") else "--bind"
        cmd += [flag, src, dst]

    # Make DNS resolution work inside the sandbox.
    #
    # /etc/resolv.conf is usually a symlink (systemd-resolved, NetworkManager, ...)
    # into /run. The /etc ro-bind above carries the symlink in, but its target lives
    # under /run, which the sandbox never mounts -- so the symlink dangles and every
    # lookup fails. We therefore bind a real file at the symlink's *resolved* target;
    # the symlink (carried in by the /etc ro-bind) then resolves to it. For a plain
    # /etc/resolv.conf the ro-bind already exposes the real contents, so we only bind
    # when we have an override to apply.
    #
    # The bound file is our public-resolver override under --no-lan (whose resolver
    # must survive the LAN block), otherwise the host's own resolved resolv.conf.
    # Placed after the /etc ro-bind so it wins by bwrap's last-wins ordering.
    etc_resolv = "/etc/resolv.conf"
    if os.path.islink(etc_resolv):
        target = os.path.realpath(etc_resolv)
        src = resolv if resolv is not None else Path(target)
        cmd += ["--ro-bind", str(src), target]
    elif resolv is not None:
        cmd += ["--ro-bind", str(resolv), etc_resolv]

    cmd += [
        "--chdir", str(cwd),
        "--unshare-all", "--share-net", "--die-with-parent",
        *inner,
    ]
    return cmd


def ensure_image(image: str) -> None:
    if subprocess.run(["podman", "image", "exists", image]).returncode == 0:
        return
    print(f"image {image} not found; building from {SCRIPT_DIR}", file=sys.stderr)
    subprocess.run(["podman", "build", "-t", image, str(SCRIPT_DIR)], check=True)


def image_id(image: str) -> str:
    """Return the local image's content digest (sha256, no prefix). Pulls
    the image first if it isn't present locally."""
    fmt = ["podman", "image", "inspect", "--format", "{{.Id}}", image]
    r = subprocess.run(fmt, capture_output=True, text=True)
    if r.returncode != 0:
        subprocess.run(["podman", "pull", image], check=True)
        r = subprocess.run(fmt, capture_output=True, text=True, check=True)
    return r.stdout.strip().removeprefix("sha256:")


def install_claude_on_top(base_image: str) -> str:
    """Build an image that layers the claude-code apt package on top of
    `base_image` (see Dockerfile.install-claude) and return its tag. The tag is
    derived from the base image's content digest so a re-pulled base bypasses
    the stale cache; otherwise podman's layer cache makes the build a no-op."""
    tag = f"claude-isol-augmented:{image_id(base_image)[:12]}"
    if subprocess.run(["podman", "image", "exists", tag]).returncode == 0:
        return tag
    dockerfile = SCRIPT_DIR / "Dockerfile.install-claude"
    print(f"building {tag} on top of {base_image}", file=sys.stderr)
    subprocess.run(
        ["podman", "build",
         "-t", tag,
         "-f", str(dockerfile),
         "--build-arg", f"BASE_IMAGE={base_image}",
         str(SCRIPT_DIR)],
        check=True,
    )
    return tag


def spawn_proxy(ide_port: str) -> str:
    """Fork the MCP proxy and return the port it bound to.

    The child sets PR_SET_PDEATHSIG so that once this process exec's into
    podman, the proxy receives SIGTERM the moment podman exits.
    """
    port_r, port_w = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(port_r)
            os.dup2(port_w, 1)
            os.close(port_w)
            set_parent_death_signal(signal.SIGTERM)
            os.execv(sys.executable, [
                sys.executable,
                str(SCRIPT_DIR / "mcp_proxy.py"),
                "--upstream", f"ws://127.0.0.1:{ide_port}",
            ])
        except BaseException as e:
            os.write(2, f"mcp-proxy exec failed: {e!r}\n".encode())
        os._exit(127)

    os.close(port_w)
    with os.fdopen(port_r, "rb") as fr:
        line = fr.readline()
    port = line.strip().decode()
    if not port:
        os.waitpid(pid, 0)
        raise RuntimeError("mcp-proxy failed to report a port")
    return port


def _validate_volume(ctx, param, value):
    """Reject malformed -v specs up front: SRC:DST[:OPTS] (or a single PATH), with
    non-empty SRC/DST and at most one options field."""
    for spec in value:
        parts = spec.split(":")
        if len(parts) > 3 or any(p == "" for p in parts[: min(len(parts), 2)]):
            raise click.BadParameter(
                f"{spec!r}: expected SRC:DST[:OPTS] (or a single PATH)", param=param)
    return value


# Strict parsing: an unrecognized option (e.g. a typo'd --shel) is rejected rather
# than silently forwarded. To pass options through to claude, separate them with
# `--`, e.g. `claude-isol --local -- -p "hi" --model opus`. A bare prompt needs no
# separator (`claude-isol "fix the bug"`).
@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--shell", "drop_shell", is_flag=True,
              help="Drop into a shell instead of running claude (e.g. `gh auth login`).")
@click.option("--image", metavar="NAME",
              help="Run the prebuilt image NAME as-is, instead of the bundled default.")
@click.option("--install-claude", is_flag=True,
              help="Layer the claude-code package on top of --image.")
@click.option("--no-userns", is_flag=True,
              help="Omit --userns entirely (use podman's default; HOME becomes /root).")
@click.option("--local", is_flag=True,
              help="Run on the host under a bubblewrap sandbox (no container).")
@click.option("--tmpfs-home", is_flag=True,
              help="Mount HOME as a fresh tmpfs (already the default under --local).")
@click.option("--mount-cwd-recursively", is_flag=True,
              help="Skip the confirmation prompted when filesystems are mounted under "
                   "the working directory. The cwd is always bound recursively (rootless "
                   "runtimes can't bind a subtree non-recursively), so such submounts "
                   "are exposed to the sandbox; this acknowledges that up front.")
@click.option("--no-lan", is_flag=True,
              help="Block all traffic to local networks (the internet stays "
                   "reachable); DNS is pinned to a public resolver. Works in both modes.")
@click.option("-v", "--volume", "volumes", multiple=True, metavar="SRC:DST[:OPTS]",
              callback=_validate_volume,
              help="Extra mount, repeatable; works in both modes.")
@click.argument("claude_args", nargs=-1, type=click.UNPROCESSED)
def main(drop_shell, image, install_claude, no_userns, local, tmpfs_home,
         mount_cwd_recursively, no_lan, volumes, claude_args):
    """Run Claude Code in isolation.

    Unknown options are rejected; pass options through to claude after a `--`
    separator, e.g. `claude-isol --local -- -p "hi"`.
    """
    args = list(claude_args)
    volumes = list(volumes)

    if install_claude and not image:
        raise click.UsageError("--install-claude requires --image")

    if local and (image or install_claude or no_userns):
        raise click.UsageError(
            "--local cannot be combined with --image/--install-claude/--no-userns")
    if local and tmpfs_home:
        click.echo("note: --tmpfs-home is redundant under --local "
                   "(HOME is always a tmpfs there)", err=True)

    # --no-lan creates a per-run cgroup; if our terminal's cgroup isn't delegated
    # (PyCharm/SSH/TTY), re-exec into a delegated scope first. No-op otherwise.
    if no_lan:
        reexec_in_delegated_scope()

    if local:
        if shutil.which("bwrap") is None:
            click.echo("--local requires bubblewrap (bwrap); install the "
                       "'bubblewrap' package", err=True)
            raise SystemExit(2)
        cwd = Path.cwd()
        confirm_cwd_submounts(cwd, mount_cwd_recursively)
        # Scoped gh/git credentials, created on first run (shared with container mode).
        (HOME / ".config" / "gh-claude").mkdir(parents=True, exist_ok=True)
        (HOME / ".gitconfig-claude").touch(exist_ok=True)
        inner = [os.environ.get("SHELL", "bash")] if drop_shell else ["claude", *args]
        resolv = None
        if no_lan:
            run_cg = setup_nolan_cgroup()
            resolv = public_resolv()
            # Move ourselves into the filtered cgroup; the exec'd bwrap (and its
            # whole tree) inherit the membership and thus the egress filter.
            (run_cg / "cgroup.procs").write_text(str(os.getpid()))
        os.execvp("bwrap", build_bwrap_cmd(cwd, volumes, inner, resolv))
        return  # unreachable

    if image is None:
        # No image specified: use the bundled default, building it on demand.
        image = DEFAULT_IMAGE
        ensure_image(image)
    elif install_claude:
        image = install_claude_on_top(image)

    cwd = Path.cwd()
    confirm_cwd_submounts(cwd, mount_cwd_recursively)
    ide_lock = None if drop_shell else find_ide_lock(cwd)

    # Without --userns=keep-id, container uid 0 maps to the host user, so HOME
    # inside the container is /root rather than the host home path.
    container_home = Path("/root") if no_userns else HOME

    network = "pasta"
    extra_mounts: list[str] = []

    if ide_lock is not None:
        proxy_port = spawn_proxy(ide_lock.stem)
        tmp_ide = Path(tempfile.mkdtemp(prefix="claude-ide-"))
        shutil.copy(ide_lock, tmp_ide / f"{proxy_port}.lock")
        network = f"pasta:-T,{proxy_port}"
        extra_mounts = ["-v", f"{tmp_ide}:{container_home}/.claude/ide"]

    gh_config = HOME / ".config" / "gh-claude"
    gh_config.mkdir(parents=True, exist_ok=True)

    gitconfig = HOME / ".gitconfig-claude"
    gitconfig.touch(exist_ok=True)

    notify_mounts: list[str] = []
    notify_env: list[str] = []
    notify_args: list[str] = []
    if not drop_shell:
        notify_mounts, notify_env, notify_args = notify_wiring()

    tty_flag = ["-t"] if sys.stdin.isatty() and sys.stdout.isatty() else []
    userns_flag = [] if no_userns else ["--userns=keep-id"]
    # A tmpfs HOME wipes whatever the image ships under the home dir; the config
    # bind mounts below are layered on top (podman orders mounts parent-first).
    tmpfs_flag = ["--tmpfs", str(container_home)] if tmpfs_home else []

    # --no-lan: run the whole podman process tree under a cgroup that carries the
    # egress filter, and pin DNS to the public resolver.
    #
    # Two things matter here. (1) We must use the cgroupfs manager: with the
    # default systemd manager, rootless podman asks the user systemd manager for a
    # fresh podman-<pid>.scope and moves itself there, escaping our cgroup. (2) We
    # do NOT pass our cgroup's full root-relative path as --cgroup-parent -- rootless
    # podman+cgroupfs interprets --cgroup-parent relative to its *current* cgroup, so
    # the full path gets appended to wherever podman already is, creating a doubled,
    # unfiltered cgroup elsewhere. Instead we move ourselves into the filtered cgroup
    # just before exec (see below); podman then stays put (cgroupfs manager) and
    # creates conmon, pasta and the container as descendants -- all inheriting the
    # filter. pasta is the process that re-originates the container's packets, so it
    # too must live under the filtered cgroup, which this guarantees.
    nolan_flags: list[str] = []
    nolan_cg: Optional[Path] = None
    if no_lan:
        nolan_cg = setup_nolan_cgroup()
        nolan_flags = [
            "--cgroup-manager=cgroupfs",
            "--cgroup-parent=ctr",
            *[a for ns in PUBLIC_DNS for a in ("--dns", ns)],
        ]

    cmd = [
        "podman", "run", "--rm", "-i", *tty_flag,
        f"--network={network}",
        *nolan_flags,
        *userns_flag,
        *tmpfs_flag,
        "--pid=host",
        "-v", f"{HOME}/.claude:{container_home}/.claude",
        *extra_mounts,
        "-v", f"{HOME}/.claude.json:{container_home}/.claude.json",
        "-v", f"{gh_config}:{container_home}/.config/gh",
        "-v", f"{gitconfig}:{container_home}/.gitconfig",
        "-v", f"{cwd}:{cwd}",
        *[arg for spec in volumes for arg in ("-v", spec)],
        *notify_mounts,
        "-w", str(cwd),
        "-e", f"HOME={container_home}",
        "-e", "TERM",
        *notify_env,
        image,
        *(["bash"] if drop_shell else ["claude", *notify_args, *args]),
    ]

    # Move ourselves into the filtered cgroup last, just before exec: with the
    # cgroupfs manager podman stays in this cgroup and spawns conmon/pasta/the
    # container beneath it, so the egress filter is inherited by the lot.
    if nolan_cg is not None:
        (nolan_cg / "cgroup.procs").write_text(str(os.getpid()))

    os.execvp("podman", cmd)
    return  # unreachable


if __name__ == "__main__":
    main()