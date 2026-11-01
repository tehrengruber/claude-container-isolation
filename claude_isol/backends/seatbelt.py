#!/usr/bin/env python3
"""--local on macOS: the host's own claude under Seatbelt (sandbox-exec).

bubblewrap gives the sandbox a filesystem of its own, built out of mounts.
Seatbelt cannot mount anything; it can only allow or deny access to the real
one. So the shape --local has on Linux is rebuilt here from rules plus an
environment:

  * HOME is a fresh per-run directory rather than a tmpfs. It holds symlinks to
    ~/.claude and the scoped gh/git credentials, and a throwaway copy of
    ~/.claude.json. It is read-only, as on Linux, apart from the
    --tmpfs-home-subdirectory paths -- or all of it under --tmpfs-home.
  * The real home directory cannot be read, except what is linked in above, the
    working directory, -v sources and the claude install. Only its ancestors'
    metadata stays visible, since resolving a path walks them.
  * Nothing can be written except the working directory, ~/.claude (with
    CLAUDE_CONFIG_RO denied on top, existing or not), the scoped credentials,
    the per-run TMPDIR, /private/tmp, /dev, and -v sources not marked ro.
  * No Apple events and no LaunchServices, or `osascript` could have Terminal
    run a command, and `open` launch an app, outside the sandbox. Signals reach
    only processes inside it.

What it cannot match: the session shares the host's process table, network
and IPC, and reaches the same system services -- the login keychain included,
which is where claude keeps its credentials on macOS.

Seatbelt matches the real path of whatever is opened, after symlinks. That is
why every path in the profile is resolved first (/var is /private/var, /tmp is
/private/tmp), and why the HOME symlinks need no rules of their own.

The per-run directory lives in TMPDIR and outlives the session, since the
launcher execs sandbox-exec in place like every backend execs its foreground
process; macOS clears stale entries there by itself.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import click

from ..claude_isol import (CLAUDE_CONFIG_RO, ENV_ALLOWLIST, HOME,
                           confirm_cwd_submounts, parse_volume, resolve_prompt,
                           stdin_from_text)
from ..launch import Launch

SANDBOX_EXEC = "/usr/bin/sandbox-exec"


def _sb(path: str) -> str:
    """`path` as an SBPL string literal."""
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _under(path: str, top: str) -> bool:
    return path == top or path.startswith(top + "/")


def _ancestors(path: str, top: str) -> list[str]:
    """`top` and every directory between it and `path`, which lies under it."""
    out = []
    d = os.path.dirname(path)
    while _under(d, top):
        out.append(d)
        if d == top:
            break
        d = os.path.dirname(d)
    return out


def _claude_install_dirs() -> list[str]:
    """Where the claude binary lives, so it stays readable when that is under
    HOME (the native installer's ~/.local/...). Two directories, for the same
    reason as on Linux: the PATH entry is often a symlink into a versioned
    install dir, and both ends have to resolve."""
    claude_bin = shutil.which("claude")
    if not claude_bin:
        return []
    return list(dict.fromkeys(
        os.path.realpath(d) for d in (os.path.dirname(claude_bin),
                                      os.path.dirname(os.path.realpath(claude_bin)))))


def _volumes(specs: list[str]) -> list[tuple[str, bool]]:
    """-v specs as (real path, read-only). Seatbelt grants access to a path but
    cannot move it, so a spec has to name the same place on both sides."""
    out = []
    for spec in specs:
        src, dst, opts = parse_volume(spec)
        real = os.path.realpath(src)
        if real != os.path.realpath(dst):
            raise click.UsageError(
                f"-v {spec}: --local on macOS cannot place a path elsewhere; "
                "use -v SRC (or SRC:SRC) to make SRC reachable where it is")
        out.append((real, "ro" in opts.split(",")))
    return out


def build_profile(cwd: str, run_home: str, run_tmp: str,
                  volumes: list[tuple[str, bool]], home_writable: list[str]) -> str:
    """The SBPL profile for one run. Later rules win, so each deny is followed
    by the exceptions to it."""
    home = os.path.realpath(HOME)
    claude_dir = os.path.realpath(HOME / ".claude")
    gh = os.path.realpath(HOME / ".config" / "gh-claude")
    gitconfig = os.path.realpath(HOME / ".gitconfig-claude")

    readable = [cwd, claude_dir, gh, gitconfig, *_claude_install_dirs(),
                *(src for src, _ in volumes)]
    writable = [cwd, claude_dir, gh, gitconfig, run_tmp, "/private/tmp", "/dev",
                f"{run_home}/.claude.json", *home_writable,
                *(src for src, ro in volumes if not ro)]
    traversed = dict.fromkeys(a for p in readable if _under(p, home)
                              for a in _ancestors(p, home))

    lines = [
        "(version 1)",
        "(allow default)",
        "(deny appleevent-send)",
        '(deny mach-lookup (global-name "com.apple.coreservices.launchservicesd"))',
        "(deny signal)",
        "(allow signal (target same-sandbox))",
        f"(deny file-read* (subpath {_sb(home)}))",
        *(f"(allow file-read* (subpath {_sb(p)}))" for p in readable),
        *(f"(allow file-read-metadata (literal {_sb(p)}))" for p in traversed),
        "(deny file-write*)",
        *(f"(allow file-write* (subpath {_sb(p)}))" for p in writable),
        *(f"(deny file-write* (subpath {_sb(f'{claude_dir}/{name}')}))"
          for name in CLAUDE_CONFIG_RO),
    ]
    return "\n".join(lines) + "\n"


def _populate_home(run_home: Path, home_subdirs: tuple[str, ...]) -> None:
    """The per-run HOME: links to the shared config, a copy of ~/.claude.json,
    and the --tmpfs-home-subdirectory directories."""
    (run_home / ".config").mkdir(parents=True)
    (run_home / ".claude").symlink_to(HOME / ".claude")
    (run_home / ".config" / "gh").symlink_to(HOME / ".config" / "gh-claude")
    (run_home / ".gitconfig").symlink_to(HOME / ".gitconfig-claude")
    # A throwaway copy, not the host file: ~/.claude.json carries mcpServers,
    # which the host's claude would launch on its next run.
    shutil.copy(HOME / ".claude.json", run_home / ".claude.json")
    for rel in home_subdirs:
        (run_home / rel).mkdir(parents=True, exist_ok=True)


def launch(spec: Launch) -> None:
    if not os.access(SANDBOX_EXEC, os.X_OK):
        click.echo(f"--local requires {SANDBOX_EXEC}, which ships with macOS",
                   err=True)
        raise SystemExit(2)

    confirm_cwd_submounts(spec.cwd, spec.mount_cwd_recursively)
    # Scoped gh/git credentials, created on first run (shared with container mode).
    (HOME / ".config" / "gh-claude").mkdir(parents=True, exist_ok=True)
    (HOME / ".gitconfig-claude").touch(exist_ok=True)

    prompt = None
    if spec.drop_shell:
        inner = [os.environ.get("SHELL", "/bin/zsh")]
    elif spec.exec_cmd:
        inner = spec.args
    else:
        # The sandbox keeps host paths, so a prompt file is referenced where it
        # already lives; resolve_prompt adds it to the -v specs, read-only.
        dst = str(spec.prompt_file.resolve()) if spec.prompt_file else ""
        args, prompt = resolve_prompt(spec, dst)
        inner = ["claude", *args]

    volumes = _volumes(spec.volumes)
    run_dir = Path(os.path.realpath(tempfile.mkdtemp(prefix="claude-isol-")))
    run_home, run_tmp = run_dir / "home", run_dir / "tmp"
    run_tmp.mkdir()
    _populate_home(run_home, spec.home_subdirs)
    home_writable = ([str(run_home)] if spec.tmpfs_home else
                     [str(run_home / rel) for rel in spec.home_subdirs])

    profile = run_dir / "profile.sb"
    profile.write_text(build_profile(os.path.realpath(spec.cwd), str(run_home),
                                     str(run_tmp), volumes, home_writable))

    # An explicit environment, then an allowlist: forwarding ours whole would
    # hand GH_TOKEN, ANTHROPIC_API_KEY and friends to the sandbox and defeat the
    # scoped credentials. Anything else the caller actually wants is what -e is for.
    env = {var: os.environ[var] for var in ENV_ALLOWLIST if var in os.environ}
    env.update(HOME=str(run_home), TMPDIR=f"{run_tmp}/")
    for var in spec.env_vars:
        name, _, val = var.partition("=")
        val = val if "=" in var else os.environ.get(name)
        if val is not None:
            env[name] = val

    if prompt is not None:
        stdin_from_text(prompt)

    os.execve(SANDBOX_EXEC, [SANDBOX_EXEC, "-f", str(profile), *inner], env)
