#!/usr/bin/env python3
"""Host-side executor for `claude-isol --host-exec`.

Runs on the host, outside the sandbox, listening on a Unix socket that is
bind-mounted into the container. One JSON request per connection

    {"command": "<shell command>", "timeout": <seconds>}

is shown to the user in a confirmation dialog carrying the verbatim command,
and only runs -- as `bash -lc`, in the directory the session was launched in --
once it is approved there. The reply is one JSON line:

    {"exit": <code>, "stdout": "...", "stderr": "..."}   or   {"error": "..."}

The dialog is the real gate. The socket lives inside the container, so anything
in there reaches this daemon: the MCP tool (see `host_mcp.py`), but equally a
plain shell command that opens the socket itself. Neither can skip the prompt,
because the prompt is on this side of the boundary. Requests are handled one at
a time so two sessions can never race two dialogs onto the screen.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import selectors
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

MAX_OUTPUT = 64 * 1024  # per stream, as much as is useful to hand back to Claude
DEFAULT_TIMEOUT = 600.0
MAX_TIMEOUT = 3600.0
DIALOG_TIMEOUT = 300.0  # an unanswered prompt is a denied prompt
REQUEST_TIMEOUT = 10.0  # reading the request itself; the dialog is untimed by this

TITLE = "claude-isol: run on the host?"
NOTIFY_DEST = "org.freedesktop.Notifications"
NOTIFY_PATH = "/org/freedesktop/Notifications"


def _clip(raw: bytes) -> str:
    text = raw.decode("utf-8", "replace")
    if len(text) > MAX_OUTPUT:
        text = text[:MAX_OUTPUT] + f"\n[... truncated at {MAX_OUTPUT} bytes]"
    return text


def _prompt_text(command: str, cwd: Path, label: str) -> str:
    who = f"Claude ({label})" if label else "Claude"
    return (f"{who} wants to run this on the host, outside the sandbox:\n\n"
            f"{command}\n\nWorking directory: {cwd}")


def _run_dialog(argv: list[str]) -> bool:
    try:
        return subprocess.run(argv, timeout=DIALOG_TIMEOUT).returncode == 0
    except subprocess.TimeoutExpired:
        return False
    except OSError as e:
        print(f"host-execd: {argv[0]} failed: {e}", file=sys.stderr)
        return False


def _ask_zenity(text: str) -> Optional[bool]:
    exe = shutil.which("zenity")
    if exe is None:
        return None
    # zenity renders --text as Pango markup, so the command has to be escaped.
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return _run_dialog([exe, "--question", "--width=600", "--title", TITLE,
                        "--ok-label=Run on host", "--cancel-label=Deny",
                        "--text", escaped])


def _ask_kdialog(text: str) -> Optional[bool]:
    exe = shutil.which("kdialog")
    if exe is None:
        return None
    return _run_dialog([exe, "--title", TITLE, "--warningyesno", text,
                        "--yes-label", "Run on host", "--no-label", "Deny"])


def _gdbus(method: str, *args: str) -> Optional[str]:
    cmd = ["gdbus", "call", "--session", "--dest", NOTIFY_DEST,
           "--object-path", NOTIFY_PATH, "--method", method, *args]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.stdout.decode() if p.returncode == 0 else None


def _ask_notification(text: str) -> Optional[bool]:
    """Last resort: a notification with Run/Deny buttons, the same D-Bus service
    `notifyd.py` already renders the status board through. Only usable if the
    notification server implements actions -- many do not, and a prompt with no
    buttons is no prompt at all, so we hand back None and let the caller say so."""
    if shutil.which("gdbus") is None:
        return None
    caps = _gdbus("org.freedesktop.Notifications.GetCapabilities")
    if caps is None or "'actions'" not in caps:
        return None

    # Watch before we notify: the answer can arrive before Notify() even returns.
    try:
        mon = subprocess.Popen(
            ["gdbus", "monitor", "--session", "--dest", NOTIFY_DEST],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        return None

    nid = None
    try:
        # urgency critical + expire_timeout 0: the prompt must not time itself out.
        out = _gdbus("org.freedesktop.Notifications.Notify",
                     "claude-isol", "0", "dialog-question", TITLE, text,
                     "['run', 'Run on host', 'deny', 'Deny']",
                     "{'urgency': <byte 2>}", "0")
        m = re.search(r"uint32\s+(\d+)", out or "")  # gdbus prints "(uint32 N,)"
        if m is None:
            return None
        nid = m.group(1)

        action = re.compile(rf"ActionInvoked \(uint32 {nid}, '([^']*)'\)")
        closed = re.compile(rf"NotificationClosed \(uint32 {nid},")
        sel = selectors.DefaultSelector()
        sel.register(mon.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + DIALOG_TIMEOUT
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not sel.select(remaining):
                return False
            line = mon.stdout.readline().decode("utf-8", "replace")
            if not line:
                return False
            m = action.search(line)
            if m is not None:
                return m.group(1) == "run"
            if closed.search(line):  # dismissed without choosing
                return False
    finally:
        mon.kill()
        mon.wait()
        if nid is not None:
            _gdbus("org.freedesktop.Notifications.CloseNotification", nid)


def approved(command: str, cwd: Path, label: str) -> bool:
    text = _prompt_text(command, cwd, label)
    for backend in (_ask_zenity, _ask_kdialog, _ask_notification):
        answer = backend(text)
        if answer is not None:
            return answer
    print("host-execd: no way to ask -- install zenity or kdialog; denying",
          file=sys.stderr)
    return False


def run_command(command: str, cwd: Path, timeout: float) -> dict:
    try:
        p = subprocess.run(["bash", "-lc", command], cwd=str(cwd),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=timeout)
    except subprocess.TimeoutExpired as e:
        return {"exit": None,
                "error": f"timed out after {timeout:g}s",
                "stdout": _clip(e.stdout or b""), "stderr": _clip(e.stderr or b"")}
    except OSError as e:
        return {"exit": None, "error": f"could not run the command: {e}"}
    return {"exit": p.returncode, "stdout": _clip(p.stdout), "stderr": _clip(p.stderr)}


def handle(conn: socket.socket, cwd: Path, label: str) -> None:
    conn.settimeout(REQUEST_TIMEOUT)
    buf = bytearray()
    while b"\n" not in buf:
        chunk = conn.recv(65536)
        if not chunk:
            break
        buf += chunk
        if len(buf) > MAX_OUTPUT:
            break
    conn.settimeout(None)  # the dialog and the command take as long as they take

    try:
        req = json.loads(bytes(buf).decode("utf-8"))
        command = req["command"]
        if not isinstance(command, str) or not command.strip():
            raise ValueError("empty command")
    except Exception:
        reply(conn, {"error": "malformed request"})
        return

    try:
        timeout = float(req.get("timeout") or DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT
    timeout = max(1.0, min(timeout, MAX_TIMEOUT))

    if not approved(command, cwd, label):
        reply(conn, {"error": "denied on the host"})
        return
    reply(conn, run_command(command, cwd, timeout))


def reply(conn: socket.socket, payload: dict) -> None:
    try:
        conn.sendall((json.dumps(payload) + "\n").encode())
    except OSError:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--socket", required=True, help="Unix socket to listen on")
    ap.add_argument("--cwd", required=True, help="working directory for commands")
    ap.add_argument("--label", default="", help="session name shown in the dialog")
    args = ap.parse_args()

    cwd = Path(args.cwd)
    sock_path = Path(args.socket)
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    if sock_path.exists():
        sock_path.unlink()

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock_path))
    os.chmod(sock_path, 0o600)
    srv.listen(8)

    # PR_SET_PDEATHSIG gets us a SIGTERM when the launcher's podman exits; turn it
    # into a normal unwind so the socket is removed.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    # The launcher waits for this line before handing the socket to podman.
    print("ready", flush=True)
    try:
        while True:
            conn, _ = srv.accept()
            with conn:
                try:
                    handle(conn, cwd, args.label)
                except Exception as e:  # one bad request must not kill the daemon
                    print(f"host-execd: {e!r}", file=sys.stderr)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        srv.close()
        sock_path.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
