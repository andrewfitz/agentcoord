"""Promote a verified stable release's formula into this repository's tap."""
from __future__ import annotations

import argparse
import hashlib
import re
from pathlib import Path


def _field(formula: str, name: str) -> str:
    values = re.findall(rf'^  {name} "([^"\n]+)"$', formula, re.MULTILINE)
    if len(values) != 1:
        raise ValueError(f"Formula must contain exactly one {name}")
    return values[0]


def _version(value: str) -> tuple[int, ...]:
    if not re.fullmatch(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", value):
        raise ValueError("Only stable major.minor.patch releases may update the tap")
    return tuple(map(int, value.split(".")))


def update_formula(assets: Path, destination: Path, *, tag: str, repository: str) -> bool:
    """Validate release identity and both asset hashes before changing the tap."""
    if not tag.startswith("v"):
        raise ValueError("Release tag must start with v")
    version = tag[1:]
    selected = _version(version)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Repository must be an owner/name pair")
    checksums = {}
    for line in (assets / "SHA256SUMS").read_text().splitlines():
        match = re.fullmatch(r"([a-f0-9]{64})  ([A-Za-z0-9_.-]+)", line)
        if not match or match[2] in checksums:
            raise ValueError("Invalid or duplicate release checksum entry")
        checksums[match[2]] = match[1]
    for name in ("agentcoord.rb", "agentcoord-install.tar.gz"):
        if hashlib.sha256((assets / name).read_bytes()).hexdigest() != checksums.get(name):
            raise ValueError(f"Release checksum mismatch: {name}")
    content = (assets / "agentcoord.rb").read_bytes()
    formula = content.decode("utf-8")
    expected_url = f"https://github.com/{repository}/releases/download/{tag}/agentcoord-install.tar.gz"
    if (_field(formula, "version") != version or _field(formula, "url") != expected_url
            or _field(formula, "sha256") != checksums["agentcoord-install.tar.gz"]):
        raise ValueError("Formula does not match the selected public release")
    if destination.exists():
        current = destination.read_bytes()
        previous = _version(_field(current.decode("utf-8"), "version"))
        if selected < previous:
            raise ValueError("Refusing to downgrade the Homebrew tap")
        if selected == previous:
            if content != current:
                raise ValueError("Refusing to replace an existing version's immutable formula")
            return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--destination", type=Path, default=Path("Formula/agentcoord.rb"))
    args = parser.parse_args()
    changed = update_formula(args.assets, args.destination, tag=args.tag, repository=args.repository)
    print(f"Homebrew formula {'updated' if changed else 'already current'}: {args.tag}")


if __name__ == "__main__":
    main()
