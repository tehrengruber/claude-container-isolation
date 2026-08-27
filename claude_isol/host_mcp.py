#!/usr/bin/env python3
"""In-container MCP server for `claude-isol --host-exec`.

Bind-mounted read-only into the container (like `notify_client.py`) and started
by Claude Code over stdio, it exposes a single tool -- `mcp__host__run` -- that
forwards a shell command to the host daemon (`host_execd.py`) on the socket
named by $CLAUDE_ISOL_HOSTEXEC_SOCK, and hands back what it ran.

Speaks just enough MCP to be that one tool, over the JSON-RPC framing Claude
Code uses on stdio: one JSON object per line. Nothing here is a security
boundary -- the confirmation dialog lives on the host side, out of reach of
anything in this container.
"""
from __future__ import annotations

import json
import os
import socket
import sys

SOCK_ENV = "CLAUDE_ISOL_HOSTEXEC_SOCK"
DEFAULT_PROTOCOL = "2025-06-18"
SERVER_INFO = {"name": "claude-isol-host", "version": "1"}

TOOL = {
    "name": "run",
    "description": (
        "Run a shell command on the HOST, outside this sandbox: the host's own "
        "filesystem, network, devices and credentials, with `bash -lc` in the "
        "directory this session was launched from. Every call raises a "
        "confirmation dialog on the host showing the command verbatim, and "
        "nothing runs unless the user approves it there. Use it only for work "
        "that genuinely cannot happen inside the sandbox, and keep the command "
        "short enough to read in a dialog."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to run on the host.",
            },
            "timeout": {
                "type": "number",
                "description": "Seconds the command may run for (default 600).",
            },
        },
        "required": ["command"],
    },
}


def send(msg: dict) -> None:
    msg["jsonrpc"] = "2.0"
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def call_host(command: str, timeout=None) -> dict:
    path = os.environ.get(SOCK_ENV)
    if not path:
        return {"error": f"{SOCK_ENV} is not set -- was --host-exec passed?"}
    req = {"command": command}
    if timeout is not None:
        req["timeout"] = timeout
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(path)
            s.sendall((json.dumps(req) + "\n").encode())
            s.shutdown(socket.SHUT_WR)
            buf = bytearray()
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
    except OSError as e:
        return {"error": f"cannot reach the host executor: {e}"}
    try:
        return json.loads(bytes(buf).decode("utf-8"))
    except ValueError:
        return {"error": "malformed reply from the host executor"}


def render(res: dict) -> tuple[str, bool]:
    """(text, is_error). A non-zero exit is a result, not a tool failure; only a
    denial or a broken transport is."""
    if res.get("exit") is None:
        return res.get("error", "the host executor returned nothing"), True
    parts = [f"exit code: {res['exit']}"]
    for stream in ("stdout", "stderr"):
        if res.get(stream):
            parts.append(f"{stream}:\n{res[stream]}")
    if len(parts) == 1:
        parts.append("(no output)")
    return "\n\n".join(parts), False


def handle_call(params: dict) -> dict:
    if params.get("name") != TOOL["name"]:
        return {"content": [{"type": "text", "text": "no such tool"}], "isError": True}
    args = params.get("arguments") or {}
    command = args.get("command")
    if not isinstance(command, str) or not command.strip():
        return {"content": [{"type": "text", "text": "`command` is required"}],
                "isError": True}
    text, is_error = render(call_host(command, args.get("timeout")))
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        method, mid = msg.get("method"), msg.get("id")
        if method is None:  # a response to something we never sent
            continue

        if method == "initialize":
            proto = (msg.get("params") or {}).get("protocolVersion") or DEFAULT_PROTOCOL
            send({"id": mid, "result": {"protocolVersion": proto,
                                        "capabilities": {"tools": {}},
                                        "serverInfo": SERVER_INFO}})
        elif method == "tools/list":
            send({"id": mid, "result": {"tools": [TOOL]}})
        elif method == "tools/call":
            send({"id": mid, "result": handle_call(msg.get("params") or {})})
        elif method == "ping":
            send({"id": mid, "result": {}})
        elif mid is not None:  # any other request; notifications need no reply
            send({"id": mid, "error": {"code": -32601, "message": "method not found"}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
