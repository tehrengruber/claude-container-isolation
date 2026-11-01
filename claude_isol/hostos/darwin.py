#!/usr/bin/env python3
"""macOS: container mode through podman machine, --local under Seatbelt.

podman on a Mac is a client for a Linux VM (`podman machine`), and that one
fact decides what carries over. The container runs on the VM's kernel, behind
the VM's network: a Unix socket bind-mounted from here arrives as a dead file,
and pasta forwards a port to the VM's loopback rather than this Mac's. So
everything that hands the container a way back to the host -- the JetBrains MCP
proxy, host notifications, --host-exec, --tcp-forward -- is off. --no-lan and
--vm are built on Linux kernel features and have no counterpart here.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

NAME = "darwin"
LABEL = "macOS"
BACKENDS = {"container": "container", "local": "seatbelt"}
UNSUPPORTED = {
    "no_lan": "the egress filter is a Linux cgroup eBPF program",
    "host_exec": "its socket cannot reach this Mac from inside podman machine's VM",
    "tcp_forwards": "pasta forwards to podman machine's VM, not to this Mac",
    "dev_binds": "it is a bubblewrap option, and --local uses Seatbelt here",
}
CONTAINER_SHARES_HOST = False


def submounts_under(path: Path) -> list[str]:
    """Mountpoints nested strictly under `path`, from mount(8): macOS has no
    /proc/self/mountinfo. Each line reads `<device> on <mountpoint> (<opts>)`."""
    root = str(path)
    out = []
    listing = subprocess.run(["/sbin/mount"], capture_output=True, text=True)
    for line in listing.stdout.splitlines():
        _, sep, rest = line.partition(" on ")
        if not sep:
            continue
        mp = rest.rsplit(" (", 1)[0]
        if mp != root and mp.startswith(root + "/"):
            out.append(mp)
    return out
