#!/bin/bash
# Guest-side setup for --vm, run by the ssh session before claude starts.
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

mountpoint -q /work || mount -t virtiofs cwd /work
mountpoint -q /run/cfg || mount -t virtiofs -o ro cfg /run/cfg

mkdir -p /root/.claude /root/.config/gh
mountpoint -q /root/.claude || mount -t virtiofs claude /root/.claude

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

# --no-lan pins a public resolver: the guest's queries are re-originated by passt
# on the host, where the egress filter drops anything bound for a private
# address -- which the host's own resolvers usually are.
copy_in /run/cfg/resolv.conf /etc/resolv.conf

# Each -v arrives as its own virtiofs tag; where it goes is in this table rather
# than baked into the host side, because the mount point is the guest's business.
if [ -r /run/cfg/mounts ]; then
    while IFS=$'\t' read -r tag dst opts; do
        [ -n "${tag:-}" ] || continue
        mkdir -p "$dst" 2>/dev/null
        mountpoint -q "$dst" && continue
        if [ "$opts" = "ro" ]; then
            mount -t virtiofs -o ro "$tag" "$dst"
        else
            mount -t virtiofs "$tag" "$dst"
        fi
    done < /run/cfg/mounts
fi

exec "$@"
