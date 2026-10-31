#!/usr/bin/env python3
"""The default mode: claude in a rootless podman container."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Optional

from ..claude_isol import (CLAUDE_CONFIG_RO, DEFAULT_IMAGE, HOME, PUBLIC_DNS,
                           confirm_cwd_submounts,
                           ensure_image, find_ide_lock, hostexec_wiring, image_id,
                           install_claude_on_top, notify_wiring, resolve_prompt,
                           setup_nolan_cgroup, spawn_proxy, stdin_from_text)
from ..launch import Launch

__all__ = ["launch", "image_id"]


def launch(spec: Launch) -> None:
    image = spec.image
    if image is None:
        # No image specified: use the bundled default, building it on demand.
        image = DEFAULT_IMAGE
        ensure_image(image)
    elif spec.install_claude:
        image = install_claude_on_top(image)

    cwd = spec.cwd
    confirm_cwd_submounts(cwd, spec.mount_cwd_recursively)

    prompt = None
    if not spec.drop_shell and not spec.exec_cmd:
        dst = (f"/run/claude-isol-prompt{spec.prompt_file.suffix}"
               if spec.prompt_file else "")
        args, prompt = resolve_prompt(spec, dst)
    else:
        args = spec.args

    ide_lock = (None if (spec.drop_shell or spec.exec_cmd)
                else find_ide_lock(cwd))

    # Without --userns=keep-id, container uid 0 maps to the host user, so HOME
    # inside the container is /root rather than the host home path.
    container_home = Path("/root") if spec.no_userns else HOME

    pasta_opts: list[str] = []
    extra_mounts: list[str] = []
    ide_env: list[str] = []

    if ide_lock is not None:
        proxy_port = spawn_proxy(ide_lock.stem)
        tmp_ide = Path(tempfile.mkdtemp(prefix="claude-ide-"))
        shutil.copy(ide_lock, tmp_ide / f"{proxy_port}.lock")
        pasta_opts.append(f"-T,{proxy_port}")
        extra_mounts = ["-v", f"{tmp_ide}:{container_home}/.claude/ide"]
        # Name the port out of band. Left to discover the IDE on its own, claude
        # vets a lock file by signal-probing the pid recorded in it and walking the
        # process tree looking for that pid -- both of which are answers only the
        # host's pid namespace can give. Announcing the port instead satisfies the
        # lookup directly, so the container keeps its own pid namespace.
        ide_env = ["-e", f"CLAUDE_CODE_SSE_PORT={proxy_port}"]

    pasta_opts.extend(f"-T,{port}" for port in spec.tcp_forwards)
    network = "pasta" + (":" + ",".join(pasta_opts) if pasta_opts else "")

    gh_config = HOME / ".config" / "gh-claude"
    gh_config.mkdir(parents=True, exist_ok=True)

    gitconfig = HOME / ".gitconfig-claude"
    gitconfig.touch(exist_ok=True)

    # The parts of ~/.claude the host's claude would execute on its next run are
    # mounted read-only over the share (see CLAUDE_CONFIG_RO), and ~/.claude.json
    # -- which carries mcpServers -- is handed over as a throwaway copy rather
    # than the real file. Without this a session can write itself a hook that runs
    # unsandboxed on the host the next time claude starts there.
    config_ro = [
        arg for name in CLAUDE_CONFIG_RO
        if (HOME / ".claude" / name).exists()
        for arg in ("-v", f"{HOME / '.claude' / name}:"
                          f"{container_home}/.claude/{name}:ro")
    ]
    claude_json = Path(tempfile.mkdtemp(prefix="claude-isol-cfg-")) / "claude.json"
    shutil.copy(HOME / ".claude.json", claude_json)

    notify_mounts: list[str] = []
    notify_env: list[str] = []
    notify_args: list[str] = []
    if not spec.drop_shell and not spec.exec_cmd:
        notify_mounts, notify_env, notify_args = notify_wiring()

    hostexec_mounts: list[str] = []
    hostexec_env: list[str] = []
    hostexec_args: list[str] = []
    if spec.host_exec:
        hostexec_mounts, hostexec_env, hostexec_args = hostexec_wiring(cwd)

    # No pty when the prompt is arriving on stdin: -t would put one between the
    # pipe and claude, and claude reads a tty as an interactive session.
    tty_flag = (["-t"] if prompt is None and sys.stdin.isatty() and sys.stdout.isatty()
                else [])
    userns_flag = [] if spec.no_userns else ["--userns=keep-id"]
    # A tmpfs HOME wipes whatever the image ships under the home dir; the config
    # bind mounts below are layered on top (podman orders mounts parent-first).
    tmpfs_flag = ["--tmpfs", str(container_home)] if spec.tmpfs_home else []

    # Writable scratch inside an otherwise untouched HOME. Without this, HOME in the
    # container is the image's root-owned directory with only the config paths bound
    # over it, so a tool that wants ~/.cache or ~/.npm cannot even create it. podman
    # orders mounts parent-first, so these land on top of a tmpfs HOME as well.
    # mode=1777 because the tmpfs is created root-owned and the session runs as the
    # mapped user; exec so it matches /tmp (podman's tmpfs default is noexec).
    home_subdir_flags = [
        arg for rel in spec.home_subdirs
        for arg in ("--tmpfs", f"{container_home / rel}:rw,exec,mode=1777")
    ]

    # --no-lan: run the whole podman process tree under a cgroup that carries the
    # egress filter, and pin DNS to the public resolver.
    #
    # Two things matter here. (1) We must use the cgroupfs manager: with the
    # default systemd manager, rootless podman asks the user systemd manager for a
    # fresh podman-<pid>.scope and moves itself there, escaping our cgroup. (2) We
    # do NOT pass our cgroup's full root-relative path as --cgroup-parent -- rootless
    # podman+cgroupfs interprets --cgroup-parent relative to its *current* cgroup, so
    # the full path gets appended to wherever podman already is, creating a doubled,
    # unfiltered cgroup elsewhere. Instead we move ourselves into the filtered cgroup
    # just before exec (see below); podman then stays put (cgroupfs manager) and
    # creates conmon, pasta and the container as descendants -- all inheriting the
    # filter. pasta is the process that re-originates the container's packets, so it
    # too must live under the filtered cgroup, which this guarantees.
    nolan_flags: list[str] = []
    nolan_cg: Optional[Path] = None
    if spec.no_lan:
        nolan_cg = setup_nolan_cgroup()
        nolan_flags = [
            "--cgroup-manager=cgroupfs",
            "--cgroup-parent=ctr",
            *[a for ns in PUBLIC_DNS for a in ("--dns", ns)],
        ]

    cmd = [
        "podman", "run", "--rm", "-i", *tty_flag,
        f"--network={network}",
        *nolan_flags,
        *userns_flag,
        *tmpfs_flag,
        *home_subdir_flags,
        "-v", f"{HOME}/.claude:{container_home}/.claude",
        *extra_mounts,
        "-v", f"{claude_json}:{container_home}/.claude.json",
        *config_ro,
        "-v", f"{gh_config}:{container_home}/.config/gh",
        "-v", f"{gitconfig}:{container_home}/.gitconfig",
        "-v", f"{cwd}:{cwd}",
        *[arg for vol in spec.volumes for arg in ("-v", vol)],
        *notify_mounts,
        *hostexec_mounts,
        "-w", str(cwd),
        "-e", f"HOME={container_home}",
        "-e", "TERM",
        *ide_env,
        *[arg for var in spec.env_vars for arg in ("-e", var)],
        *notify_env,
        *hostexec_env,
        # Last of the options, just before the image, so a --podman-arg can override
        # anything set above it -- and so a stray one lands as a podman flag rather
        # than as an argument to claude.
        *spec.podman_args,
        image,
        *(["bash"] if spec.drop_shell else
          list(args) if spec.exec_cmd else
          ["claude", *notify_args, *hostexec_args, *args]),
    ]

    # Move ourselves into the filtered cgroup last, just before exec: with the
    # cgroupfs manager podman stays in this cgroup and spawns conmon/pasta/the
    # container beneath it, so the egress filter is inherited by the lot.
    if nolan_cg is not None:
        (nolan_cg / "cgroup.procs").write_text(str(os.getpid()))

    if prompt is not None:
        stdin_from_text(prompt)

    os.execvp("podman", cmd)
