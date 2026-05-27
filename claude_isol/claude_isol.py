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
from pathlib import Path
from typing import Optional

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_IMAGE = os.environ.get("CLAUDE_ISO_IMAGE", "claude-isolation:latest")
HOME = Path(os.environ["HOME"])

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


def ensure_image(image: str) -> None:
    if subprocess.run(["podman", "image", "exists", image]).returncode == 0:
        return
    print(f"image {image} not found; building from {SCRIPT_DIR}", file=sys.stderr)
    subprocess.run(["podman", "build", "-t", image, str(SCRIPT_DIR)], check=True)


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
    # Leading flags are consumed here; the rest is forwarded to claude.
    #   --shell      drop into bash instead of running claude (e.g. `gh auth login`)
    #   --image NAME run the prebuilt image NAME as-is, instead of the bundled default
    while args:
        if args[0] == "--shell":
            drop_shell = True
            args = args[1:]
        elif args[0] == "--image":
            if len(args) < 2:
                print("--image requires an argument", file=sys.stderr)
                return 2
            image, args = args[1], args[2:]
        else:
            break

    if image is None:
        # No image specified: use the bundled default, building it on demand.
        image = DEFAULT_IMAGE
        ensure_image(image)

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

    tty_flag = ["-t"] if sys.stdin.isatty() and sys.stdout.isatty() else []

    cmd = [
        "podman", "run", "--rm", "-i", *tty_flag,
        f"--network={network}",
        "--userns=keep-id",
        "--pid=host",
        "-v", f"{HOME}/.claude:{HOME}/.claude",
        *extra_mounts,
        "-v", f"{HOME}/.claude.json:{HOME}/.claude.json",
        "-v", f"{gh_config}:{HOME}/.config/gh",
        "-v", f"{gitconfig}:{HOME}/.gitconfig",
        "-v", f"{cwd}:{cwd}",
        "-w", str(cwd),
        "-e", f"HOME={HOME}",
        "-e", "TERM",
        image,
        *(["bash"] if drop_shell else ["claude", *args]),
    ]

    os.execvp("podman", cmd)
    return 1  # unreachable


if __name__ == "__main__":
    sys.exit(main())