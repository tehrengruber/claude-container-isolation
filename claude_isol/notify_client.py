#!/usr/bin/env python3
"""In-container hook forwarder for claude-isol notifications.

Registered as the command for several Claude Code hook events inside the
container. It does no interpretation: it reads the hook payload from stdin,
wraps it together with this container's instance id, the host pid of the
process that owns the session (so the daemon can drop the line when the session
dies) and the directory the session was launched in, then writes one JSON line
to the host daemon's Unix socket. All event->state mapping happens host-side in
`notifyd.py`.

Every failure is swallowed and the process always exits 0, so a missing or slow
daemon can never block or break the Claude session.
"""
from __future__ import annotations

import json
import os
import socket
import sys


def main() -> int:
    sock_path = os.environ.get("CLAUDE_ISOL_NOTIFY_SOCK")
    instance = os.environ.get("CLAUDE_ISOL_INSTANCE")
    if not sock_path or not instance:
        return 0
    try:
        hook = json.load(sys.stdin)
    except Exception:
        return 0

    msg = {"instance": instance, "hook": hook}
    label = os.environ.get("CLAUDE_ISOL_LABEL")
    if label:
        msg["label"] = label
    try:
        msg["pid"] = int(os.environ["CLAUDE_ISOL_HOST_PID"])
    except (KeyError, ValueError):
        pass

    line = json.dumps(msg) + "\n"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            s.connect(sock_path)
            s.sendall(line.encode())
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
