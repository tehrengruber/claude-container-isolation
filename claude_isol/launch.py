#!/usr/bin/env python3
"""What the CLI collected, and which flags belong to which mode.

`claude_isol.py` parses options into a `Launch` and hands it to exactly one
backend (see `backends/`); nothing else branches on the mode. A new mode means a
new backend module plus rows in MODE_ONLY -- not another `if` in shared code.
What the host OS cannot do is declared in the same spirit, in hostos/.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import click

from . import hostos

# Flags that only mean something in some modes. Checked in one loop, so an
# unsupported combination fails the same way everywhere instead of growing its
# own `if ... raise UsageError`.
MODE_ONLY: dict[str, tuple[str, ...]] = {
    "image": ("container",),
    "install_claude": ("container",),
    "no_userns": ("container",),
    "tmpfs_home": ("container", "local"),
    "tcp_forwards": ("container",),
    "podman_args": ("container",),
    "host_exec": ("container",),
    "dev_binds": ("local",),
    "vm_memory": ("vm",),
    "vm_cpus": ("vm",),
}

# How each flag is spelled on the command line, for error messages.
FLAG_NAMES = {
    "image": "--image", "install_claude": "--install-claude",
    "no_userns": "--no-userns", "tmpfs_home": "--tmpfs-home",
    "tcp_forwards": "--tcp-forward", "podman_args": "--podman-arg",
    "host_exec": "--host-exec", "dev_binds": "--dev-bind",
    "vm_memory": "--vm-memory", "vm_cpus": "--vm-cpus", "no_lan": "--no-lan",
}

MODE_FLAGS = {"container": "(the default)", "local": "--local", "vm": "--vm"}


@dataclass
class Launch:
    """One run, as the CLI understood it. Backends read the fields they care
    about; MODE_ONLY guarantees the rest are empty."""
    mode: str
    cwd: Path
    args: list[str]
    drop_shell: bool = False
    exec_cmd: bool = False
    prompt_file: Optional[Path] = None
    volumes: list[str] = field(default_factory=list)
    env_vars: tuple[str, ...] = ()
    home_subdirs: tuple[str, ...] = ()
    no_lan: bool = False
    mount_cwd_recursively: bool = False
    # container only
    image: Optional[str] = None
    install_claude: bool = False
    no_userns: bool = False
    tmpfs_home: bool = False
    tcp_forwards: tuple[int, ...] = ()
    host_exec: bool = False
    podman_args: tuple[str, ...] = ()
    # local only
    dev_binds: tuple[str, ...] = ()
    # vm only
    vm_memory: Optional[str] = None
    vm_cpus: Optional[int] = None

    def check_mode_flags(self) -> None:
        """Reject flags that do not apply to the selected mode, and a mode or
        flag the host OS has no machinery for (see hostos/)."""
        if self.mode not in hostos.BACKENDS:
            raise click.UsageError(
                f"{MODE_FLAGS[self.mode]} is not available on {hostos.LABEL}")
        for attr, why in hostos.UNSUPPORTED.items():
            if getattr(self, attr, None):
                raise click.UsageError(
                    f"{FLAG_NAMES[attr]} is not available on {hostos.LABEL}: {why}")
        for attr, modes in MODE_ONLY.items():
            if not getattr(self, attr, None) or self.mode in modes:
                continue
            allowed = " / ".join(MODE_FLAGS[m] for m in modes)
            raise click.UsageError(
                f"{FLAG_NAMES[attr]} applies to {allowed}, "
                f"not {MODE_FLAGS[self.mode]}")
