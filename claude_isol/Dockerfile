# Built for the host's own architecture. BASE is the official archlinux image
# on amd64; on arm64, where that image does not exist, the launcher builds
# Dockerfile.archlinuxarm first and passes it in.
ARG BASE=docker.io/library/archlinux:latest
FROM $BASE

RUN pacman -Syu --noconfirm --needed curl ca-certificates

# The arch-pre-built repository ships x86_64 packages only (claude-code and the
# extra Python versions among them), so other architectures skip it.
RUN if [ "$(uname -m)" = x86_64 ]; then \
        pacman-key --init \
     && curl -fsSL -o /etc/pacman.d/arch-pre-built.gpg \
            https://github.com/tehrengruber/arch-pre-built-packages/releases/download/latest/arch-pre-built.gpg \
     && pacman-key --add /etc/pacman.d/arch-pre-built.gpg \
     && pacman-key --lsign-key till@ehrengruber.ch \
     && printf '\n[arch-pre-built]\nServer = https://github.com/tehrengruber/arch-pre-built-packages/releases/download/latest\n' \
            >> /etc/pacman.conf; \
    fi

# -Syu, not -Sy: -Sy refreshes the package DB without upgrading what's already
# installed. That partial upgrade leaves the packages installed here linked
# against library versions the image never receives.
RUN pacman -Syu --noconfirm git github-cli python openmpi base-devel cmake sudo \
 && if [ "$(uname -m)" = x86_64 ]; then \
        pacman -S --noconfirm claude-code python310 python311 python312; \
    else \
        pacman -S --noconfirm nodejs npm \
     && npm install -g @anthropic-ai/claude-code; \
    fi \
 && echo 'ALL ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/00-claude-isol \
 && chmod 0440 /etc/sudoers.d/00-claude-isol

CMD ["/bin/bash"]
