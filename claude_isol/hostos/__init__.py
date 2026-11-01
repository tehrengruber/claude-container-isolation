#!/usr/bin/env python3
"""What differs between the host operating systems, behind one import.

`linux.py` and `darwin.py` each define the same small interface, and this
module re-exports whichever one matches the running host:

    NAME                   "linux" / "darwin", as in sys.platform
    LABEL                  how the OS is called in error messages
    BACKENDS               mode -> backend module (see backends/); a mode
                           missing here is not available on this OS
    UNSUPPORTED            Launch field -> why that flag cannot work here
    CONTAINER_SHARES_HOST  whether podman's containers run on this kernel, so a
                           Unix socket bind-mounted in, or a loopback port
                           forwarded by pasta, reaches this host
    submounts_under(path)  mountpoints nested under `path`

Machinery that exists on Linux alone -- the --no-lan cgroup filter, the parent
death signal, the XDG runtime dir -- is imported from `hostos.linux` by name,
so the import itself says the code is Linux-only. Every path that reaches it is
closed off on macOS by UNSUPPORTED, BACKENDS or CONTAINER_SHARES_HOST.
"""
from __future__ import annotations

import sys

if sys.platform == "darwin":
    from .darwin import (BACKENDS, CONTAINER_SHARES_HOST, LABEL, NAME,
                         UNSUPPORTED, submounts_under)
elif sys.platform.startswith("linux"):
    from .linux import (BACKENDS, CONTAINER_SHARES_HOST, LABEL, NAME,
                        UNSUPPORTED, submounts_under)
else:  # pragma: no cover
    raise ImportError(f"claude-isol does not support {sys.platform}")

__all__ = ["BACKENDS", "CONTAINER_SHARES_HOST", "LABEL", "NAME", "UNSUPPORTED",
           "submounts_under"]
