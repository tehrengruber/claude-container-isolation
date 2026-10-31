#!/usr/bin/env python3
"""--vm: claude as root in a QEMU virtual machine.

The point of this mode is that the session has *more* power inside and the host
has *more* protection outside: claude is root in its own kernel, free to run
containers in whatever mode it likes, mount things and load modules, while the
only ways back to the host are the three virtiofs shares and nothing else.

The shape follows the rest of the tool: fork the helpers with PR_SET_PDEATHSIG
so they die with us, then exec the foreground process in place. Here that
foreground process is `ssh`, which is what makes the terminal behave -- window
resizes, signals, job control and the exit status are the ssh protocol's job
rather than ours. The guest needs no sshd configuration for this:
systemd-ssh-generator binds vsock::22 on its own when it detects a VM, and
systemd-ssh-proxy on the host side turns `ssh vsock/<cid>` into a vsock connect.

Filesystem (see vm_init.sh for the guest half):

    cwd     $PWD            -> /work, the one read-write tree
    claude  ~/.claude       -> /root/.claude
    cfg     a per-run dir   -> /run/cfg, whose single files are bind-mounted
                               into place inside the guest

virtiofsd maps guest root to the launching user (--translate-uid), so claude is
root in the VM yet everything it writes to a share is owned by you on the host.
"""
from __future__ import annotations

import os
import random
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import click

from ..claude_isol import (DEFAULT_IMAGE, HOME, confirm_cwd_submounts,
                           ensure_image, resolve_prompt, set_parent_death_signal,
                           stdin_from_text)
from ..launch import Launch
from ..vm import image as vm_image

DEFAULT_MEMORY = "8G"
GUEST_INIT = "/usr/local/bin/claude-isol-vm-init"
BOOT_TIMEOUT = 90.0        # generous: a cold page cache plus systemd
SSH_RETRY_DELAY = 0.3


def _runtime_dir() -> Path:
    rd = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    d = Path(rd) / "claude-isol"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _run_dir(run_id: str) -> Path:
    """Everything this run creates lives here: the ephemeral ssh key, the staged
    credentials, the virtiofsd sockets. One directory means one thing to remove."""
    d = _runtime_dir() / f"run-{run_id}"
    d.mkdir(mode=0o700, exist_ok=True)
    return d


def _sweep_stale() -> None:
    """Drop run directories whose session is gone -- a crash, a SIGKILL, or a
    reboot that left the runtime dir behind. The run id starts with the launcher's
    pid, so a live session is easy to tell from a dead one."""
    for d in _runtime_dir().glob("run-*"):
        pid = d.name.removeprefix("run-").split("-")[0]
        if pid.isdigit() and Path(f"/proc/{pid}").exists():
            continue
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


def _free_cid() -> int:
    """A context id no live VM is using. 0-2 are reserved; a collision only shows
    up when QEMU starts, so probe by binding the address ourselves first."""
    for _ in range(64):
        cid = random.randint(3, 0xFFFFFFF0)
        try:
            s = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
        except (AttributeError, OSError):
            return cid  # no AF_VSOCK on this host to probe with; let QEMU judge
        with s:
            try:
                s.bind((cid, 22))
            except OSError:
                continue
        return cid
    raise click.UsageError("could not find a free vsock CID")


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


def _spawn_virtiofsd(tag: str, shared: Path, run_dir: Path) -> Path:
    """One daemon per share. --translate-uid is what lets the guest run as root
    while its writes land on the host as the launching user; --sandbox none keeps
    the daemon unprivileged, which is all it needs for a directory we own."""
    sock = run_dir / f"vfs-{tag}.sock"
    sock.unlink(missing_ok=True)
    _spawn([
        shutil.which("virtiofsd") or "/usr/lib/virtiofsd",
        f"--socket-path={sock}", "--shared-dir", str(shared),
        "--sandbox", "none", "--xattr", "--cache", "auto",
        f"--translate-uid=map:0:{os.getuid()}:1",
        f"--translate-gid=map:0:{os.getgid()}:1",
    ], ready=sock.is_socket)
    return sock


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


def _qemu_argv(spec: Launch, disk: Path, kernel: Path, initrd: Path,
               cid: int, pubkey: str, socks: dict[str, Path]) -> list[str]:
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
        "-append", "root=/dev/vda rw console=ttyS0 systemd.show_status=0 loglevel=3",
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
        # User-mode networking: outbound works, nothing listens on the host, and
        # no privileged setup is needed.
        "-netdev", "user,id=net0", "-device", "virtio-net-pci,netdev=net0",
        # The guest's ssh key, delivered as a systemd credential.
        "-smbios",
        f"type=11,value=io.systemd.credential:ssh.ephemeral-authorized_keys-all={pubkey}",
        "-serial", "null", "-monitor", "none",
    ]
    for tag, sock in socks.items():
        argv += ["-chardev", f"socket,id=vfs-{tag},path={sock}",
                 "-device", f"vhost-user-fs-pci,chardev=vfs-{tag},tag={tag}"]
    return argv


def _wait_for_ssh(key: Path, cid: int) -> None:
    """Poll until the guest's vsock ssh socket answers, or give up."""
    deadline = time.monotonic() + BOOT_TIMEOUT
    probe = _ssh_argv(key, cid) + ["--", "true"]
    while time.monotonic() < deadline:
        if subprocess.run(probe, capture_output=True).returncode == 0:
            return
        time.sleep(SSH_RETRY_DELAY)
    raise click.UsageError(
        f"the VM did not answer on vsock/{cid} within {BOOT_TIMEOUT:g}s")


def _ssh_argv(key: Path, cid: int) -> list[str]:
    return [
        "ssh", "-q", "-i", str(key),
        # The "host" is a fresh VM every time, so a known_hosts entry would be
        # noise at best; the transport is a private vsock, not a network.
        "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR", "-o", "IdentitiesOnly=yes",
        # No agent or X11 forwarding: the VM must not reach the host's keys.
        "-o", "ForwardAgent=no", "-o", "ForwardX11=no",
        f"root@vsock/{cid}",
    ]


def launch(spec: Launch) -> None:
    _require("qemu-system-x86_64", "ssh", "ssh-keygen")
    if shutil.which("virtiofsd") is None and not Path("/usr/lib/virtiofsd").exists():
        raise click.UsageError("--vm needs virtiofsd (pacman -S virtiofsd)")

    confirm_cwd_submounts(spec.cwd, spec.mount_cwd_recursively)

    base = spec.image or DEFAULT_IMAGE
    if spec.image is None:
        ensure_image(base)
    disk = vm_image.disk_for(base)
    kernel, initrd = vm_image.boot_files(base)

    _sweep_stale()
    run_id = f"{os.getpid()}-{random.randint(0, 0xFFFF):04x}"
    run_dir = _run_dir(run_id)
    _spawn_reaper(run_dir)
    prompt = None
    if spec.drop_shell:
        inner = ["bash", "-l"]
    elif spec.exec_cmd:
        inner = list(spec.args)
    else:
        dst = (f"/run/cfg/prompt{spec.prompt_file.suffix}"
               if spec.prompt_file else "")
        args, prompt = resolve_prompt(spec, dst)
        inner = ["claude", *args]

    stage = _config_share(run_dir)
    if spec.prompt_file is not None:
        shutil.copy(spec.prompt_file, stage / f"prompt{spec.prompt_file.suffix}")

    socks = {
        "cwd": _spawn_virtiofsd("cwd", spec.cwd, run_dir),
        "claude": _spawn_virtiofsd("claude", HOME / ".claude", run_dir),
        "cfg": _spawn_virtiofsd("cfg", stage, run_dir),
    }

    key, pubkey = _ssh_key(run_dir)
    cid = _free_cid()
    _spawn(_qemu_argv(spec, disk, kernel, initrd, cid, pubkey, socks),
           env={"TMPDIR": str(vm_image.cache_dir())})
    _wait_for_ssh(key, cid)

    if prompt is not None:
        stdin_from_text(prompt)

    # exec in place: ssh owns the terminal from here, and when it exits our
    # death takes QEMU and the three virtiofsd with it (PR_SET_PDEATHSIG).
    argv = _ssh_argv(key, cid)
    if not spec.drop_shell and sys.stdin.isatty() and prompt is None:
        argv.append("-t")
    os.execvp("ssh", [*argv, "--", GUEST_INIT, *inner])
