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


def main() -> int:
    args = sys.argv[1:]
    drop_shell = False
    image: Optional[str] = None
    install_claude = False
    no_userns = False
    volume_args: list[str] = []
    # Leading flags are consumed here; the rest is forwarded to claude.
    #   --shell           drop into bash instead of running claude (e.g. `gh auth login`)
    #   --image NAME      run the prebuilt image NAME as-is, instead of the bundled default
    #   --install-claude  layer the claude-code apt package on top of --image
    #   --no-userns       omit --userns entirely (use podman's own default)
    #   -v/--volume V     extra mount, passed straight to `podman run -v` (repeatable)
    while args:
        if args[0] == "--shell":
            drop_shell = True
            args = args[1:]
        elif args[0] == "--image":
            if len(args) < 2:
                print("--image requires an argument", file=sys.stderr)
                return 2
            image, args = args[1], args[2:]
        elif args[0] == "--install-claude":
            install_claude, args = True, args[1:]
        elif args[0] == "--no-userns":
            no_userns, args = True, args[1:]
        elif args[0] in ("-v", "--volume"):
            if len(args) < 2:
                print(f"{args[0]} requires an argument", file=sys.stderr)
                return 2
            volume_args += ["-v", args[1]]
            args = args[2:]
        else:
            break

    if install_claude and image is None:
        print("--install-claude requires --image", file=sys.stderr)
        return 2

    if image is None:
        # No image specified: use the bundled default, building it on demand.
        image = DEFAULT_IMAGE
        ensure_image(image)
    elif install_claude:
        image = install_claude_on_top(image)

    cwd = Path.cwd()
    ide_lock = None if drop_shell else find_ide_lock(cwd)

    network = "pasta"
    extra_mounts: list[str] = []

    if ide_lock is not None:
        proxy_port = spawn_proxy(ide_lock.stem)
        tmp_ide = Path(tempfile.mkdtemp(prefix="claude-ide-"))
        shutil.copy(ide_lock, tmp_ide / f"{proxy_port}.lock")
        network = f"pasta:-T,{proxy_port}"
        extra_mounts = ["-v", f"{tmp_ide}:{HOME}/.claude/ide"]

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

    cmd = [
        "podman", "run", "--rm", "-i", *tty_flag,
        f"--network={network}",
        *userns_flag,
        "--pid=host",
        "-v", f"{HOME}/.claude:{HOME}/.claude",
        *extra_mounts,
        "-v", f"{HOME}/.claude.json:{HOME}/.claude.json",
        "-v", f"{gh_config}:{HOME}/.config/gh",
        "-v", f"{gitconfig}:{HOME}/.gitconfig",
        "-v", f"{cwd}:{cwd}",
        *volume_args,
        *notify_mounts,
        "-w", str(cwd),
        "-e", f"HOME={HOME}",
        "-e", "TERM",
        *notify_env,
        image,
        *(["bash"] if drop_shell else ["claude", *notify_args, *args]),
    ]

    os.execvp("podman", cmd)
    return 1  # unreachable


if __name__ == "__main__":
    sys.exit(main())