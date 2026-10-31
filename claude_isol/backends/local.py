#!/usr/bin/env python3
"""--local: the host's own claude under a bubblewrap sandbox, no container."""
from __future__ import annotations

import os
import shutil

import click

from ..claude_isol import (HOME, build_bwrap_cmd, confirm_cwd_submounts,
                           public_resolv, resolve_prompt, setup_nolan_cgroup,
                           stdin_from_text)
from ..launch import Launch


def launch(spec: Launch) -> None:
    if shutil.which("bwrap") is None:
        click.echo("--local requires bubblewrap (bwrap); install the "
                   "'bubblewrap' package", err=True)
        raise SystemExit(2)

    confirm_cwd_submounts(spec.cwd, spec.mount_cwd_recursively)
    # Scoped gh/git credentials, created on first run (shared with container mode).
    (HOME / ".config" / "gh-claude").mkdir(parents=True, exist_ok=True)
    (HOME / ".gitconfig-claude").touch(exist_ok=True)

    prompt = None
    if spec.drop_shell:
        inner = [os.environ.get("SHELL", "bash")]
    elif spec.exec_cmd:
        inner = spec.args
    else:
        # The sandbox keeps host paths, so a prompt file is referenced where it
        # already lives; the bind rides in with the user -v specs, which are
        # emitted after the tmpfs HOME and so survive a prompt file under it.
        dst = str(spec.prompt_file.resolve()) if spec.prompt_file else ""
        args, prompt = resolve_prompt(spec, dst)
        inner = ["claude", *args]

    resolv = None
    if spec.no_lan:
        run_cg = setup_nolan_cgroup()
        resolv = public_resolv()
        # Move ourselves into the filtered cgroup; the exec'd bwrap (and its
        # whole tree) inherit the membership and thus the egress filter.
        (run_cg / "cgroup.procs").write_text(str(os.getpid()))

    if prompt is not None:
        stdin_from_text(prompt)

    os.execvp("bwrap", build_bwrap_cmd(spec.cwd, spec.volumes, inner, resolv,
                                       list(spec.dev_binds), spec.home_subdirs,
                                       spec.tmpfs_home, spec.env_vars))
