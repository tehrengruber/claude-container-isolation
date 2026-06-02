#!/usr/bin/env python3
"""Host-side notification daemon for claude-isol.

Listens on a Unix socket and receives one JSON line per Claude Code hook event
from containerized sessions (see `notify_client.py`). Each line is

    {"instance": "<id>", "hook": { <verbatim hook payload> }}

The daemon does all the interpretation: it maps the hook event to a per-instance
state (busy / needs-input / done) and renders a single, continuously updated
desktop notification via D-Bus (`gdbus`) — one line per instance, an icon for
the state, and a short summary. Updating one notification in place (via
`replaces_id`) turns it into a live status board.

States:
    ⏳ busy   — Claude is working
    ❓ input  — Claude is blocked on you (permission / idle prompt)
    ✅ done   — Claude finished a turn, or a fresh session is idle

Transitions into ❓ and the finish-✅ raise an alert (normal urgency + sound);
busy updates are silent (low urgency) and debounced so rapid tool calls don't
spam the notification server.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import socket
import sys
from pathlib import Path

APP_NAME = "claude-isol"
NOTIFY_DEST = "org.freedesktop.Notifications"
NOTIFY_PATH = "/org/freedesktop/Notifications"

ICONS = {"idle": "✅", "busy": "⏳", "input": "❓", "done": "✅"}
PRIORITY = {"input": 0, "busy": 1, "done": 2, "idle": 3}
SUMMARY_MAX = 60
DEBOUNCE_SECONDS = 0.25

UNIT_TEMPLATE = """\
[Unit]
Description=claude-isol host notification daemon

[Service]
Type=simple
ExecStart={exec_start}
Restart=on-failure

[Install]
WantedBy=default.target
"""


def runtime_dir() -> Path:
    rd = os.environ.get("XDG_RUNTIME_DIR")
    return Path(rd) if rd else Path(f"/run/user/{os.getuid()}")


def socket_path() -> Path:
    return runtime_dir() / "claude-isol" / "notify.sock"


def interpret(hook: dict):
    """Map a hook payload to an action.

    Returns None to ignore, ("remove",) to drop the instance, or
    ("upsert", state, summary, alert).
    """
    event = hook.get("hook_event_name")

    if event == "SessionStart":
        return ("upsert", "idle", "started", False)
    if event == "UserPromptSubmit":
        return ("upsert", "busy", "working…", False)
    if event == "PreToolUse":
        return ("upsert", "busy", f"running {hook.get('tool_name') or 'tool'}", False)
    if event == "Notification":
        ntype = hook.get("notification_type")
        defaults = {
            "permission_prompt": "needs permission",
            "idle_prompt": "waiting for input",
            "elicitation_dialog": "needs input",
        }
        if ntype in defaults:
            return ("upsert", "input", hook.get("message") or defaults[ntype], True)
        return None  # auth_success / elicitation_complete / ... are not actionable
    if event == "Stop":
        return ("upsert", "done", "finished", True)
    if event == "SessionEnd":
        if hook.get("reason") in ("clear", "compact", "resume"):
            return None  # the session process stays alive; keep the line
        return ("remove",)
    return None


class Notifier:
    def __init__(self) -> None:
        self.instances: dict[str, tuple[str, str, str]] = {}  # id -> (state, label, summary)
        self.notif_id = 0
        self.alert_pending = False
        self.wake = asyncio.Event()

    def apply(self, instance: str, hook: dict) -> bool:
        action = interpret(hook)
        if action is None:
            return False
        if action[0] == "remove":
            return self.instances.pop(instance, None) is not None
        _, state, summary, alert = action
        summary = " ".join((summary or "").split())
        if len(summary) > SUMMARY_MAX:
            summary = summary[: SUMMARY_MAX - 1] + "…"
        label = Path(hook.get("cwd") or "").name or "session"
        self.instances[instance] = (state, label, summary)
        if alert:
            self.alert_pending = True
        return True

    def reset(self) -> None:
        self.instances.clear()

    def body(self) -> str:
        rows = sorted(self.instances.values(), key=lambda r: (PRIORITY.get(r[0], 9), r[1]))
        return "\n".join(f"{ICONS.get(s, '•')} {label}: {summary}" for s, label, summary in rows)

    async def render(self) -> None:
        if not self.instances:
            await self.close()
            return
        alert = self.alert_pending
        self.alert_pending = False
        n = len(self.instances)
        title = "Claude Code" if n == 1 else f"Claude Code — {n} sessions"
        if alert:
            hints = "{'urgency': <byte 1>}"
        else:
            hints = "{'urgency': <byte 0>, 'suppress-sound': <true>}"
        out = await self._gdbus(
            "org.freedesktop.Notifications.Notify",
            APP_NAME, str(self.notif_id), "", title, self.body(), "[]", hints, "0",
        )
        if out is not None:
            m = re.search(r"uint32\s+(\d+)", out)  # gdbus prints "(uint32 N,)"
            if m:
                self.notif_id = int(m.group(1))

    async def close(self) -> None:
        if self.notif_id:
            await self._gdbus(
                "org.freedesktop.Notifications.CloseNotification", str(self.notif_id)
            )
            self.notif_id = 0

    async def _gdbus(self, method: str, *args: str):
        cmd = [
            "gdbus", "call", "--session",
            "--dest", NOTIFY_DEST,
            "--object-path", NOTIFY_PATH,
            "--method", method, *args,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
        except FileNotFoundError:
            print("notifyd: gdbus not found on PATH", file=sys.stderr)
            return None
        if proc.returncode != 0:
            print(f"notifyd: gdbus {method} failed: {stderr.decode().strip()}", file=sys.stderr)
            return None
        return stdout.decode()

    def touch(self) -> None:
        self.wake.set()

    async def render_loop(self) -> None:
        while True:
            await self.wake.wait()
            self.wake.clear()
            if not self.alert_pending:
                await asyncio.sleep(DEBOUNCE_SECONDS)  # coalesce bursts of busy updates
            await self.render()


async def handle_conn(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                      notifier: Notifier) -> None:
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=5.0)
    except (asyncio.TimeoutError, OSError):
        line = b""
    finally:
        writer.close()
    if not line:
        return
    try:
        msg = json.loads(line)
    except Exception:
        return

    if isinstance(msg, dict) and "control" in msg:
        if msg.get("control") == "reset":
            notifier.reset()
            notifier.touch()
        return

    try:
        instance = msg["instance"]
        hook = msg["hook"]
    except (KeyError, TypeError):
        return
    if notifier.apply(instance, hook):
        notifier.touch()


async def serve() -> None:
    path = socket_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        path.unlink()

    notifier = Notifier()
    asyncio.create_task(notifier.render_loop())
    server = await asyncio.start_unix_server(
        lambda r, w: handle_conn(r, w, notifier), path=str(path)
    )
    os.chmod(path, 0o600)
    print(f"notifyd: listening on {path}", file=sys.stderr)
    async with server:
        await server.serve_forever()


def send_control(payload: dict) -> int:
    path = socket_path()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(2.0)
            s.connect(str(path))
            s.sendall((json.dumps(payload) + "\n").encode())
    except OSError as e:
        print(f"notifyd not reachable at {path}: {e}", file=sys.stderr)
        return 1
    return 0


def install_user_unit() -> int:
    exe = shutil.which("claude-isol-notifyd") or os.path.realpath(sys.argv[0])
    dst_dir = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "systemd" / "user"
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / "claude-isol-notifyd.service"
    dst.write_text(UNIT_TEMPLATE.format(exec_start=exe))
    print(f"installed {dst}")
    print("enable with: systemctl --user enable --now claude-isol-notifyd")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="claude-isol host notification daemon")
    ap.add_argument("--install-user-unit", action="store_true",
                    help="write a systemd --user unit to ~/.config/systemd/user and exit")
    ap.add_argument("--reset", action="store_true",
                    help="clear all instances from a running daemon and exit")
    args = ap.parse_args()

    if args.install_user_unit:
        return install_user_unit()
    if args.reset:
        return send_control({"control": "reset"})

    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
