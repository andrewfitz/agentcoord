"""Build immutable local artifacts and a Homebrew virtualenv resource formula.

Run from a release environment containing build and pip. Nothing is installed or
published by this script. A formula can use the local immutable archive URI or an
explicit immutable source URL supplied by the release owner.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tomllib
from pathlib import Path
from urllib.request import urlopen


def _ruby(value: str) -> str:
    # Ruby double-quoted literals interpolate #{...}; JSON escaping alone is insufficient.
    return json.dumps(value, ensure_ascii=False).replace("#{", "\\#{")


def formula(version: str, source_url: str, sha256: str, dependencies: list[dict], *, python_formula: str = "python@3.14") -> str:
    lines = ["# Generated from immutable release artifacts by scripts/build_release.py.",
             "class Agentcoord < Formula", "  include Language::Python::Virtualenv",
             '  desc "Native coordination for agents sharing a local Git workspace"',
             f"  url {_ruby(source_url)}", f"  version {_ruby(version)}", f"  sha256 {_ruby(sha256)}",
             f"  depends_on {_ruby(python_formula)}",
             '  depends_on "rust" => :build', '  depends_on "pkgconf" => :build',
             '  depends_on "openssl@3"', '  depends_on "libffi"', ""]
    for dependency in sorted(dependencies, key=lambda item: item["name"].lower()):
        lines.extend([f"  resource {_ruby(dependency['name'])} do",
                      f"    url {_ruby(dependency['url'])}", f"    sha256 {_ruby(dependency['sha256'])}", "  end", ""])
    lines.extend(["  def install", f"    virtualenv_install_with_resources(using: {_ruby(python_formula)}, system_site_packages: false)", "  end", "",
                  "  test do", '    assert_match "agentcoord", shell_output("#{bin}/agentcoord --help")', "  end", "end", ""])
    return "\n".join(lines)


def _run(arguments: list[str], *, log: Path) -> None:
    with log.open("wb") as stream:
        result = subprocess.run(arguments, stdout=stream, stderr=subprocess.STDOUT, check=False)
    if result.returncode:
        raise RuntimeError(f"Release command failed (exit {result.returncode}); inspect {log}")


def _sdist(name: str, version: str) -> dict:
    with urlopen(f"https://pypi.org/pypi/{name}/{version}/json", timeout=30) as response:
        document = json.load(response)
    candidates = [item for item in document["urls"] if item["packagetype"] == "sdist"]
    if len(candidates) != 1:
        raise RuntimeError(f"Expected one immutable sdist for {name}=={version}; found {len(candidates)}")
    artifact = candidates[0]
    return {"name": name, "version": version, "url": artifact["url"], "sha256": artifact["digests"]["sha256"]}


def build_release(package: Path, output: Path, *, formula_output: Path, python: str = sys.executable, source_url: str | None = None,
                  python_formula: str = "python@3.14") -> dict:
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
    output.mkdir(parents=True, mode=0o700)
    _run([python, "-m", "build", "--outdir", str(output / "dist"), str(package)], log=output / "build.log")
    wheels, sources = list((output / "dist").glob("*.whl")), list((output / "dist").glob("*.tar.gz"))
    if len(wheels) != 1 or len(sources) != 1:
        raise RuntimeError("Build did not produce exactly one wheel and one source archive")
    archive_bytes = sources[0].read_bytes()
    digest = hashlib.sha256(archive_bytes).hexdigest()
    immutable = output / "artifacts" / digest / sources[0].name
    immutable.parent.mkdir(parents=True)
    with immutable.open("xb") as stream:
        stream.write(archive_bytes)
    report = output / "dependency-resolution.json"
    _run([python, "-m", "pip", "install", "--dry-run", "--ignore-installed", "--report", str(report), str(wheels[0])], log=output / "resolve.log")
    resolution = json.loads(report.read_text())
    dependencies = []
    for candidate in resolution["install"]:
        name, resolved_version = candidate["metadata"]["name"], candidate["metadata"]["version"]
        if name.lower().replace("_", "-") == "agentcoord":
            continue
        dependencies.append(_sdist(name, resolved_version))
    source_url = immutable.as_uri() if source_url is None else source_url
    rendered = formula(version, source_url, digest, dependencies, python_formula=python_formula)
    formula_output.parent.mkdir(parents=True, exist_ok=True)
    with formula_output.open("x", encoding="utf-8") as stream:
        stream.write(rendered)
    receipt = {"version": version, "wheel": str(wheels[0]), "source_archive": str(immutable),
               "source_sha256": digest, "source_url": source_url, "formula": str(formula_output),
               "dependencies": dependencies, "installed": False, "published": False}
    (output / "release.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    return receipt


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--formula-output", type=Path, required=True)
    parser.add_argument("--source-url")
    parser.add_argument("--python-formula", default="python@3.14")
    args = parser.parse_args(argv)
    try:
        result = build_release(args.package, args.output, formula_output=args.formula_output, source_url=args.source_url, python_formula=args.python_formula)
    except (OSError, ValueError, KeyError, RuntimeError) as error:
        print(f"Release build failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
