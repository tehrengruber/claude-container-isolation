#!/usr/bin/env python3
"""Turn the container image into a bootable disk for --vm, and cache it.

Three steps, none of which need root on the host:

  podman build   -- Dockerfile.vm layers systemd, openssh and a kernel on top of
                    the container image, so the two modes install the same things
  podman export  -- the image's filesystem as a tar
  mkfs.ext4 -d   -- populate a filesystem image straight from a directory, with
                    no loop mount; run under `podman unshare` so the extracted
                    tree can keep its root-owned files

The result is cached under ~/.cache/claude-isol/vm keyed by the *base* image's
digest, the same trick install_claude_on_top uses for its layered tag: the disk
is rebuilt only when the image it came from actually changed.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from ..claude_isol import SCRIPT_DIR, image_id

VM_TAG = "claude-isolation-vm:latest"
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


def build_vm_image(base_image: str) -> Path:
    """Build the guest image from `base_image` and return the ext4 disk path."""
    _run(["podman", "build", "-f", str(SCRIPT_DIR / "Dockerfile.vm"),
          "--build-arg", f"BASE={base_image}", "-t", VM_TAG, str(SCRIPT_DIR)])
    return VM_TAG  # type: ignore[return-value]


def disk_for(base_image: str) -> Path:
    """The disk image for `base_image`, built on first use and cached after."""
    digest = image_id(base_image)
    disk = cache_dir() / f"{digest[:16]}.ext4"
    if disk.exists():
        return disk

    print(f"building the VM disk for {base_image} (first run; minutes)",
          file=sys.stderr)
    build_vm_image(base_image)

    work = cache_dir() / f".build-{digest[:16]}"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    tmp_disk = disk.with_suffix(".partial")
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


def boot_files(base_image: str) -> tuple[Path, Path]:
    """Kernel and initramfs for a direct boot, extracted from the image once and
    cached beside the disk. Direct kernel boot keeps a bootloader out of the
    picture; the fallback initramfs is the one with the virtio modules in it."""
    digest = image_id(base_image)
    kernel = cache_dir() / f"{digest[:16]}.vmlinuz"
    initrd = cache_dir() / f"{digest[:16]}.initrd"
    if kernel.exists() and initrd.exists():
        return kernel, initrd

    for src, dst in (("/boot/vmlinuz-linux", kernel),
                     ("/boot/initramfs-linux-fallback.img", initrd)):
        tmp = dst.with_suffix(".partial")
        with tmp.open("wb") as fh:
            _run(["podman", "run", "--rm", VM_TAG, "cat", src], stdout=fh)
        tmp.replace(dst)
    return kernel, initrd
