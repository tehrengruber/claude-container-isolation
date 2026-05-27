# Maintainer:

pkgname=claude-isol
pkgver=0.1.0
pkgrel=1
pkgdesc="Run Claude Code inside an isolated podman container with an MCP-filtering proxy"
arch=('any')
license=('custom')
depends=('podman' 'python' 'python-websockets')
source=('claude-isol.py'
        'mcp-proxy.py'
        'Dockerfile')
sha256sums=('SKIP'
            'SKIP'
            'SKIP')

package() {
    install -Dm755 "$srcdir/claude-isol.py" "$pkgdir/usr/share/$pkgname/claude-isol"
    install -Dm755 "$srcdir/mcp-proxy.py"   "$pkgdir/usr/share/$pkgname/mcp-proxy.py"
    install -Dm644 "$srcdir/Dockerfile"     "$pkgdir/usr/share/$pkgname/Dockerfile"
    install -dm755 "$pkgdir/usr/bin"
    ln -s "/usr/share/$pkgname/claude-isol" "$pkgdir/usr/bin/$pkgname"
}