#!/usr/bin/env python3
"""--vm: claude as root in a QEMU virtual machine.

The trade this mode makes is more power inside for more protection outside.
Inside, claude is root in its own kernel: it can run containers in whatever mode
it likes, mount filesystems, load modules. Outside, the host is behind a VM
boundary instead of sharing a kernel with the sandbox.

What the guest can actually touch on the host, which is the part worth being
precise about:

  * the virtiofs shares listed below, and nothing else in the filesystem;
  * the network, through passt -- outbound only. passt is started with
    --no-map-gw, so unlike QEMU's built-in user networking it does not offer
    itself as a route to the host's own loopback services. With --no-lan, passt
    also runs under the same eBPF egress cgroup filter the other two modes use;
  * the vsock port its sshd listens on, which is reachable by any local user who
    can open /dev/vsock but only answers pubkey auth against a per-run key.

Within the shares, two things are enforced on the host side rather than in the
guest, because the guest is root and can remount whatever it likes:

  * the entries of ~/.claude that the *host's* claude would later execute --
    settings.json, agents/, plugins/ and the rest of CLAUDE_CONFIG_RO -- are
    read-only, so a session cannot write itself a hook that runs unsandboxed;
  * the cfg share is read-only outright. It sits on the host's XDG_RUNTIME_DIR
    tmpfs, which a guest able to write it could exhaust.

Both are done by running each virtiofsd inside a bubblewrap mount namespace
that holds only what the daemon needs plus its own share.

Filesystem (see vm_init.sh for the guest half):

    cwd     $PWD            -> /work, the one read-write tree
    claude  ~/.claude       -> /root/.claude, with CLAUDE_CONFIG_RO pinned ro
    cfg     a per-run dir   -> /run/cfg, read-only; the guest copies out of it
    vol<n>  each -v source  -> wherever that -v asked for

virtiofsd maps guest root to the launching user (--translate-uid), so claude is
root in the VM yet everything it writes to a share is owned by you on the host --
the effect --userns=keep-id has in container mode, without a user namespace.

The process shape follows the rest of the tool: fork the helpers with
PR_SET_PDEATHSIG so they die with us, then exec the foreground process in place.
Here that is `ssh`, which is what makes the terminal behave -- window resizes,
signals, job control and the exit status are the ssh protocol's job rather than
ours. The guest needs no sshd configuration: systemd-ssh-generator binds
vsock::22 by itself when it detects a VM, and systemd-ssh-proxy turns
`ssh vsock/<cid>` into a vsock connect. Nothing is sent to that VM until it has
read back a secret from its own cfg share, since a cid cannot be reserved in
advance and another local VM could otherwise be answering.
"""
from __future__ import annotations

import fcntl
import os
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import click

from ..claude_isol import (CLAUDE_CONFIG_RO, DEFAULT_IMAGE, HOME, PUBLIC_DNS,
                           _prompt_index, confirm_cwd_submounts, ensure_image,
                           parse_volume, runtime_dir, set_parent_death_signal,
                           setup_nolan_cgroup, stdin_from_text)
from ..launch import Launch
from ..vm import image as vm_image

DEFAULT_MEMORY = "8G"
GUEST_INIT = "/usr/local/bin/claude-isol-vm-init"
CFG_GUEST_DIR = "/run/cfg"
HANDSHAKE_GUEST_PATH = f"{CFG_GUEST_DIR}/handshake"
# systemd's helper that turns a vsock/<cid> host into an AF_VSOCK connection.
# ssh learns about it from a drop-in in the system ssh_config, which -F /dev/null
# discards, so the path is named here instead.
SSH_PROXY = "/usr/lib/systemd/systemd-ssh-proxy"
# What virtiofsd needs in its own mount namespace, and nothing else: it serves
# only --shared-dir, so anything not bound here is out of reach even if its path
# resolution were tricked.
VFSD_SYSTEM_DIRS = ("/usr", "/etc", "/opt")
BOOT_TIMEOUT = 90.0        # generous: a cold page cache plus systemd
SSH_RETRY_DELAY = 0.3
PROBE_TIMEOUT = 10.0       # one connection attempt, so the loop keeps progressing
SSH_FAILED = 255           # ssh's own exit status: it never reached the guest


def _console_hint() -> str:
    return f"guest console: {vm_image.cache_dir() / 'last-console.log'}"


def _remote_command(*argv: str) -> str:
    """One string for ssh to hand the guest's shell, quoted for it. ssh joins a
    remote command with spaces and the far shell splits it again, so quoting
    here is what keeps a prompt or a path with a space in one piece."""
    return " ".join(shlex.quote(a) for a in argv)


def _run_dir(run_id: str) -> Path:
    """Everything this run creates lives here: the ephemeral ssh key, the staged
    credentials, the virtiofsd sockets. One directory means one thing to remove.

    A lock file in it marks the run as live. The fd is deliberately inheritable,
    so it survives the exec into ssh and the kernel holds the lock for exactly as
    long as the session does -- including through a SIGKILL, which the reaper
    cannot survive. _sweep_stale() reads that rather than guessing from a pid."""
    d = runtime_dir() / f"run-{run_id}"
    d.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(d / "lock", os.O_CREAT | os.O_WRONLY, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.set_inheritable(fd, True)
    return d


def _sweep_stale() -> None:
    """Drop run directories whose session is gone -- a crash, a SIGKILL, or a
    reboot that left the runtime dir behind.

    Liveness is whether the run's lock is still held. Keying it on /proc/<pid>
    instead would be defeated by pid reuse, which on a 32k-pid box takes
    minutes: the sweep would then skip that directory forever and leave a
    private key and credential copies behind until the next reboot."""
    for d in runtime_dir().glob("run-*"):
        lock = d / "lock"
        if lock.exists():
            fd = os.open(lock, os.O_RDONLY)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                continue            # still held: a live session
            finally:
                os.close(fd)        # releases our probe lock with it
        shutil.rmtree(d, ignore_errors=True)


def _spawn_reaper(run_dir: Path) -> int:
    """Fork a child whose only job is to delete `run_dir` once we are gone.

    The launcher exec's into ssh, so it cannot clean up after itself; and the run
    directory holds a private key and copies of the gh credentials, which should
    not outlive the session. PR_SET_PDEATHSIG turns our exit into a SIGTERM here,
    and the handler removes the directory. A SIGKILL of the whole process group
    would skip this, which is what _sweep_stale() covers on the next run."""
    pid = os.fork()
    if pid == 0:
        try:
            set_parent_death_signal(signal.SIGTERM)

            def cleanup(*_):
                shutil.rmtree(run_dir, ignore_errors=True)
                os._exit(0)

            for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
                signal.signal(sig, cleanup)
            # The parent may already have exited between fork and prctl, in which
            # case no signal is coming and we clean up straight away.
            if os.getppid() == 1:
                cleanup()
            while True:
                signal.pause()
        except BaseException:
            shutil.rmtree(run_dir, ignore_errors=True)
        os._exit(0)
    return pid


def _stage_resolv(stage: Path) -> None:
    """Give the guest a resolver it can actually reach.

    passt will not offer one over DHCP that the guest cannot use, and on a host
    running systemd-resolved /etc/resolv.conf names the 127.0.0.53 stub -- host
    loopback, which is exactly what --no-map-gw declines to bridge. So passt
    reports "no IPv4 nameserver available for DHCP", the guest gets none, and
    resolved falls back to shouting LLMNR at a network that has no answer.

    Under --no-lan a public resolver is required anyway: the host's own are
    usually in the private ranges the egress filter drops. Written with the rest
    of the staging, before the share is served; the guest picks it up in
    vm_init.sh."""
    (stage / "resolv.conf").write_text(
        "".join(f"nameserver {ns}\n" for ns in PUBLIC_DNS))


def _enter_nolan_cgroup() -> None:
    """Join the filtered cgroup, which anything forked after this inherits.

    Nothing new is needed for --no-lan here: passt re-originates the guest's
    packets from host sockets, exactly as pasta does for the container, so passt
    and QEMU running under the filter puts every connection the guest makes
    under it too. Unlike podman -- which asks the user systemd manager for a
    fresh scope, and had to be pinned to the cgroupfs manager to stop it
    escaping -- a plain fork simply stays in our cgroup."""
    run_cg = setup_nolan_cgroup()
    (run_cg / "cgroup.procs").write_text(str(os.getpid()))


def _require(*binaries: str) -> None:
    missing = [b for b in binaries if shutil.which(b) is None]
    if missing:
        raise click.UsageError(
            "--vm needs " + ", ".join(missing) + " on PATH "
            "(pacman -S qemu-base virtiofsd openssh)")
    if not os.access("/dev/kvm", os.R_OK | os.W_OK):
        raise click.UsageError(
            "--vm needs access to /dev/kvm (join the 'kvm' group); software "
            "emulation is far too slow to be useful here")
    if not Path(SSH_PROXY).exists():
        raise click.UsageError(
            f"--vm needs {SSH_PROXY} to reach the guest over vsock "
            "(systemd 256 or newer)")


def _pick_cid() -> int:
    """A context id to ask QEMU for. There is no way to reserve one up front: a
    host-side AF_VSOCK bind is only permitted for a *local* CID, so probing a
    guest CID tells us nothing. QEMU is the authority -- it fails to start if the
    cid is taken -- which is why _wait_for_guest() insists on proof that the VM
    answering is ours before anything is sent to it."""
    return 3 + secrets.randbelow(0xFFFFFF00)


def _spawn(argv: list[str], ready: callable = None,
           env: dict[str, str] = None) -> int:
    """Fork a helper that dies with us, optionally waiting for it to be ready."""
    pid = os.fork()
    if pid == 0:
        try:
            set_parent_death_signal(signal.SIGTERM)
            if env:
                os.environ.update(env)
            os.execvp(argv[0], argv)
        except BaseException as e:  # noqa: BLE001
            os.write(2, f"{argv[0]} exec failed: {e!r}\n".encode())
        os._exit(127)
    if ready is not None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if ready():
                return pid
            if os.waitpid(pid, os.WNOHANG)[0]:
                raise click.UsageError(f"{argv[0]} exited before it was ready")
            time.sleep(0.05)
        raise click.UsageError(f"{argv[0]} did not become ready")
    return pid


def _spawn_virtiofsd(tag: str, shared: Path, run_dir: Path,
                     read_only: tuple[str, ...] = (),
                     writable: bool = True) -> Path:
    """One daemon per share, run inside a bubblewrap mount namespace.

    Two things come from that namespace. It is where `read_only` is enforced:
    the guest runs as root and can remount anything it likes, so a read-only
    mount *inside* the VM would be advisory -- the only place a restriction
    holds is on this side, where virtiofsd physically gets EROFS. And it is the
    daemon's containment: only the system dirs it needs to run plus the share
    itself are present, so a path-resolution slip has nothing else to reach.

    --translate-uid is what lets the guest run as root while its writes land on
    the host as the launching user. No --xattr: forwarding xattrs unmapped lets
    guest root set POSIX ACLs on host files and hand them to another local user,
    and nothing here needs them.
    """
    sock = run_dir / f"vfs-{tag}.sock"
    sock.unlink(missing_ok=True)

    inner = [
        shutil.which("virtiofsd") or "/usr/lib/virtiofsd",
        f"--socket-path={sock}", "--shared-dir", str(shared),
        "--sandbox", "none", "--cache", "auto",
        f"--translate-uid=map:0:{os.getuid()}:1",
        f"--translate-gid=map:0:{os.getgid()}:1",
    ]

    wrap = ["bwrap", "--unshare-all", "--share-net", "--die-with-parent",
            "--proc", "/proc", "--dev", "/dev"]
    for d in VFSD_SYSTEM_DIRS:
        if Path(d).is_dir():
            wrap += ["--ro-bind", d, d]
    for d in ("/bin", "/sbin", "/lib", "/lib64"):
        pd = Path(d)
        if pd.is_symlink():
            wrap += ["--symlink", os.readlink(d), d]
        elif pd.is_dir():
            wrap += ["--ro-bind", d, d]
    wrap += ["--bind", str(run_dir), str(run_dir)]      # for the socket
    wrap += ["--ro-bind" if not writable else "--bind", str(shared), str(shared)]
    for name in read_only:                              # last-wins, so after
        src = shared / name
        if src.exists():
            wrap += ["--ro-bind", str(src), str(src)]
    _spawn([*wrap, "--", *inner], ready=sock.is_socket)
    return sock


def _stage_volumes(volumes: list[str], stage: Path) -> list[tuple[str, Path, bool]]:
    """Check each -v and tell the guest where to mount it.

    virtiofs shares directories, so a file source is refused rather than quietly
    widened: sharing its parent would hand the guest every sibling, which is
    emphatically not what `-v ~/.ssh/config:/x` asks for. The mount points are
    the guest's business, so they travel as a table in the cfg share -- written
    here, before that share is served (see vm_init.sh for the other half)."""
    plan: list[tuple[str, Path, bool]] = []
    table: list[str] = []
    for i, vol in enumerate(volumes):
        src, dst, opts = parse_volume(vol)
        path = Path(src).resolve()
        if not path.is_dir():
            raise click.UsageError(
                f"-v {vol}: --vm can only share directories, and {src} is not "
                "one (virtiofs has no way to share a single file; share its "
                "directory, or use --prompt-file for a prompt)")
        ro = "ro" in opts.split(",")
        tag = f"vol{i}"
        plan.append((tag, path, ro))
        table.append(f"{tag}\t{dst}\t{'ro' if ro else 'rw'}")
    if table:
        (stage / "mounts").write_text("\n".join(table) + "\n")
    return plan


def _config_share(run_dir: Path) -> Path:
    """A per-run directory holding what the guest needs bind-mounted onto single
    paths: virtiofs can share directories but not individual files."""
    stage = run_dir / "cfg"
    stage.mkdir(mode=0o700, exist_ok=True)
    gh = HOME / ".config" / "gh-claude"
    gh.mkdir(parents=True, exist_ok=True)
    gitconfig = HOME / ".gitconfig-claude"
    gitconfig.touch(exist_ok=True)
    # Symlinks would dangle in the guest, so the small files are copied in and
    # the directory is shared as-is by its own virtiofs tag.
    shutil.copy(HOME / ".claude.json", stage / "claude.json")
    shutil.copy(gitconfig, stage / "gitconfig")
    (stage / "gh").mkdir(exist_ok=True)
    for f in gh.iterdir():
        if f.is_file():
            shutil.copy(f, stage / "gh" / f.name)
    return stage


def _ssh_key(run_dir: Path) -> tuple[Path, str]:
    """An ephemeral keypair for this run. The public half travels on QEMU's
    command line, which every user on the machine can read -- fine for a public
    key, and the reason nothing secret goes through that channel."""
    key = run_dir / "vm-key"
    key.unlink(missing_ok=True)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C",
                    "claude-isol", "-f", str(key)], check=True)
    # ssh-keygen appends ".pub" literally; with_suffix would replace whatever it
    # took for an extension in the run id instead of adding to it.
    return key, Path(f"{key}.pub").read_text().strip()


def _spawn_passt(run_dir: Path) -> Path:
    """The guest's way out. passt runs unprivileged, re-originates the guest's
    packets from this process tree -- which is what puts them under the --no-lan
    cgroup filter -- and with --no-map-gw does not offer itself as a route to
    the host's own services."""
    sock = run_dir / "net.sock"
    sock.unlink(missing_ok=True)
    _spawn(["passt", "--foreground", "--no-map-gw", "--no-ndp", "--no-dhcpv6",
            "--socket", str(sock)], ready=sock.exists)
    return sock


def _qemu_argv(spec: Launch, disk: Path, kernel: Path, initrd: Path,
               cid: int, pubkey: str, socks: dict[str, Path],
               net_sock: Path) -> list[str]:
    mem = spec.vm_memory or DEFAULT_MEMORY
    cpus = spec.vm_cpus or max(2, (os.cpu_count() or 2) // 2)
    argv = [
        "qemu-system-x86_64", "-enable-kvm", "-cpu", "host",
        "-machine", "q35", "-smp", str(cpus), "-m", mem,
        "-nographic", "-no-reboot",
        # virtiofs needs the guest's RAM to be shareable with the daemons.
        "-object", f"memory-backend-memfd,id=mem,size={mem},share=on",
        "-numa", "node,memdev=mem",
        "-kernel", str(kernel), "-initrd", str(initrd),
        # systemd.firstboot=off: the rootfs comes from `podman export` and so has
        # no /etc/machine-id, which makes systemd treat every boot as the first
        # one and run systemd-firstboot interactively -- it sits on this console
        # asking for a timezone, before sysinit.target, and sshd.socket never
        # starts. There is nobody to answer it.
        "-append", "root=/dev/vda rw console=ttyS0 systemd.firstboot=off "
                   "systemd.show_status=0 loglevel=3",
        # -snapshot is what makes the guest disk ephemeral *and* safe to share:
        # the cached image is opened read-only and every write goes to a temporary
        # overlay, so concurrent VMs cannot corrupt each other (or the cache), and
        # a dirty filesystem never survives into the next run. QEMU unlinks that
        # overlay as soon as it creates it, so it disappears even on SIGKILL --
        # which is why nothing here has to clean it up. TMPDIR points it at the
        # image cache because the host's /tmp is often a tmpfs, and a build inside
        # the VM would otherwise land in RAM.
        "-snapshot",
        "-drive", f"file={disk},if=virtio,format=raw,cache=unsafe",
        "-device", f"vhost-vsock-pci,guest-cid={cid}",
        # passt, not slirp: slirp maps the host at 10.0.2.2 and completes guest
        # connections against the host's own loopback, so every service bound to
        # 127.0.0.1 that trusts localhost would be reachable from the VM --
        # looser than container mode, where that is what --tcp-forward is for.
        # passt --no-map-gw declines to be that bridge. It is also the process
        # that re-originates the guest's packets, which is what lets --no-lan
        # work here at all (see below).
        "-netdev", f"stream,id=net0,addr.type=unix,addr.path={net_sock}",
        "-device", "virtio-net-pci,netdev=net0",
        # The guest's ssh key, delivered as a systemd credential.
        "-smbios",
        f"type=11,value=io.systemd.credential:ssh.ephemeral-authorized_keys-all={pubkey}",
        # The guest's console goes to a file in the image cache, not to our
        # terminal (ssh owns that) and not to /dev/null. When a boot fails there
        # is otherwise nothing to look at: the session just never answers.
        "-serial", f"file:{vm_image.cache_dir() / 'last-console.log'}",
        "-monitor", "none",
    ]
    for tag, sock in socks.items():
        argv += ["-chardev", f"socket,id=vfs-{tag},path={sock}",
                 "-device", f"vhost-user-fs-pci,chardev=vfs-{tag},tag={tag}"]
    return argv


def _wait_for_guest(key: Path, cid: int, token: str, qemu_pid: int) -> None:
    """Wait for the guest to answer, and prove it is the one we started.

    The guest's identity is not checked by ssh here (a fresh VM has no known
    host key), so instead the guest must read back a secret we placed in its
    `cfg` share, which only our VM has. Without this, any local process able to
    create a VM could squat the cid and receive the session -- the prompt, the
    remote command line, and everything typed afterwards.

    Only one thing is worth waiting for: a guest that has not finished booting,
    which shows up as ssh failing to connect at all (exit 255, connection
    refused or reset). Everything else is decided -- qemu gone, our key refused,
    the guest answering but unable to produce the token -- and is reported the
    moment it happens rather than after the full timeout.

    The probe runs the guest init, not a bare `cat`: the token lives in the cfg
    share, and nothing has mounted that until the init does."""
    deadline = time.monotonic() + BOOT_TIMEOUT
    probe = _ssh_argv(key, cid) + [
        "--", _remote_command(GUEST_INIT, "cat", HANDSHAKE_GUEST_PATH)]
    last = ""
    while time.monotonic() < deadline:
        try:
            gone = bool(os.waitpid(qemu_pid, os.WNOHANG)[0])
        except ChildProcessError:
            gone = True          # already reaped; either way it is not running
        if gone:
            raise click.UsageError(
                f"qemu exited before the VM came up (cid {cid} already in use?); "
                f"{_console_hint()}")
        try:
            # A probe that blocks forever would stop the loop ever reaching the
            # deadline again, turning a failure into an indefinite hang -- which
            # is what a vsock connect to a guest that never answers does.
            r = subprocess.run(probe, capture_output=True, text=True,
                               timeout=PROBE_TIMEOUT)
        except subprocess.TimeoutExpired:
            last = f"ssh did not return within {PROBE_TIMEOUT:g}s"
            continue

        if r.returncode == 0:
            if r.stdout.strip() == token:
                return
            raise click.UsageError(
                f"the VM answering on vsock/{cid} is not the one we started; "
                "refusing to hand it the session")

        err = (r.stderr or r.stdout).strip()
        tail = err.splitlines()[-1] if err else f"ssh exited {r.returncode}"
        if r.returncode != SSH_FAILED:
            # ssh itself worked, so the guest is up and waiting changes nothing:
            # the init or the token failed, and that is a real error now.
            raise click.UsageError(
                f"the guest is up but could not hand back its token: {tail}")
        if "Permission denied" in err or "publickey" in err:
            raise click.UsageError(
                "the guest refused our key -- the authorized-key credential did "
                f"not reach it: {tail}")
        last = tail or last
        time.sleep(SSH_RETRY_DELAY)
    raise click.UsageError(
        f"the VM did not answer on vsock/{cid} within {BOOT_TIMEOUT:g}s; "
        f"{_console_hint()}" + (f"\nlast ssh error: {last}" if last else ""))


def _ssh_argv(key: Path, cid: int) -> list[str]:
    return [
        # -F /dev/null because ssh takes the first value it obtains for each
        # option and reads the per-user file before the system one: a `Host *`
        # stanza in ~/.ssh/config -- a ProxyJump through a corporate bastion,
        # say -- wins over the vsock drop-in and sends this connection somewhere
        # it cannot possibly work. That is a plausible configuration rather than
        # an attack; no mode shares ~/.ssh with a session.
        #
        # Starting from no config means supplying the two options the drop-in
        # would have: they are the only thing that teaches ssh what a vsock/<cid>
        # host is, and without them it treats it as a hostname and tries DNS.
        "ssh", "-q", "-F", "/dev/null", "-i", str(key),
        "-o", f"ProxyCommand={SSH_PROXY} %h %p", "-o", "ProxyUseFdpass=yes",
        "-o", "CheckHostIP=no",
        # The "host" is a fresh VM every time, so a known_hosts entry would be
        # noise at best; the transport is a private vsock, not a network.
        "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR", "-o", "IdentitiesOnly=yes",
        # No agent or X11 forwarding: the VM must not reach the host's keys.
        "-o", "ForwardAgent=no", "-o", "ForwardX11=no",
        f"root@vsock/{cid}",
    ]


def launch(spec: Launch) -> None:
    _require("qemu-system-x86_64", "ssh", "ssh-keygen", "passt", "bwrap")
    if shutil.which("virtiofsd") is None and not Path("/usr/lib/virtiofsd").exists():
        raise click.UsageError("--vm needs virtiofsd (pacman -S virtiofsd)")

    confirm_cwd_submounts(spec.cwd, spec.mount_cwd_recursively)

    base = spec.image or DEFAULT_IMAGE
    if spec.image is None:
        ensure_image(base)
    # One digest for the guest image, shared by the disk and the boot files, so
    # they cannot come from different builds.
    digest = vm_image.guest_digest(base)
    disk = vm_image.disk_for(digest)
    kernel, initrd = vm_image.boot_files(digest)

    _sweep_stale()
    run_id = f"{os.getpid()}-{secrets.token_hex(3)}"
    run_dir = _run_dir(run_id)
    _spawn_reaper(run_dir)
    stage = _config_share(run_dir)
    token = secrets.token_hex(16)
    (stage / "handshake").write_text(token)
    _stage_resolv(stage)
    volume_plan = _stage_volumes(spec.volumes, stage)

    prompt = None
    if spec.drop_shell:
        inner = ["bash", "-l"]
    elif spec.exec_cmd:
        inner = list(spec.args)
    else:
        # The prompt never travels as an argument here. The remote command is a
        # string the guest's shell re-parses, so a prompt containing `;` or
        # `$(...)` would run as root in the guest before claude ever started --
        # and an argument is world-readable in the host's ssh argv besides. Print
        # mode gets it on stdin; anything else goes through the cfg share and is
        # referenced as @path.
        args = list(spec.args)
        src = spec.prompt_file
        if src is None:
            idx = _prompt_index(args)
            if idx is not None:
                # An inline prompt: stage it so it travels by reference too.
                src = stage / "prompt.md"
                src.write_text(args[idx])
                args = args[:idx] + args[idx + 1:]
        if src is not None:
            text = src.read_text()
            if not text.strip():
                raise click.UsageError(f"--prompt-file: {src} is empty")
            if any(a in ("-p", "--print") for a in args) and sys.stdin.isatty():
                prompt = text                      # print mode: stdin, verbatim
            else:
                name = f"prompt{src.suffix or '.md'}"
                if src.parent != stage:
                    shutil.copy(src, stage / name)
                args.append(f"@{CFG_GUEST_DIR}/{name}")
        inner = ["claude", *args]

    socks = {
        # ~/.claude has to be writable for transcripts and token refreshes, but
        # the entries the host's own claude would later execute are pinned
        # read-only here, where the guest cannot remount them (see finding 1).
        "claude": _spawn_virtiofsd("claude", HOME / ".claude", run_dir,
                                   read_only=CLAUDE_CONFIG_RO),
        "cwd": _spawn_virtiofsd("cwd", spec.cwd, run_dir),
        # Read-only, and not because the guest has no business writing it: the
        # staging dir sits on the host's XDG_RUNTIME_DIR tmpfs, so a guest that
        # could write it could fill that tmpfs and take down the user's login
        # session. The guest copies what it needs out instead (vm_init.sh).
        "cfg": _spawn_virtiofsd("cfg", stage, run_dir, writable=False),
    }
    for tag, path, ro in volume_plan:
        socks[tag] = _spawn_virtiofsd(tag, path, run_dir, writable=not ro)

    key, pubkey = _ssh_key(run_dir)
    cid = _pick_cid()

    # Before passt and QEMU are forked, so both inherit the filtered cgroup.
    if spec.no_lan:
        _enter_nolan_cgroup()
    net_sock = _spawn_passt(run_dir)

    qemu_pid = _spawn(_qemu_argv(spec, disk, kernel, initrd, cid, pubkey, socks,
                                 net_sock),
                      env={"TMPDIR": str(vm_image.cache_dir())})
    _wait_for_guest(key, cid, token, qemu_pid)

    if prompt is not None:
        stdin_from_text(prompt)

    # exec in place: ssh owns the terminal from here, and when it exits our
    # death takes QEMU and the three virtiofsd with it (PR_SET_PDEATHSIG).
    argv = _ssh_argv(key, cid)
    if not spec.drop_shell and sys.stdin.isatty() and prompt is None:
        argv.append("-t")
    # ssh joins the remote command with spaces and the guest's shell splits it
    # again, so every element is quoted for that shell.
    os.execvp("ssh", [*argv, "--", _remote_command(GUEST_INIT, *inner)])
