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
import grp
import json
import os
import pwd
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Optional

import click

from . import backends
from .launch import Launch

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

# --host-exec wiring (see host_execd.py / host_mcp.py). The MCP server runs in the
# container and forwards to the host daemon over a socket bind-mounted in; the
# daemon prompts on the host before running anything.
HOSTEXEC_SOCK_CONTAINER = "/run/claude-isol-hostexec.sock"
HOSTEXEC_MCP_CONTAINER = "/opt/claude-isol/host_mcp.py"
HOSTEXEC_MCP_SRC = SCRIPT_DIR / "host_mcp.py"
HOSTEXEC_DAEMON_SRC = SCRIPT_DIR / "host_execd.py"
# Ceiling for one tool call: the dialog's own wait plus the longest command the
# daemon will run (DIALOG_TIMEOUT + MAX_TIMEOUT in host_execd.py), with room to spare.
HOSTEXEC_CALL_TIMEOUT_MS = 4_200_000

# What --local forwards into the sandbox. Everything else is dropped (see the
# --clearenv in build_bwrap_cmd): the point of the scoped gh/git credentials is
# that the session never sees the host's real ones, and an exported GH_TOKEN or
# ANTHROPIC_API_KEY in the launching shell would hand them over anyway. Use -e to
# pass something deliberately.
ENV_ALLOWLIST = (
    "HOME", "USER", "LOGNAME", "SHELL", "TERM", "COLORTERM", "LANG", "LC_ALL",
    "LC_CTYPE", "TZ", "PATH", "XDG_RUNTIME_DIR", "XDG_SESSION_TYPE",
    "DISPLAY", "WAYLAND_DISPLAY", "SSH_TTY", "NO_COLOR", "PAGER", "EDITOR",
)

# ~/.claude is shared read-write because a session has to write its transcripts,
# todos and token refreshes there. These entries are the exception: they are
# executed by the *host's* claude on its next run, so leaving them writable hands
# any session a way out of the sandbox -- write a hook into settings.json or an
# mcpServers entry into .claude.json and it runs as the user, unsandboxed, with
# no prompt. They are mounted read-only on top of the share.
CLAUDE_CONFIG_RO = (
    "settings.json", "settings.local.json",
    "agents", "commands", "plugins", "hooks", "skills", "output-styles",
)

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


def find_ide_lock(cwd: Path) -> Optional[Path]:
    ide_dir = HOME / ".claude" / "ide"
    if not ide_dir.is_dir():
        return None
    for lock in ide_dir.glob("*.lock"):
        # The name is the port, and this directory is writable by the session,
        # which then hands it to spawn_proxy() to interpolate into the proxy's
        # ws://127.0.0.1:<port> upstream. A planted `8080.lock` would aim the
        # next launch at a host service; `80@evil.example` would redirect the URL
        # outright. Only a plain unprivileged port is accepted.
        if not (lock.stem.isdigit() and 1024 <= int(lock.stem) <= 65535):
            continue
        try:
            data = json.loads(lock.read_text())
        except Exception:
            continue
        for folder in data.get("workspaceFolders", []):
            f = Path(folder)
            if cwd == f or f in cwd.parents:
                return lock
    return None


def _prompt_index(args: list[str]) -> Optional[int]:
    """Index of the prompt positional, when it can be picked out without knowing
    claude's whole option table: the only non-option argument, or the token right
    after `-p`/`--print`, which is a boolean flag. None when it is ambiguous."""
    positionals = [i for i, a in enumerate(args) if not a.startswith("-")]
    if len(positionals) == 1:
        return positionals[0]
    return next((i for i in positionals if args[i - 1] in ("-p", "--print")), None)


def prompt_from_file(path: Path, args: list[str],
                     dst: str) -> tuple[list[str], Optional[str], Optional[str]]:
    """Wire up an opening prompt read from `path`, keeping it off every command
    line -- the launcher's, claude's, and the shell history of whoever starts it.

    Print mode takes it on stdin, where it arrives as the prompt exactly as
    written. An interactive session has no such channel (the TUI owns stdin), so
    the file is bound in at `dst` and referenced as `@dst`; claude expands that
    inline when it builds the message, without the Read tool and without a
    permission prompt. Returns (args, stdin text, mount spec).
    """
    text = path.read_text()
    if not text.strip():
        raise click.UsageError(f"--prompt-file: {path} is empty")
    if any(a in ("-p", "--print") for a in args) and sys.stdin.isatty():
        return args, text, None
    return [*args, f"@{dst}"], None, f"{path}:{dst}:ro"


def prompt_off_argv(args: list[str]) -> tuple[list[str], Optional[str]]:
    """Pull the prompt out of a print-mode arg list so it can go in on stdin.

    A command line is public: /proc/<pid>/cmdline is world-readable, so a prompt
    passed as an argument is visible to every user on the machine in `ps ax` for
    as long as the session runs -- in the launcher's own argv, and again in
    claude's, which is a plain host process even in container mode. Print mode
    reads the prompt from stdin when it isn't given one, which keeps it out of
    both. Returns (args without the prompt, the prompt).

    Only unambiguous cases are claimed. Telling a positional from an option's
    value in general would mean knowing claude's whole option table, so the
    prompt is taken when it is the only non-option argument, or when it directly
    follows `-p`/`--print` -- a boolean flag, so the next token is a positional.
    Anything else is left where it is rather than risked.
    """
    if not sys.stdin.isatty():
        return args, None  # stdin already carries something; not ours to take
    if not any(a in ("-p", "--print") for a in args):
        return args, None  # interactive: claude takes the opening prompt in argv only

    idx = _prompt_index(args)
    if idx is None:
        return args, None
    return args[:idx] + args[idx + 1:], args[idx]


def resolve_prompt(spec, dst: str) -> tuple[list[str], Optional[str]]:
    """The prompt plumbing every backend shares: returns (claude args, text to
    feed on stdin). `dst` is where a --prompt-file will be readable from inside
    that backend's sandbox, which is the only part that differs between modes; a
    mount spec for it is appended to spec.volumes."""
    if spec.prompt_file is not None:
        args, prompt, mount = prompt_from_file(spec.prompt_file, spec.args, dst)
        if mount is not None:
            spec.volumes.append(mount)
        return args, prompt
    return prompt_off_argv(spec.args)


def stdin_from_text(text: str) -> None:
    """Point stdin at an unlinked temp file holding `text`, just before exec, so
    the child reads it as piped input and it never lands in a command line."""
    tmp = tempfile.TemporaryFile()
    tmp.write(text.encode())
    tmp.flush()
    os.lseek(tmp.fileno(), 0, os.SEEK_SET)
    os.dup2(tmp.fileno(), 0)


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
            "PreCompact": hook(),
            "Notification": hook(),
            "Stop": hook(),
            "SessionEnd": hook(),
        }
    }


def notify_wiring() -> tuple[list[str], list[str], list[str]]:
    """Return (podman_mounts, podman_env, claude_args) for host notifications,
    or empty lists when the notifyd socket isn't present (daemon not running)."""
    sock = runtime_dir() / "notify.sock"
    if not sock.is_socket():
        return [], [], []
    mounts = [
        "-v", f"{sock}:{NOTIFY_SOCK_CONTAINER}",
        "-v", f"{NOTIFY_CLIENT_SRC}:{NOTIFY_CLIENT_CONTAINER}:ro",
    ]
    env = [
        "-e", f"CLAUDE_ISOL_NOTIFY_SOCK={NOTIFY_SOCK_CONTAINER}",
        "-e", f"CLAUDE_ISOL_INSTANCE={uuid.uuid4().hex}",
        # We exec podman in-place further down, so our pid *is* the pid of the
        # process that owns the session for its whole lifetime; the daemon watches
        # it to drop the line when the session dies without a SessionEnd hook.
        "-e", f"CLAUDE_ISOL_HOST_PID={os.getpid()}",
        # The launch directory, as a stable label. The hook payload's own cwd
        # wanders (subagents, tool calls) and would mislabel the line.
        "-e", f"CLAUDE_ISOL_LABEL={Path.cwd().name or 'session'}",
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


def _nss_file_with_row(src: str, key: int, row: str) -> Optional[Path]:
    """A temp copy of the colon-separated file `src` with `row` appended -- unless a
    line already carries numeric id `key` in its third field, in which case the id is
    already resolvable from the file the /etc bind carries in and we return None."""
    try:
        existing = Path(src).read_text()
    except OSError:
        existing = ""
    for line in existing.splitlines():
        fields = line.split(":")
        if len(fields) > 2 and fields[2] == str(key):
            return None
    fd, path = tempfile.mkstemp(prefix="claude-isol-nss-")
    with os.fdopen(fd, "w") as fh:
        fh.write(existing)
        if existing and not existing.endswith("\n"):
            fh.write("\n")
        fh.write(row)
    return Path(path)


def _passwd_group_binds() -> list[str]:
    """Make the launching user resolvable inside the --local sandbox.

    Directory accounts (LDAP/AD via SSSD/nss) have no row in /etc/passwd or /etc/group
    -- they're served over an nss socket the sandbox doesn't expose -- so getpwuid()
    inside fails ("cannot find name for user ID ...") and breaks whoami, git, shell
    prompt expansion, etc. Copy the host's files and append the current user's/group's
    row when it's missing, then bind the copies over /etc/{passwd,group}. Placed after
    the /etc ro-bind so they win by bwrap's last-wins ordering."""
    binds: list[str] = []
    try:
        pw = pwd.getpwuid(os.getuid())
    except KeyError:
        pw = None
    if pw is not None:
        row = ":".join((pw.pw_name, "x", str(pw.pw_uid), str(pw.pw_gid),
                        pw.pw_gecos, pw.pw_dir, pw.pw_shell)) + "\n"
        f = _nss_file_with_row("/etc/passwd", pw.pw_uid, row)
        if f is not None:
            binds += ["--ro-bind", str(f), "/etc/passwd"]
    try:
        gr = grp.getgrgid(os.getgid())
    except KeyError:
        gr = None
    if gr is not None:
        row = ":".join((gr.gr_name, "x", str(gr.gr_gid), ",".join(gr.gr_mem))) + "\n"
        f = _nss_file_with_row("/etc/group", gr.gr_gid, row)
        if f is not None:
            binds += ["--ro-bind", str(f), "/etc/group"]
    return binds


def _claude_json_copy() -> Path:
    """A per-run copy of ~/.claude.json for the sandbox to scribble on. See
    CLAUDE_CONFIG_RO for why the original is not handed over."""
    dst = Path(tempfile.mkdtemp(prefix="claude-isol-cfg-")) / "claude.json"
    shutil.copy(HOME / ".claude.json", dst)
    return dst


def build_bwrap_cmd(cwd: Path, volumes: list[str], inner: list[str],
                    resolv: Optional[Path] = None,
                    dev_binds: Optional[list[str]] = None,
                    home_subdirs: tuple[str, ...] = (),
                    writable_home: bool = False,
                    env_vars: tuple[str, ...] = ()) -> list[str]:
    """Build the `bwrap` argv for --local mode: a host sandbox that exposes only the
    minimal system dirs read-only, a fresh ephemeral HOME with just the Claude config
    bound in, and the cwd as the single read-write tree. `inner` is the command run
    inside the sandbox (e.g. `["claude", ...]` or `["bash"]`).

    The system-dir layout is probed at runtime rather than hardcoded so the same logic
    works on Arch (where /bin /lib /sbin are symlinks into /usr) and on distros where
    they are real directories (some openSUSE layouts).
    """
    cmd = ["bwrap", "--clearenv"]

    # --clearenv, then an allowlist. Without it bwrap forwards our whole environ,
    # which quietly defeats the scoped gh/git credentials: GH_TOKEN, GITHUB_TOKEN,
    # ANTHROPIC_API_KEY, AWS_*, GIT_ASKPASS and friends would walk straight into
    # the sandbox. Anything else the caller actually wants is what -e is for.
    for var in ENV_ALLOWLIST:
        val = os.environ.get(var)
        if val is not None:
            cmd += ["--setenv", var, val]
    for spec in env_vars or ():
        name, _, val = spec.partition("=")
        val = val if "=" in spec else os.environ.get(name)
        if val is not None:
            cmd += ["--setenv", name, val]

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

    # CA trust store: bind any trust anchors that live outside the system trees
    # already bound above (see _ca_trust_binds).
    cmd += _ca_trust_binds()

    # Make the launching user (a directory account may be absent from /etc/passwd)
    # resolvable inside the sandbox (see _passwd_group_binds).
    cmd += _passwd_group_binds()

    cmd += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]

    # Fresh ephemeral HOME (writable tmpfs so ~/.cache etc. work), with only the
    # Claude config and the scoped gh/git credentials bound in.
    home = str(HOME)
    cmd += [
        "--tmpfs", home,
        "--bind", f"{HOME}/.claude", f"{home}/.claude",
        # A throwaway copy, not the host file: ~/.claude.json carries mcpServers,
        # which the host's claude would launch on its next run. The session can
        # write this freely; nothing it writes reaches the host.
        "--bind", str(_claude_json_copy()), f"{home}/.claude.json",
        "--bind", f"{HOME}/.config/gh-claude", f"{home}/.config/gh",
        "--bind", f"{HOME}/.gitconfig-claude", f"{home}/.gitconfig",
    ]

    # Writable scratch inside the otherwise read-only HOME (see the --remount-ro at
    # the end). A bwrap tmpfs comes up owned by the sandbox user, so unlike podman's
    # it needs no mode of its own.
    for rel in home_subdirs:
        cmd += ["--tmpfs", f"{home}/{rel}"]

    # Make sure the claude binary itself is reachable when it lives outside the
    # system dirs bound above -- an npm-global install under /usr/local, or a
    # native one under HOME (~/.local/...). Two directories, because the one PATH
    # found it in and the one it really lives in differ when the PATH entry is a
    # symlink into a versioned install dir; bind only the target and the symlink
    # itself is gone with the tmpfs, so PATH no longer resolves.
    #
    # After the tmpfs home, and for the same reason as the cwd below: an install
    # under the home dir bound before it is simply shadowed, and the sandbox comes
    # up with no claude in it at all ("bwrap: execvp claude: No such file or
    # directory").
    claude_bin = shutil.which("claude")
    if claude_bin:
        for d in dict.fromkeys((os.path.dirname(claude_bin),
                                os.path.dirname(os.path.realpath(claude_bin)))):
            if not any(d == p or d.startswith(p + "/")
                       for p in ("/usr", "/opt", "/bin", "/sbin")):
                cmd += ["--ro-bind", d, d]

    # Read-only on top of the writable ~/.claude bind: the entries the host's own
    # claude would execute later (see CLAUDE_CONFIG_RO). Last-wins ordering, so
    # these have to come after the bind above.
    for name in CLAUDE_CONFIG_RO:
        src = HOME / ".claude" / name
        if src.exists():
            cmd += ["--ro-bind", str(src), f"{home}/.claude/{name}"]

    # The one read-write tree. Placed after the tmpfs home so a cwd under HOME
    # still wins by bwrap's last-wins ordering.
    cmd += ["--bind", str(cwd), str(cwd)]

    # User -v specs, translated from podman's src:dst[:opts] to bwrap binds.
    # Placed last so the user can override anything bound above.
    for spec in volumes:
        src, dst, opts = parse_volume(spec)
        flag = "--ro-bind" if "ro" in opts.split(",") else "--bind"
        cmd += [flag, src, dst]

    # User --dev-bind specs: like -v but mounted with device access, so device nodes
    # (GPUs at /dev/dri, /dev/bus/usb, ...) actually work -- a plain bind blocks that.
    # bwrap-only. Placed after -v so a later --dev-bind can override an earlier -v.
    for spec in dev_binds or []:
        parts = spec.split(":")
        src, dst = (parts[0], parts[0]) if len(parts) == 1 else (parts[0], parts[1])
        cmd += ["--dev-bind", src, dst]

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

    # HOME read-only, to match container mode, where HOME is the image's root-owned
    # directory and only the config paths bound over it are writable. Everything
    # mounted *into* HOME above -- the config binds, the cwd, --tmpfs-home-subdirectory,
    # a claude install under the home dir -- is a mount of its own and stays writable:
    # --remount-ro is not recursive. It has to come after all of them, since mounting
    # anything needs a writable mountpoint, and it makes the mount itself read-only
    # rather than just the mode bits, so nothing in the sandbox can chmod its way out.
    if not writable_home:
        cmd += ["--remount-ro", home]

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


def spawn_hostexecd(cwd: Path, label: str) -> Path:
    """Fork the host-side executor and return the socket it listens on.

    Same shape as spawn_proxy: the child sets PR_SET_PDEATHSIG so the daemon is
    torn down the moment the exec'd podman exits, and we wait for its ready line
    -- podman needs the socket to exist before it can bind-mount it.
    """
    sock = runtime_dir() / f"hostexec-{uuid.uuid4().hex}.sock"

    ready_r, ready_w = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(ready_r)
            os.dup2(ready_w, 1)
            os.close(ready_w)
            set_parent_death_signal(signal.SIGTERM)
            os.execv(sys.executable, [
                sys.executable, str(HOSTEXEC_DAEMON_SRC),
                "--socket", str(sock),
                "--cwd", str(cwd),
                "--label", label,
            ])
        except BaseException as e:
            os.write(2, f"host-execd exec failed: {e!r}\n".encode())
        os._exit(127)

    os.close(ready_w)
    with os.fdopen(ready_r, "rb") as fr:
        line = fr.readline()
    if line.strip() != b"ready":
        os.waitpid(pid, 0)
        raise RuntimeError("host-execd failed to start")
    return sock


def hostexec_wiring(cwd: Path) -> tuple[list[str], list[str], list[str]]:
    """Return (podman_mounts, podman_env, claude_args) for --host-exec."""
    sock = spawn_hostexecd(cwd, cwd.name or "session")
    mounts = [
        "-v", f"{sock}:{HOSTEXEC_SOCK_CONTAINER}",
        "-v", f"{HOSTEXEC_MCP_SRC}:{HOSTEXEC_MCP_CONTAINER}:ro",
    ]
    env = ["-e", f"CLAUDE_ISOL_HOSTEXEC_SOCK={HOSTEXEC_SOCK_CONTAINER}"]
    # A tool call blocks while the host dialog is up, so it needs a per-call
    # timeout longer than the dialog can wait plus the command's own limit. The
    # default is ample, but a low MCP_TOOL_TIMEOUT in the environment would apply
    # here too, and a call cut off mid-prompt would still be approved and run on
    # the host with nothing left to hand the result back to. A per-server timeout
    # takes precedence over that variable, so pin it.
    mcp_config = {"mcpServers": {"host": {
        "command": "python3", "args": [HOSTEXEC_MCP_CONTAINER],
        "timeout": HOSTEXEC_CALL_TIMEOUT_MS,
    }}}
    return mounts, env, ["--mcp-config", json.dumps(mcp_config)]


def parse_volume(spec: str) -> tuple[str, str, str]:
    """A -v spec as (src, dst, opts), in podman's SRC:DST[:OPTS] spelling. A bare
    path means the same location on both sides."""
    parts = spec.split(":")
    if len(parts) == 1:
        return parts[0], parts[0], ""
    return parts[0], parts[1], parts[2] if len(parts) > 2 else ""


def _validate_volume(ctx, param, value):
    """Reject malformed -v specs up front: SRC:DST[:OPTS] (or a single PATH), with
    non-empty SRC/DST and at most one options field."""
    for spec in value:
        parts = spec.split(":")
        if len(parts) > 3 or any(p == "" for p in parts[: min(len(parts), 2)]):
            raise click.BadParameter(
                f"{spec!r}: expected SRC:DST[:OPTS] (or a single PATH)", param=param)
    return value


def _validate_podman_args(ctx, param, value):
    """One occurrence, one podman argument, taken exactly as the shell delivered
    it -- so quoting is handled once, by the shell, and not again here.

    Batching several arguments into one occurrence would need a separator, and
    both candidates are wrong for podman. Commas are data in its values
    (`--mount type=bind,source=/a,target=/b`, and the `:rw,exec,mode=1777` this
    wrapper passes itself); whitespace would quietly cut a path containing a
    space in half."""
    for spec in value:
        if not spec.strip():
            raise click.BadParameter("empty argument", param=param)
    return tuple(value)


def _validate_home_subdir(ctx, param, value):
    """Normalize --tmpfs-home-subdirectory specs to paths relative to HOME.

    Takes comma-separated lists as well as repetition, and either a relative path
    (`.cache`) or an absolute one under HOME (`~/.cache`, already expanded by the
    shell). A path outside HOME is rejected -- that is what -v is for -- and so is
    HOME itself, which is --tmpfs-home."""
    out: list[str] = []
    for spec in value:
        for raw in (s.strip() for s in spec.split(",")):
            if not raw:
                continue
            p = os.path.normpath(os.path.join(str(HOME), os.path.expanduser(raw)))
            if p == str(HOME) or not p.startswith(str(HOME) + os.sep):
                raise click.BadParameter(
                    f"{raw!r}: expected a path inside HOME, e.g. .cache", param=param)
            rel = os.path.relpath(p, str(HOME))
            if rel not in out:
                out.append(rel)
    return tuple(out)


def _validate_dev_bind(ctx, param, value):
    """Reject malformed --dev-bind specs: SRC:DST (or a single PATH), both non-empty.
    Unlike -v there is no options field (a device bind is always read-write)."""
    for spec in value:
        parts = spec.split(":")
        if len(parts) > 2 or any(p == "" for p in parts):
            raise click.BadParameter(
                f"{spec!r}: expected SRC:DST (or a single PATH)", param=param)
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
@click.option("--vm", is_flag=True,
              help="Run in a QEMU virtual machine, where claude is root in its own "
                   "kernel and can do anything (nested containers included) while "
                   "the host stays behind the VM boundary. Needs /dev/kvm; the "
                   "working directory is shared in over virtiofs.")
@click.option("--vm-memory", metavar="SIZE",
              help="RAM for --vm (QEMU syntax, e.g. 8G). Default 8G.")
@click.option("--vm-cpus", metavar="N", type=int,
              help="vCPUs for --vm. Default: half the host's, at least 2.")
@click.option("--tmpfs-home", is_flag=True,
              help="Mount HOME as a fresh tmpfs. Under --local HOME is always a fresh "
                   "tmpfs; there this flag leaves it writable instead of read-only.")
@click.option("--tmpfs-home-subdirectory", "home_subdirs", multiple=True,
              metavar="PATH[,PATH...]", callback=_validate_home_subdir,
              help="Mount a writable tmpfs at PATH inside HOME, leaving the rest of "
                   "HOME alone. Repeatable and comma-separated; paths are relative "
                   "to HOME (e.g. .cache,.npm). Contents are discarded when the "
                   "session ends. Works in both modes.")
@click.option("--mount-cwd-recursively", is_flag=True,
              help="Skip the confirmation prompted when filesystems are mounted under "
                   "the working directory. The cwd is always bound recursively (rootless "
                   "runtimes can't bind a subtree non-recursively), so such submounts "
                   "are exposed to the sandbox; this acknowledges that up front.")
@click.option("--no-lan", is_flag=True,
              help="Block all traffic to local networks (the internet stays "
                   "reachable); DNS is pinned to a public resolver. Works in both modes.")
@click.option("--exec", "exec_cmd", is_flag=True,
              help="Run the trailing arguments as the command inside the sandbox "
                   "instead of claude (e.g. `claude-isol --exec -- htop`).")
@click.option("-e", "--env", "env_vars", multiple=True, metavar="VAR[=VAL]",
              help="Pass an environment variable into the container (repeatable; "
                   "a bare VAR forwards the host value). No-op under --local, "
                   "which forwards the full host environment already.")
@click.option("--tcp-forward", "tcp_forwards", multiple=True, type=int, metavar="PORT",
              help="Forward localhost:PORT inside the container to host "
                   "localhost:PORT (repeatable). No-op under --local, which "
                   "shares the host network already.")
@click.option("--host-exec", "host_exec", is_flag=True,
              help="Give claude a tool that runs commands on the host, outside the "
                   "sandbox. Every call raises a confirmation dialog on the host "
                   "showing the command; nothing runs unless you approve it there. "
                   "Container mode only.")
@click.option("--prompt-file", "prompt_file", metavar="PATH",
              type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Take the opening prompt from PATH, so it never appears in a "
                   "command line or in shell history. Fed in on stdin in print "
                   "mode; bound in and passed as an @-reference otherwise.")
@click.option("--podman-arg", "podman_args", multiple=True, metavar="ARG",
              callback=_validate_podman_args,
              help="Pass ARG straight through to `podman run`, for the podman flags "
                   "this wrapper does not wrap (--memory, --cap-add, --security-opt, "
                   "...). One argument per occurrence, repeatable: "
                   "--podman-arg=--memory=4g --podman-arg=--privileged. Use the "
                   "=form, or the value is taken for an option. Container mode only.")
@click.option("-v", "--volume", "volumes", multiple=True, metavar="SRC:DST[:OPTS]",
              callback=_validate_volume,
              help="Extra mount, repeatable; works in both modes.")
@click.option("--dev-bind", "dev_binds", multiple=True, metavar="SRC:DST",
              callback=_validate_dev_bind,
              help="Bind SRC onto DST with device access (GPUs at /dev/dri, "
                   "/dev/bus/usb, ...), which a plain -v bind blocks; repeatable. "
                   "--local only.")
@click.argument("claude_args", nargs=-1, type=click.UNPROCESSED)
def main(drop_shell, image, install_claude, no_userns, local, vm, vm_memory,
         vm_cpus, tmpfs_home, mount_cwd_recursively, no_lan, exec_cmd, env_vars,
         tcp_forwards, host_exec, prompt_file, home_subdirs, podman_args, volumes,
         dev_binds, claude_args):
    """Run Claude Code in isolation.

    Unknown options are rejected; pass options through to claude after a `--`
    separator, e.g. `claude-isol --local -- -p "hi"`.
    """
    args = list(claude_args)
    volumes = list(volumes)

    if install_claude and not image:
        raise click.UsageError("--install-claude requires --image")

    if exec_cmd and drop_shell:
        raise click.UsageError("--exec cannot be combined with --shell")
    if exec_cmd and not args:
        raise click.UsageError("--exec requires a command after `--`")

    if prompt_file is not None:
        if drop_shell or exec_cmd:
            raise click.UsageError(
                "--prompt-file cannot be combined with --shell/--exec")
        if _prompt_index(args) is not None:
            raise click.UsageError(
                "--prompt-file conflicts with the prompt given as an argument")

    if host_exec and (drop_shell or exec_cmd):
        # The tool is exposed to a claude session; there is none to expose it to.
        raise click.UsageError("--host-exec cannot be combined with --shell/--exec")

    if local and vm:
        raise click.UsageError("--local and --vm are different modes; pick one")

    spec = Launch(
        mode="local" if local else "vm" if vm else "container",
        cwd=Path.cwd(),
        args=args,
        drop_shell=drop_shell,
        exec_cmd=exec_cmd,
        prompt_file=prompt_file,
        volumes=volumes,
        env_vars=env_vars,
        home_subdirs=home_subdirs,
        no_lan=no_lan,
        mount_cwd_recursively=mount_cwd_recursively,
        image=image,
        install_claude=install_claude,
        no_userns=no_userns,
        tmpfs_home=tmpfs_home,
        tcp_forwards=tcp_forwards,
        host_exec=host_exec,
        podman_args=podman_args,
        dev_binds=dev_binds,
        vm_memory=vm_memory,
        vm_cpus=vm_cpus,
    )
    spec.check_mode_flags()

    # --no-lan creates a per-run cgroup; if our terminal's cgroup isn't delegated
    # (PyCharm/SSH/TTY), re-exec into a delegated scope first. No-op otherwise.
    if no_lan:
        reexec_in_delegated_scope()

    backends.launch(spec)
    return  # unreachable: the backend exec's

