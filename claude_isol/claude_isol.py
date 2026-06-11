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


def build_bwrap_cmd(cwd: Path, volumes: list[str], inner: list[str]) -> list[str]:
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
@click.option("-v", "--volume", "volumes", multiple=True, metavar="SRC:DST[:OPTS]",
              callback=_validate_volume,
              help="Extra mount, repeatable; works in both modes.")
@click.argument("claude_args", nargs=-1, type=click.UNPROCESSED)
def main(drop_shell, image, install_claude, no_userns, local, tmpfs_home,
         volumes, claude_args):
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

    if local:
        if shutil.which("bwrap") is None:
            click.echo("--local requires bubblewrap (bwrap); install the "
                       "'bubblewrap' package", err=True)
            raise SystemExit(2)
        cwd = Path.cwd()
        # Scoped gh/git credentials, created on first run (shared with container mode).
        (HOME / ".config" / "gh-claude").mkdir(parents=True, exist_ok=True)
        (HOME / ".gitconfig-claude").touch(exist_ok=True)
        inner = [os.environ.get("SHELL", "bash")] if drop_shell else ["claude", *args]
        os.execvp("bwrap", build_bwrap_cmd(cwd, volumes, inner))
        return  # unreachable

    if image is None:
        # No image specified: use the bundled default, building it on demand.
        image = DEFAULT_IMAGE
        ensure_image(image)
    elif install_claude:
        image = install_claude_on_top(image)

    cwd = Path.cwd()
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

    cmd = [
        "podman", "run", "--rm", "-i", *tty_flag,
        f"--network={network}",
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

    os.execvp("podman", cmd)
    return  # unreachable


if __name__ == "__main__":
    main()