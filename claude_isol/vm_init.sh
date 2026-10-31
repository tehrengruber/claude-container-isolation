#!/bin/bash
# Guest-side setup for --vm, run by the ssh session before claude starts.
#
# Mounts the virtiofs shares and puts the single config files in place. Only
# directories can be shared over virtiofs, so the scoped credentials arrive in
# one `cfg` share and are bind-mounted onto their real paths here -- something
# guest root can do freely, and the reason this runs in the guest rather than
# being arranged on the host.
set -u

mountpoint -q /work || mount -t virtiofs cwd /work
mountpoint -q /run/cfg || mount -t virtiofs cfg /run/cfg

mkdir -p /root/.claude /root/.config/gh
mountpoint -q /root/.claude || mount -t virtiofs claude /root/.claude

bind_file() {  # bind_file <source in the cfg share> <target>
    [ -e "$1" ] || return 0
    [ -e "$2" ] || : > "$2"
    mountpoint -q "$2" || mount --bind "$1" "$2"
}
bind_file /run/cfg/claude.json /root/.claude.json
bind_file /run/cfg/gitconfig /root/.gitconfig
[ -d /run/cfg/gh ] && { mountpoint -q /root/.config/gh || mount --bind /run/cfg/gh /root/.config/gh; }

exec "$@"
