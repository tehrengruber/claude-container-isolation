class ClaudeIsol < Formula
  include Language::Python::Virtualenv

  desc "Run Claude Code in an isolated podman container or a host sandbox"
  homepage "https://github.com/tehrengruber/claude-container-isolation"
  head "https://github.com/tehrengruber/claude-container-isolation.git", branch: "main"

  depends_on "python@3.13"

  resource "click" do
    url "https://files.pythonhosted.org/packages/c7/0e/7fa0ef50764b67090eca4114772a2abf8b6148198475e54c660b97caeee6/click-8.5.0.tar.gz"
    sha256 "ba0d2089de75ea0310e2dde03160e6ca10009947fb95a182f9b54021bb272e34"
  end

  resource "websockets" do
    url "https://files.pythonhosted.org/packages/01/89/3f825ab71c242fffb62ea8fe638741c290f62f8d7aadf8125ff897747af3/websockets-17.2.tar.gz"
    sha256 "36c2fb94c990cc2545143b12690e2de6c16300f9dbe5b4f33fa300cf57dc8792"
  end

  def install
    virtualenv_install_with_resources
  end

  def caveats
    on_macos do
      <<~EOS
        Container mode (the default) needs podman and a running podman machine:
          brew install podman
          podman machine init && podman machine start

        --local needs nothing more: it uses macOS's own sandbox-exec.
      EOS
    end
  end

  test do
    assert_match "Run Claude Code in isolation", shell_output("#{bin}/claude-isol --help")
    assert_match "not available on", shell_output("#{bin}/claude-isol --vm 2>&1", 2) if OS.mac?
  end
end
