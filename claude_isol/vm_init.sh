#!/bin/bash
# Guest-side setup for --vm, run by the ssh session before the real command.
#
# Mounts the virtiofs shares and puts the single config files in place. Mount
# points are created here rather than in the image: /run is a fresh tmpfs by the
# time this runs, so anything the image put under it is gone.
#
# The cfg share is mounted read-only -- it lives on the host's $XDG_RUNTIME_DIR
# tmpfs, and a guest that could write it could exhaust that tmpfs and take the
# user's whole login session down with it. So the single config files are
# *copied* out of it rather than bind-mounted from it: claude needs to write
# ~/.claude.json, and gh rewrites its own config, and neither should be able to
# write anything the host still holds.
#
# ~/.claude is a different matter: it is shared read-write because transcripts
# and token refreshes belong on the host, with the entries the host's own claude
# would later execute pinned read-only on that side (see CLAUDE_CONFIG_RO).
set -u

die() { echo "claude-isol-vm-init: $*" >&2; exit 1; }

mount_tag() {  # mount_tag <virtiofs tag> <mount point> [mount options]
    mkdir -p "$2" || die "cannot create $2"
    mountpoint -q "$2" && return 0
    if [ -n "${3:-}" ]; then
        mount -t virtiofs -o "$3" "$1" "$2" || die "mounting '$1' on $2 failed"
    else
        mount -t virtiofs "$1" "$2" || die "mounting '$1' on $2 failed"
    fi
}

mount_tag cwd /work
mount_tag cfg /run/cfg ro
mount_tag claude /root/.claude

mkdir -p /root/.config/gh

copy_in() {  # copy_in <source in the cfg share> <target>
    [ -e "$1" ] || return 0
    cp -f "$1" "$2" 2>/dev/null || return 0
    chmod u+w "$2" 2>/dev/null || true
}
copy_in /run/cfg/claude.json /root/.claude.json
copy_in /run/cfg/gitconfig /root/.gitconfig
if [ -d /run/cfg/gh ]; then
    cp -rf /run/cfg/gh/. /root/.config/gh/ 2>/dev/null || true
    chmod -R u+w /root/.config/gh 2>/dev/null || true
fi

# The resolver the host staged. /etc/resolv.conf is a symlink into resolved's
# state here, so it is removed first: copying onto it would write through the
# link, into a file resolved owns and rewrites.
if [ -r /run/cfg/resolv.conf ]; then
    rm -f /etc/resolv.conf
    cp -f /run/cfg/resolv.conf /etc/resolv.conf
fi

# Each -v arrives as its own virtiofs tag; where it goes is in this table rather
# than baked into the host side, because the mount point is the guest's business.
if [ -r /run/cfg/mounts ]; then
    while IFS=$'\t' read -r tag dst opts; do
        [ -n "${tag:-}" ] || continue
        if [ "$opts" = "ro" ]; then
            mount_tag "$tag" "$dst" ro
        else
            mount_tag "$tag" "$dst"
        fi
    done < /run/cfg/mounts
fi

exec "$@"
