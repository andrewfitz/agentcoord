# Generated from immutable release artifacts by scripts/build_release.py.
class Agentcoord < Formula
  include Language::Python::Virtualenv
  desc "Native coordination for agents sharing a local Git workspace"
  url "https://github.com/andrewfitz/agentcoord/releases/download/v0.1.15/agentcoord-install.tar.gz"
  version "0.1.15"
  sha256 "b51d47b90a2279e7f10afe822054d416a67b5c4a71c9aea973712a62df0fc084"
  depends_on "python@3.15"

  def install
    python = Formula["python@3.15"].opt_bin/"python3.15"
    system python, "-c", "import sys,sysconfig; actual=f'{sys.implementation.name}-{sys.version_info.major}{sys.version_info.minor}-{sysconfig.get_platform()}'; assert actual == 'cpython-315-macosx-27.0-arm64', 'release target mismatch: '+actual; assert sysconfig.get_config_var('SOABI') == 'cpython-315-darwin', 'release ABI mismatch'"
    virtualenv_create(libexec, "python3.15", system_site_packages: false)
    system python, "-m", "pip", "--python=#{libexec}/bin/python", "install",
           "--no-index", "--only-binary=:all:", "--require-hashes",
           "--find-links=#{buildpath}/wheels", "--requirement=#{buildpath}/requirements.txt"
    bin.install_symlink libexec/"bin/agentcoord"
  end

  test do
    assert_match "agentcoord", shell_output("#{bin}/agentcoord --help")
  end
end
