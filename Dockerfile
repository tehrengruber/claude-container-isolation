FROM archlinux:latest

RUN pacman -Syu --noconfirm --needed curl ca-certificates

RUN pacman-key --init \
 && curl -fsSL -o /etc/pacman.d/arch-pre-built.gpg \
        https://github.com/tehrengruber/arch-pre-built-packages/releases/download/latest/arch-pre-built.gpg \
 && pacman-key --add /etc/pacman.d/arch-pre-built.gpg \
 && pacman-key --lsign-key till@ehrengruber.ch

RUN printf '\n[arch-pre-built]\nServer = https://github.com/tehrengruber/arch-pre-built-packages/releases/download/latest\n' \
        >> /etc/pacman.conf

RUN pacman -Sy --noconfirm claude-code git github-cli python openmpi base-devel cmake gcc13 gcc14 sudo \
 && echo 'ALL ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/00-claude-isol \
 && chmod 0440 /etc/sudoers.d/00-claude-isol

CMD ["/bin/bash"]