#!/usr/bin/env python3
"""Filtering proxy between Claude Code CLI and a JetBrains IDE MCP endpoint.

Listens on 127.0.0.1 on an ephemeral port, forwards WebSocket traffic to
`--upstream`, and drops blocked tools from `tools/list` responses while
rejecting `tools/call` invocations for them. The chosen port is printed to
stdout on the first line.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from websockets.asyncio.client import connect as ws_connect
from websockets.asyncio.server import serve as ws_serve

DEFAULT_LOG_PATH = Path.home() / ".claude" / "mcp-proxy.log"

blocked_log = logging.getLogger("mcp-proxy.blocked")
blocked_log.propagate = False

# Only tools listed here are forwarded. Anything else is stripped from
# tools/list responses and rejected on tools/call.
ALLOWED_TOOLS = {
    "getDiagnostics",
    "openDiff",
    "close_tab",
    "closeAllDiffTabs"
}

PASSTHROUGH_HOP_HEADERS = {
    "host",
    "upgrade",
    "connection",
    "sec-websocket-key",
    "sec-websocket-version",
    "sec-websocket-extensions",
    "sec-websocket-protocol",
    "sec-websocket-accept",
}


def filter_tools_list(msg):
    result = msg.get("result")
    if isinstance(result, dict) and isinstance(result.get("tools"), list):
        result["tools"] = [
            t for t in result["tools"] if t.get("name") in ALLOWED_TOOLS
        ]
    return msg


async def bridge(client_ws, upstream_url):
    forwarded = {}
    for key, val in client_ws.request.headers.raw_items():
        if key.lower() not in PASSTHROUGH_HOP_HEADERS:
            forwarded[key] = val

    offered = client_ws.request.headers.get_all("Sec-WebSocket-Protocol")
    subprotocols = []
    for entry in offered:
        subprotocols.extend(p.strip() for p in entry.split(",") if p.strip())

    target = upstream_url.rstrip("/") + (client_ws.request.path or "/")

    async with ws_connect(
        target,
        additional_headers=forwarded,
        subprotocols=subprotocols or None,
    ) as ide_ws:

        async def client_to_ide():
            async for raw in client_ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    await ide_ws.send(raw)
                    continue
                if msg.get("method") == "tools/call":
                    name = (msg.get("params") or {}).get("name")
                    if name not in ALLOWED_TOOLS:
                        blocked_log.info("tools/call blocked: %s", name)
                        await client_ws.send(json.dumps({
                            "jsonrpc": "2.0",
                            "id": msg.get("id"),
                            "error": {
                                "code": -32601,
                                "message": f"tool '{name}' not in mcp-proxy allowlist",
                            },
                        }))
                        continue
                await ide_ws.send(json.dumps(msg))

        async def ide_to_client():
            async for raw in ide_ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    await client_ws.send(raw)
                    continue
                await client_ws.send(json.dumps(filter_tools_list(msg)))

        tasks = [
            asyncio.create_task(client_to_ide()),
            asyncio.create_task(ide_to_client()),
        ]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--upstream", required=True, help="ws://host:port")
    parser.add_argument("--log", default=str(DEFAULT_LOG_PATH),
                        help="path for the blocked-call log")
    args = parser.parse_args()

    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handler = logging.FileHandler(log_path)
    log_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    blocked_log.addHandler(log_handler)
    blocked_log.setLevel(logging.INFO)

    async def handler(ws):
        try:
            await bridge(ws, args.upstream)
        except Exception as e:
            print(f"bridge error: {e!r}", file=sys.stderr)

    server = await ws_serve(handler, "127.0.0.1", 0)
    port = next(iter(server.sockets)).getsockname()[1]
    print(port, flush=True)
    try:
        await server.serve_forever()
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass