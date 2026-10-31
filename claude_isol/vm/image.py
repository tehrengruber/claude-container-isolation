#!/usr/bin/env python3
"""Turn the container image into a bootable disk for --vm, and cache it.

Three steps, none of which need root on the host:

  podman build   -- Dockerfile.vm layers systemd, openssh and a kernel on top of
                    the container image, so the two modes install the same things
  podman export  -- the image's filesystem as a tar
  mkfs.ext4 -d   -- populate a filesystem image straight from a directory, with
                    no loop mount; run under `podman unshare` so the extracted
                    tree can keep its root-owned files

Everything derived is cached under ~/.cache/claude-isol/vm, keyed by the digest
of the *guest* image -- the thing that actually boots -- so an edit to either
Dockerfile invalidates the disk and the kernel together.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from ..claude_isol import SCRIPT_DIR, image_id

VM_TAG = "claude-isolation-vm:latest"
# Where the linux package puts them. Dockerfile.vm regenerates the initramfs in
# place, without autodetect, so this is the complete one rather than the
# build-container-specific image the pacman hook leaves behind.
GUEST_KERNEL = "/boot/vmlinuz-linux"
GUEST_INITRD = "/boot/initramfs-linux.img"
# Room for pacman, build trees and nested container images; the file is sparse,
# so this costs what is actually written.
DISK_SIZE = os.environ.get("CLAUDE_ISOL_VM_DISK", "40G")


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or f"{Path.home()}/.cache"
    d = Path(base) / "claude-isol" / "vm"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, **kw)


def guest_digest(base_image: str) -> str:
    """Build the guest image if needed and return the digest of what will boot.

    Keyed on the *guest* image rather than the base: everything derived from it
    is cached under this digest, and keying on the base meant an edit to
    Dockerfile.vm produced a new image while the old disk and kernel were
    quietly reused. podman's layer cache makes the rebuild a no-op when nothing
    changed, so this can run every time."""
    _run(["podman", "build", "-f", str(SCRIPT_DIR / "Dockerfile.vm"),
          "--build-arg", f"BASE={base_image}", "-t", VM_TAG, str(SCRIPT_DIR)])
    return image_id(VM_TAG)


def disk_for(digest: str) -> Path:
    """The disk for guest image `digest`, built on first use and cached after."""
    disk = cache_dir() / f"{digest[:16]}.ext4"
    if disk.exists():
        return disk

    print("building the VM disk (first run for this image; minutes)",
          file=sys.stderr)

    work = cache_dir() / f".build-{digest[:16]}"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    tmp_disk = disk.with_name(disk.name + ".partial")
    try:
        cid = subprocess.run(["podman", "create", VM_TAG],
                             capture_output=True, text=True,
                             check=True).stdout.strip()
        try:
            tar = work / "rootfs.tar"
            with tar.open("wb") as fh:
                _run(["podman", "export", cid], stdout=fh)
        finally:
            subprocess.run(["podman", "rm", "-f", cid], capture_output=True)

        root = work / "root"
        root.mkdir()
        # A user namespace so the extracted tree may own root-owned files, and
        # mkfs.ext4 -d so no loop mount (and so no real root) is needed.
        _run(["podman", "unshare", "--", "sh", "-euc",
              f'tar -xpf "{tar}" -C "{root}" && '
              f'mkfs.ext4 -q -F -L claude-isol -d "{root}" "{tmp_disk}" "{DISK_SIZE}"'])
        tmp_disk.replace(disk)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        tmp_disk.unlink(missing_ok=True)
    return disk


def boot_files(digest: str) -> tuple[Path, Path]:
    """Kernel and initramfs for a direct boot, copied out of the guest image once
    and cached beside the disk. Booting the kernel directly keeps a bootloader
    out of the picture entirely."""
    kernel = cache_dir() / f"{digest[:16]}.vmlinuz"
    initrd = cache_dir() / f"{digest[:16]}.initrd"
    if kernel.exists() and initrd.exists():
        return kernel, initrd

    for src, dst in ((GUEST_KERNEL, kernel), (GUEST_INITRD, initrd)):
        # A distinct temp name per artefact: one shared ".partial" let two
        # concurrent first runs write a rootfs into a kernel file.
        tmp = dst.with_name(dst.name + ".partial")
        with tmp.open("wb") as fh:
            _run(["podman", "run", "--rm", VM_TAG, "cat", src], stdout=fh)
        if tmp.stat().st_size == 0:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"{src} is empty in {VM_TAG}")
        tmp.replace(dst)
    return kernel, initrd
