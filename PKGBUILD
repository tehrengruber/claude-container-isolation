# Maintainer:

pkgname=claude-isol
pkgver=0.1.0
pkgrel=1
pkgdesc="Run Claude Code inside an isolated podman container with an MCP-filtering proxy"
arch=('any')
license=('custom')
depends=('podman' 'python' 'python-websockets')
makedepends=('python-build' 'python-installer' 'python-setuptools' 'python-wheel')
source=()
sha256sums=()

build() {
    cd "$startdir"
    rm -rf build dist *.egg-info
    python -m build --wheel --no-isolation
}

package() {
    cd "$startdir"
    python -m installer --destdir="$pkgdir" dist/*.whl
}
