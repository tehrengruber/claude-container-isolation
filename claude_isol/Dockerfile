FROM archlinux:latest

RUN pacman -Syu --noconfirm --needed curl ca-certificates

RUN pacman-key --init \
 && curl -fsSL -o /etc/pacman.d/arch-pre-built.gpg \
        https://github.com/tehrengruber/arch-pre-built-packages/releases/download/latest/arch-pre-built.gpg \
 && pacman-key --add /etc/pacman.d/arch-pre-built.gpg \
 && pacman-key --lsign-key till@ehrengruber.ch

RUN printf '\n[arch-pre-built]\nServer = https://github.com/tehrengruber/arch-pre-built-packages/releases/download/latest\n' \
        >> /etc/pacman.conf

# -Syu, not -Sy: -Sy refreshes the package DB without upgrading what's already
# installed. That partial upgrade leaves the packages installed here linked
# against library versions the image never receives.
RUN pacman -Syu --noconfirm claude-code git github-cli python python310 python311 python312 openmpi base-devel cmake sudo \
 && echo 'ALL ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/00-claude-isol \
 && chmod 0440 /etc/sudoers.d/00-claude-isol

# codebase-memory-mcp: a single static binary, no runtime and no network needed
# once installed. --skip-config because the client config comes from the mounted
# ~/.claude.json or --mcp-config, not from anything baked in here; CBM_CACHE_DIR
# keeps the installer's own bookkeeping out of /root/.cache.
RUN CBM_CACHE_DIR=/tmp/cbm-install curl -fsSL \
        https://raw.githubusercontent.com/DeusData/codebase-memory-mcp/main/install.sh \
      | CBM_CACHE_DIR=/tmp/cbm-install bash -s -- --dir /usr/local/bin --skip-config \
 && rm -rf /tmp/cbm-install \
 && /usr/local/bin/codebase-memory-mcp --version

# Index per invocation, shared with nobody. /tmp is the container's own writable
# layer, so the cache starts empty every run and dies with the container -- which
# is what this server wants: its coordination state (locks, daemon socket) lives
# in /tmp/cbm-daemon-<uid> and is per-container for the same reason, so two
# sessions sharing one cache dir would be two daemons each assuming it is the
# only one. Created here because the container user cannot write under the
# root-owned HOME, where the default ~/.cache/codebase-memory-mcp would land.
RUN mkdir -p /tmp/codebase-memory && chmod 1777 /tmp/codebase-memory
ENV CBM_CACHE_DIR=/tmp/codebase-memory

CMD ["/bin/bash"]