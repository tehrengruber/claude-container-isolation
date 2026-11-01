#!/usr/bin/env python3
"""--local on Linux: the host's own claude under a bubblewrap sandbox, no container."""
from __future__ import annotations

import grp
import os
import pwd
import shutil
import tempfile
from pathlib import Path
from typing import Optional

import click

from ..claude_isol import (CLAUDE_CONFIG_RO, ENV_ALLOWLIST, HOME,
                           claude_json_copy, confirm_cwd_submounts, parse_volume,
                           public_resolv, resolve_prompt, stdin_from_text)
from ..hostos.linux import setup_nolan_cgroup
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


def _ca_trust_binds() -> list[str]:
    """Read-only binds so the CA trust store resolves inside the --local sandbox.

    /etc and /usr are already ro-bound, which covers the usual layouts (Debian and
    Arch keep certs under /etc + /usr; Fedora under /etc/pki). But some distros store
    the real certs elsewhere and only symlink into /etc -- notably openSUSE, whose
    /etc/ssl/certs/*.pem and /etc/ssl/ca-bundle.pem point into /var/lib/ca-certificates.
    A recursive /etc bind then carries those symlinks in but not their targets, so they
    dangle and TLS fails with "unable to get local issuer certificate". Resolve the
    well-known trust anchors -- and the symlinks inside the cert dir -- and bind any
    real location that lives outside the trees already bound above."""
    bound = ("/usr", "/etc", "/opt")
    targets: set[str] = set()

    def consider(path: str) -> None:
        if not os.path.lexists(path):
            return
        real = os.path.realpath(path)
        if not os.path.exists(real):  # broken symlink on the host: nothing to bind
            return
        d = real if os.path.isdir(real) else os.path.dirname(real)
        if not any(d == b or d.startswith(b + "/") for b in bound):
            targets.add(d)

    # Bundle files and the cert dir itself (any of these may be a symlink outward).
    for p in ("/etc/ssl/certs", "/etc/ssl/cert.pem", "/etc/ssl/ca-bundle.pem",
              "/etc/pki/tls/certs/ca-bundle.crt"):
        consider(p)
    # Entries inside the cert dir are commonly per-cert symlinks into an external
    # tree (openSUSE's /var/lib/ca-certificates/pem); follow each and collect it.
    certs_dir = Path("/etc/ssl/certs")
    if certs_dir.is_dir():
        for entry in certs_dir.iterdir():
            if entry.is_symlink():
                consider(str(entry))

    # Drop any target nested under another; binding the ancestor already covers it.
    minimal = [d for d in targets
               if not any(d != o and d.startswith(o + "/") for o in targets)]
    binds: list[str] = []
    for d in sorted(minimal):
        binds += ["--ro-bind", d, d]
    return binds


def _nss_file_with_row(src: str, key: int, row: str) -> Optional[Path]:
    """A temp copy of the colon-separated file `src` with `row` appended -- unless a
    line already carries numeric id `key` in its third field, in which case the id is
    already resolvable from the file the /etc bind carries in and we return None."""
    try:
        existing = Path(src).read_text()
    except OSError:
        existing = ""
    for line in existing.splitlines():
        fields = line.split(":")
        if len(fields) > 2 and fields[2] == str(key):
            return None
    fd, path = tempfile.mkstemp(prefix="claude-isol-nss-")
    with os.fdopen(fd, "w") as fh:
        fh.write(existing)
        if existing and not existing.endswith("\n"):
            fh.write("\n")
        fh.write(row)
    return Path(path)


def _passwd_group_binds() -> list[str]:
    """Make the launching user resolvable inside the --local sandbox.

    Directory accounts (LDAP/AD via SSSD/nss) have no row in /etc/passwd or /etc/group
    -- they're served over an nss socket the sandbox doesn't expose -- so getpwuid()
    inside fails ("cannot find name for user ID ...") and breaks whoami, git, shell
    prompt expansion, etc. Copy the host's files and append the current user's/group's
    row when it's missing, then bind the copies over /etc/{passwd,group}. Placed after
    the /etc ro-bind so they win by bwrap's last-wins ordering."""
    binds: list[str] = []
    try:
        pw = pwd.getpwuid(os.getuid())
    except KeyError:
        pw = None
    if pw is not None:
        row = ":".join((pw.pw_name, "x", str(pw.pw_uid), str(pw.pw_gid),
                        pw.pw_gecos, pw.pw_dir, pw.pw_shell)) + "\n"
        f = _nss_file_with_row("/etc/passwd", pw.pw_uid, row)
        if f is not None:
            binds += ["--ro-bind", str(f), "/etc/passwd"]
    try:
        gr = grp.getgrgid(os.getgid())
    except KeyError:
        gr = None
    if gr is not None:
        row = ":".join((gr.gr_name, "x", str(gr.gr_gid), ",".join(gr.gr_mem))) + "\n"
        f = _nss_file_with_row("/etc/group", gr.gr_gid, row)
        if f is not None:
            binds += ["--ro-bind", str(f), "/etc/group"]
    return binds


def build_bwrap_cmd(cwd: Path, volumes: list[str], inner: list[str],
                    resolv: Optional[Path] = None,
                    dev_binds: Optional[list[str]] = None,
                    home_subdirs: tuple[str, ...] = (),
                    writable_home: bool = False,
                    env_vars: tuple[str, ...] = ()) -> list[str]:
    """Build the `bwrap` argv for --local mode: a host sandbox that exposes only the
    minimal system dirs read-only, a fresh ephemeral HOME with just the Claude config
    bound in, and the cwd as the single read-write tree. `inner` is the command run
    inside the sandbox (e.g. `["claude", ...]` or `["bash"]`).

    The system-dir layout is probed at runtime rather than hardcoded so the same logic
    works on Arch (where /bin /lib /sbin are symlinks into /usr) and on distros where
    they are real directories (some openSUSE layouts).
    """
    cmd = ["bwrap", "--clearenv"]

    # --clearenv, then an allowlist. Without it bwrap forwards our whole environ,
    # which quietly defeats the scoped gh/git credentials: GH_TOKEN, GITHUB_TOKEN,
    # ANTHROPIC_API_KEY, AWS_*, GIT_ASKPASS and friends would walk straight into
    # the sandbox. Anything else the caller actually wants is what -e is for.
    for var in ENV_ALLOWLIST:
        val = os.environ.get(var)
        if val is not None:
            cmd += ["--setenv", var, val]
    for spec in env_vars or ():
        name, _, val = spec.partition("=")
        val = val if "=" in spec else os.environ.get(name)
        if val is not None:
            cmd += ["--setenv", name, val]

    # Read-only system dirs that always live at a fixed path when present.
    for d in ("/usr", "/etc", "/opt", "/usr/local"):
        if Path(d).is_dir():
            cmd += ["--ro-bind", d, d]

    # The usr-merge compatibility entries: recreate symlinks as-is, bind real dirs.
    for d in ("/bin", "/sbin", "/lib", "/lib64"):
        p = Path(d)
        if p.is_symlink():
            cmd += ["--symlink", os.readlink(d), d]
        elif p.is_dir():
            cmd += ["--ro-bind", d, d]

    # CA trust store: bind any trust anchors that live outside the system trees
    # already bound above (see _ca_trust_binds).
    cmd += _ca_trust_binds()

    # Make the launching user (a directory account may be absent from /etc/passwd)
    # resolvable inside the sandbox (see _passwd_group_binds).
    cmd += _passwd_group_binds()

    cmd += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]

    # Fresh ephemeral HOME (writable tmpfs so ~/.cache etc. work), with only the
    # Claude config and the scoped gh/git credentials bound in.
    home = str(HOME)
    cmd += [
        "--tmpfs", home,
        "--bind", f"{HOME}/.claude", f"{home}/.claude",
        # A throwaway copy, not the host file: ~/.claude.json carries mcpServers,
        # which the host's claude would launch on its next run. The session can
        # write this freely; nothing it writes reaches the host.
        "--bind", str(claude_json_copy()), f"{home}/.claude.json",
        "--bind", f"{HOME}/.config/gh-claude", f"{home}/.config/gh",
        "--bind", f"{HOME}/.gitconfig-claude", f"{home}/.gitconfig",
    ]

    # Writable scratch inside the otherwise read-only HOME (see the --remount-ro at
    # the end). A bwrap tmpfs comes up owned by the sandbox user, so unlike podman's
    # it needs no mode of its own.
    for rel in home_subdirs:
        cmd += ["--tmpfs", f"{home}/{rel}"]

    # Make sure the claude binary itself is reachable when it lives outside the
    # system dirs bound above -- an npm-global install under /usr/local, or a
    # native one under HOME (~/.local/...). Two directories, because the one PATH
    # found it in and the one it really lives in differ when the PATH entry is a
    # symlink into a versioned install dir; bind only the target and the symlink
    # itself is gone with the tmpfs, so PATH no longer resolves.
    #
    # After the tmpfs home, and for the same reason as the cwd below: an install
    # under the home dir bound before it is simply shadowed, and the sandbox comes
    # up with no claude in it at all ("bwrap: execvp claude: No such file or
    # directory").
    claude_bin = shutil.which("claude")
    if claude_bin:
        for d in dict.fromkeys((os.path.dirname(claude_bin),
                                os.path.dirname(os.path.realpath(claude_bin)))):
            if not any(d == p or d.startswith(p + "/")
                       for p in ("/usr", "/opt", "/bin", "/sbin")):
                cmd += ["--ro-bind", d, d]

    # Read-only on top of the writable ~/.claude bind: the entries the host's own
    # claude would execute later (see CLAUDE_CONFIG_RO). Last-wins ordering, so
    # these have to come after the bind above.
    for name in CLAUDE_CONFIG_RO:
        src = HOME / ".claude" / name
        if src.exists():
            cmd += ["--ro-bind", str(src), f"{home}/.claude/{name}"]

    # The one read-write tree. Placed after the tmpfs home so a cwd under HOME
    # still wins by bwrap's last-wins ordering.
    cmd += ["--bind", str(cwd), str(cwd)]

    # User -v specs, translated from podman's src:dst[:opts] to bwrap binds.
    # Placed last so the user can override anything bound above.
    for spec in volumes:
        src, dst, opts = parse_volume(spec)
        flag = "--ro-bind" if "ro" in opts.split(",") else "--bind"
        cmd += [flag, src, dst]

    # User --dev-bind specs: like -v but mounted with device access, so device nodes
    # (GPUs at /dev/dri, /dev/bus/usb, ...) actually work -- a plain bind blocks that.
    # bwrap-only. Placed after -v so a later --dev-bind can override an earlier -v.
    for spec in dev_binds or []:
        parts = spec.split(":")
        src, dst = (parts[0], parts[0]) if len(parts) == 1 else (parts[0], parts[1])
        cmd += ["--dev-bind", src, dst]

    # Make DNS resolution work inside the sandbox.
    #
    # /etc/resolv.conf is usually a symlink (systemd-resolved, NetworkManager, ...)
    # into /run. The /etc ro-bind above carries the symlink in, but its target lives
    # under /run, which the sandbox never mounts -- so the symlink dangles and every
    # lookup fails. We therefore bind a real file at the symlink's *resolved* target;
    # the symlink (carried in by the /etc ro-bind) then resolves to it. For a plain
    # /etc/resolv.conf the ro-bind already exposes the real contents, so we only bind
    # when we have an override to apply.
    #
    # The bound file is our public-resolver override under --no-lan (whose resolver
    # must survive the LAN block), otherwise the host's own resolved resolv.conf.
    # Placed after the /etc ro-bind so it wins by bwrap's last-wins ordering.
    etc_resolv = "/etc/resolv.conf"
    if os.path.islink(etc_resolv):
        target = os.path.realpath(etc_resolv)
        src = resolv if resolv is not None else Path(target)
        cmd += ["--ro-bind", str(src), target]
    elif resolv is not None:
        cmd += ["--ro-bind", str(resolv), etc_resolv]

    # HOME read-only, to match container mode, where HOME is the image's root-owned
    # directory and only the config paths bound over it are writable. Everything
    # mounted *into* HOME above -- the config binds, the cwd, --tmpfs-home-subdirectory,
    # a claude install under the home dir -- is a mount of its own and stays writable:
    # --remount-ro is not recursive. It has to come after all of them, since mounting
    # anything needs a writable mountpoint, and it makes the mount itself read-only
    # rather than just the mode bits, so nothing in the sandbox can chmod its way out.
    if not writable_home:
        cmd += ["--remount-ro", home]

    cmd += [
        "--chdir", str(cwd),
        "--unshare-all", "--share-net", "--die-with-parent",
        *inner,
    ]
    return cmd
