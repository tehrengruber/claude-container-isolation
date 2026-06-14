# Maintainer:

pkgname=claude-isol
pkgver=0.1.0
pkgrel=1
pkgdesc="Run Claude Code inside an isolated podman container with an MCP-filtering proxy"
arch=('any')
license=('custom')
depends=('podman' 'python' 'python-websockets' 'python-click' 'glib2' 'libbpf')
optdepends=('bubblewrap: for --local host-sandbox mode (no container)')
makedepends=('python-build' 'python-installer' 'python-setuptools' 'python-wheel'
             'clang' 'libbpf')
install="$pkgname.install"
source=()
sha256sums=()

build() {
    cd "$startdir"
    rm -rf build dist *.egg-info
    python -m build --wheel --no-isolation
    # --no-lan egress filter + its loader (clang for the BPF object, cc for the loader).
    # Build into the (gitignored, wiped-above) build/ tree so the source stays clean.
    make -C claude_isol/bpf O="$startdir/build/bpf"
}

package() {
    cd "$startdir"
    python -m installer --destdir="$pkgdir" dist/*.whl
    install -Dm644 claude_isol/claude-isol-notifyd.service \
        "$pkgdir/usr/lib/systemd/user/claude-isol-notifyd.service"

    # --no-lan helper: the loader looks for the object beside itself, so they must
    # share a directory. Caps are set post-install (fakeroot can't persist them).
    install -Dm755 build/bpf/lan_block_load \
        "$pkgdir/usr/lib/claude-isol/lan_block_load"
    install -Dm644 build/bpf/lan_block.bpf.o \
        "$pkgdir/usr/lib/claude-isol/lan_block.bpf.o"
}
