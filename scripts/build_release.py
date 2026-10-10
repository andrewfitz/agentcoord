"""Build immutable wheel-only releases using the checked-in dependency lock.

Only the application is built. Dependencies are pinned official wheels, with no
source build fallback. The release environment needs build, pip and packaging.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import tomllib
from pathlib import Path
from urllib.request import urlopen

from packaging.requirements import Requirement
from packaging.tags import sys_tags
from packaging.utils import canonicalize_name, parse_wheel_filename


def _ruby(value: str) -> str:
    # Ruby double-quoted literals interpolate #{...}; JSON escaping alone is insufficient.
    return json.dumps(value, ensure_ascii=False).replace("#{", "\\#{")


def formula(version: str, install_url: str, sha256: str, *, target: str, abi: str, python_formula: str = "python@3.15") -> str:
    python = python_formula.replace("@", "")
    guard = ("import sys,sysconfig; "
             "actual=f'{sys.implementation.name}-{sys.version_info.major}{sys.version_info.minor}-{sysconfig.get_platform()}'; "
             f"assert actual == {target!r}, 'release target mismatch: '+actual; "
             f"assert sysconfig.get_config_var('SOABI') == {abi!r}, 'release ABI mismatch'")
    lines = ["# Generated from immutable release artifacts by scripts/build_release.py.",
             "class Agentcoord < Formula", "  include Language::Python::Virtualenv",
             '  desc "Native coordination for agents sharing a local Git workspace"',
             f"  url {_ruby(install_url)}", f"  version {_ruby(version)}", f"  sha256 {_ruby(sha256)}",
             f"  depends_on {_ruby(python_formula)}",
             ""]
    lines.extend(["  def install", f"    python = Formula[{_ruby(python_formula)}].opt_bin/{_ruby(python)}",
                  f"    system python, \"-c\", {_ruby(guard)}",
                  f"    virtualenv_create(libexec, {_ruby(python)}, system_site_packages: false)",
                  '    system python, "-m", "pip", "--python=#{libexec}/bin/python", "install",',
                  '           "--no-index", "--only-binary=:all:", "--require-hashes",',
                  '           "--find-links=#{buildpath}/wheels", "--requirement=#{buildpath}/requirements.txt"',
                  '    bin.install_symlink libexec/"bin/agentcoord"', "  end", "",
                  "  test do", '    assert_match "agentcoord", shell_output("#{bin}/agentcoord --help")', "  end", "end", ""])
    return "\n".join(lines)


def _run(arguments: list[str], *, log: Path) -> None:
    with log.open("wb") as stream:
        result = subprocess.run(arguments, stdout=stream, stderr=subprocess.STDOUT, check=False)
    if result.returncode:
        raise RuntimeError(f"Release command failed (exit {result.returncode}); inspect {log}")


def target_key() -> str:
    return f"{sys.implementation.name}-{sys.version_info.major}{sys.version_info.minor}-{sysconfig.get_platform()}"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def load_lock(package: Path, metadata: dict, *, python_formula: str) -> tuple[dict, str]:
    lock = json.loads((package / "release-lock.json").read_text())
    declared = sorted(str(Requirement(value)) for value in metadata.get("dependencies", []))
    if lock.get("schema_version") != 1 or sorted(str(Requirement(value)) for value in lock["requirements"]) != declared:
        raise RuntimeError("Dependency declarations changed; update the complete release lock")
    target = target_key()
    if target not in lock["targets"] or lock["targets"][target]["python_formula"] != python_formula:
        raise RuntimeError(f"No binary release lock for target {target} with {python_formula}")
    if lock["targets"][target]["abi"] != sysconfig.get_config_var("SOABI"):
        raise RuntimeError("Python ABI does not match the binary release lock")
    dependencies, wheels = lock["dependencies"], lock["targets"][target]["wheels"]
    expected = {canonicalize_name(item["name"]): item["version"] for item in dependencies}
    selected = {canonicalize_name(item["name"]): item["version"] for item in wheels}
    if not expected or len(expected) != len(dependencies) or len(selected) != len(wheels) or selected != expected:
        raise RuntimeError("Release lock must select exactly one wheel for every dependency")
    compatible = set(sys_tags())
    for item in wheels:
        if Path(item["filename"]).name != item["filename"]:
            raise RuntimeError("Locked wheel filename must not contain a directory")
        name, version, _, tags = parse_wheel_filename(item["filename"])
        if str(version) != item["version"] or name != canonicalize_name(item["name"]) or not compatible.intersection(tags):
            raise RuntimeError(f"Incompatible locked wheel: {item['filename']}")
        if not re.fullmatch(r"[a-f0-9]{64}", item["sha256"]) or not item["url"].startswith("https://files.pythonhosted.org/") or not item["url"].endswith("/" + item["filename"]):
            raise RuntimeError(f"Invalid official wheel record: {item['filename']}")
    return lock, target


def cached_wheels(lock: dict, target: str, cache: Path) -> tuple[list[Path], dict]:
    manifest = {"requirements": lock["requirements"], "dependencies": lock["dependencies"], "target": target,
                "selection": lock["targets"][target]}
    encoded = _canonical(manifest)
    fingerprint = hashlib.sha256(encoded).hexdigest()
    directory = cache / fingerprint
    directory.mkdir(parents=True, exist_ok=True)
    manifest_path = directory / "manifest.json"
    if directory.is_symlink() or manifest_path.is_symlink():
        raise RuntimeError("Dependency cache metadata must not be a symlink")
    if manifest_path.exists() and manifest_path.read_bytes() != encoded:
        raise RuntimeError("Dependency cache manifest does not match its content digest")
    paths, hits = [], 0
    for item in manifest["selection"]["wheels"]:
        path = directory / item["filename"]
        if path.is_symlink():
            raise RuntimeError(f"Dependency cache contains a symlink: {path}")
        if path.exists():
            hits += 1
        else:
            with urlopen(item["url"], timeout=60) as response:
                content = response.read()
            if hashlib.sha256(content).hexdigest() != item["sha256"]:
                raise RuntimeError(f"Downloaded wheel checksum mismatch: {item['filename']}")
            fd, temporary = tempfile.mkstemp(prefix=".wheel-", dir=directory)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(content)
                os.replace(temporary, path)
            finally:
                Path(temporary).unlink(missing_ok=True)
        if _digest(path) != item["sha256"]:
            raise RuntimeError(f"Cached wheel checksum mismatch: {item['filename']}")
        paths.append(path)
    manifest_path.write_bytes(encoded)
    return paths, {"key": fingerprint, "target": target, "directory": str(directory), "hits": hits, "downloads": len(paths) - hits}


def _archive(files: dict[str, Path], destination: Path) -> None:
    with (destination.open("xb") as raw,
          gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, filename="") as compressed,
          tarfile.open(fileobj=compressed, mode="w") as archive):
        for name, path in sorted(files.items()):
            content = path.read_bytes()
            member = tarfile.TarInfo("agentcoord-install/" + name)
            member.size, member.mode = len(content), 0o644
            archive.addfile(member, io.BytesIO(content))


def build_release(package: Path, output: Path, *, formula_output: Path, python: str = sys.executable, source_url: str | None = None,
                  python_formula: str = "python@3.15", cache: Path | None = None, installation_url: str | None = None) -> dict:
    package = package.resolve(strict=True)
    output = output.absolute()
    formula_output = formula_output.absolute()
    if formula_output.resolve().is_relative_to(package):
        raise RuntimeError("Machine-local formulas belong in release artifacts or a local tap, outside package source")
    if formula_output.exists():
        raise RuntimeError("Formula output must be new; existing tested formulas are immutable")
    if output.exists():
        raise RuntimeError("Release output must be new; existing tested artifacts are immutable")
    metadata = tomllib.loads((package / "pyproject.toml").read_text())["project"]
    version = metadata["version"]
    if metadata["name"] != "agentcoord":
        raise RuntimeError("Selected package is not agentcoord")
    lock, target = load_lock(package, metadata, python_formula=python_formula)
    cache = cache or Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "agentcoord" / "release-wheels"
    paths, cache_receipt = cached_wheels(lock, target, cache)
    output.mkdir(parents=True, mode=0o700)
    _run([python, "-m", "build", "--no-isolation", "--outdir", str(output / "dist"), str(package)], log=output / "build.log")
    wheels, sources = list((output / "dist").glob("*.whl")), list((output / "dist").glob("*.tar.gz"))
    if len(wheels) != 1 or len(sources) != 1:
        raise RuntimeError("Build did not produce exactly one wheel and one source archive")
    archive_bytes = sources[0].read_bytes()
    digest = hashlib.sha256(archive_bytes).hexdigest()
    immutable = output / "artifacts" / digest / sources[0].name
    immutable.parent.mkdir(parents=True)
    with immutable.open("xb") as stream:
        stream.write(archive_bytes)
    bundle = output / "install-inputs"
    (bundle / "wheels").mkdir(parents=True)
    for path in [*paths, wheels[0]]:
        shutil.copyfile(path, bundle / "wheels" / path.name)
    records = [*lock["targets"][target]["wheels"], {"name": "agentcoord", "version": version, "sha256": _digest(wheels[0])}]
    requirements = bundle / "requirements.txt"
    requirements.write_text("".join(f"{item['name']}=={item['version']} --hash=sha256:{item['sha256']}\n" for item in records))
    shutil.copyfile(package / "release-lock.json", bundle / "release-lock.json")
    report = output / "dependency-resolution.json"
    _run([python, "-m", "pip", "install", "--dry-run", "--ignore-installed", "--no-index", "--only-binary=:all:",
          "--require-hashes", "--find-links", str(bundle / "wheels"), "--requirement", str(requirements), "--report", str(report)],
         log=output / "resolve.log")
    installation = output / "agentcoord-install.tar.gz"
    _archive({str(path.relative_to(bundle)): path for path in bundle.rglob("*") if path.is_file()}, installation)
    install_digest = _digest(installation)
    install_immutable = output / "artifacts" / install_digest / installation.name
    install_immutable.parent.mkdir(parents=True)
    shutil.copyfile(installation, install_immutable)
    source_url = immutable.as_uri() if source_url is None else source_url
    installation_url = install_immutable.as_uri() if installation_url is None else installation_url
    rendered = formula(version, installation_url, install_digest, target=target,
                       abi=lock["targets"][target]["abi"], python_formula=python_formula)
    formula_output.parent.mkdir(parents=True, exist_ok=True)
    with formula_output.open("x", encoding="utf-8") as stream:
        stream.write(rendered)
    receipt = {"version": version, "wheel": str(wheels[0]), "source_archive": str(immutable),
               "source_sha256": digest, "source_url": source_url, "formula": str(formula_output),
               "installation_archive": str(install_immutable), "installation_sha256": install_digest,
               "installation_url": installation_url, "dependencies": lock["dependencies"],
               "dependency_wheels": lock["targets"][target]["wheels"], "dependency_lock_sha256": _digest(package / "release-lock.json"),
               "dependency_cache": cache_receipt, "dependency_binary_origin": "official PyPI wheels", "installed": False, "published": False}
    (output / "release.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    return receipt


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--formula-output", type=Path, required=True)
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--source-url", help="Immutable source archive provenance URL; not the formula install URL")
    parser.add_argument("--installation-url", help="Immutable installation bundle URL for a transferable formula (default: local archive URI)")
    parser.add_argument("--python-formula", default="python@3.15")
    args = parser.parse_args(argv)
    try:
        result = build_release(args.package, args.output, formula_output=args.formula_output, source_url=args.source_url,
                               python_formula=args.python_formula, cache=args.cache, installation_url=args.installation_url)
    except (OSError, ValueError, KeyError, RuntimeError) as error:
        print(f"Release build failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
