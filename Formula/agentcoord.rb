# Generated from immutable release artifacts by scripts/build_release.py.
class Agentcoord < Formula
  include Language::Python::Virtualenv
  desc "Native coordination for agents sharing a local Git workspace"
  url "https://github.com/andrewfitz/agentcoord/releases/download/v0.1.12/agentcoord-install.tar.gz"
  version "0.1.12"
  sha256 "a3f3381380443d19b7ffa69c69de382a8268963b3a1abb258a8085fe582b9280"
  depends_on "python@3.14"

  def install
    python = Formula["python@3.14"].opt_bin/"python3.14"
    system python, "-c", "import sys,sysconfig; actual=f'{sys.implementation.name}-{sys.version_info.major}{sys.version_info.minor}-{sysconfig.get_platform()}'; assert actual == 'cpython-314-macosx-27.0-arm64', 'release target mismatch: '+actual; assert sysconfig.get_config_var('SOABI') == 'cpython-314-darwin', 'release ABI mismatch'"
    virtualenv_create(libexec, "python3.14", system_site_packages: false)
    system python, "-m", "pip", "--python=#{libexec}/bin/python", "install",
           "--no-index", "--only-binary=:all:", "--require-hashes",
           "--find-links=#{buildpath}/wheels", "--requirement=#{buildpath}/requirements.txt"
    bin.install_symlink libexec/"bin/agentcoord"
  end

  test do
    assert_match "agentcoord", shell_output("#{bin}/agentcoord --help")
  end
end
