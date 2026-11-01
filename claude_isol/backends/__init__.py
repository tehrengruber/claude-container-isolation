#!/usr/bin/env python3
"""One module per way of running claude, each with the same entry point:

    launch(spec: Launch) -> NoReturn

A backend sets up whatever it needs -- forking helpers that die with us via
PR_SET_PDEATHSIG -- and then exec's the foreground process in place, so it
inherits the controlling terminal directly rather than through a wrapper.

Which module serves a mode is the host OS's call (hostos.BACKENDS): container
mode is the same everywhere, --local is `bwrap` on Linux and `seatbelt` on
macOS, and --vm exists on Linux only.

Imported lazily so that a backend's own dependencies (and the module-level work
they do) only load for the mode actually selected.
"""
from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

from .. import hostos

if TYPE_CHECKING:  # pragma: no cover
    from ..launch import Launch


def launch(spec: "Launch") -> None:
    """Hand the run to its backend. Does not return: the backend exec's."""
    if spec.mode not in hostos.BACKENDS:
        raise ValueError(f"mode {spec.mode!r} is not available on {hostos.LABEL}")
    import_module(f".{hostos.BACKENDS[spec.mode]}", __package__).launch(spec)
