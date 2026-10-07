import hashlib
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "agentcoord_update_homebrew", Path(__file__).resolve().parents[1] / "scripts/update_homebrew.py",
)
homebrew = importlib.util.module_from_spec(spec)
spec.loader.exec_module(homebrew)


def release_assets(root, version="0.1.10", *, url=None, declared_hash=None):
    root.mkdir()
    bundle = root / "agentcoord-install.tar.gz"
    bundle.write_bytes(b"immutable installation bundle")
    bundle_hash = hashlib.sha256(bundle.read_bytes()).hexdigest()
    formula = root / "agentcoord.rb"
    url = url or f"https://github.com/andrewfitz/agentcoord/releases/download/v{version}/agentcoord-install.tar.gz"
    formula.write_text(f'class Agentcoord < Formula\n  url "{url}"\n  version "{version}"\n'
                       f'  sha256 "{declared_hash or bundle_hash}"\nend\n')
    sums = root / "SHA256SUMS"
    sums.write_text(f"{hashlib.sha256(formula.read_bytes()).hexdigest()}  agentcoord.rb\n"
                    f"{bundle_hash}  agentcoord-install.tar.gz\n")
    return root


def promote(assets, destination, tag="v0.1.10"):
    return homebrew.update_formula(assets, destination, tag=tag, repository="andrewfitz/agentcoord")


def test_first_promotion_is_exact_idempotent_and_newer_release_updates(tmp_path):
    destination = tmp_path / "tap/Formula/agentcoord.rb"
    assets = release_assets(tmp_path / "first")
    assert promote(assets, destination)
    assert destination.read_bytes() == (assets / "agentcoord.rb").read_bytes()
    assert not promote(assets, destination)
    newer = release_assets(tmp_path / "next", "0.1.11")
    assert promote(newer, destination, "v0.1.11")
    assert destination.read_bytes() == (newer / "agentcoord.rb").read_bytes()


@pytest.mark.parametrize("name", ["agentcoord.rb", "agentcoord-install.tar.gz"])
def test_tampered_asset_does_not_change_existing_formula(tmp_path, name):
    assets = release_assets(tmp_path / "assets")
    destination = tmp_path / "formula.rb"
    destination.write_bytes(b"preserved formula")
    (assets / name).write_bytes(b"modified after checksum generation")
    with pytest.raises(ValueError, match="checksum mismatch"):
        promote(assets, destination)
    assert destination.read_bytes() == b"preserved formula"


@pytest.mark.parametrize("kwargs", [
    {"url": "file:///private/release/agentcoord-install.tar.gz"},
    {"url": "https://github.com/other/repo/releases/download/v0.1.10/agentcoord-install.tar.gz"},
    {"declared_hash": "f" * 64},
    {"version": "0.1.11"},
])
def test_hashed_but_wrong_release_identity_is_rejected(tmp_path, kwargs):
    assets = release_assets(tmp_path / "assets", **kwargs)
    destination = tmp_path / "formula.rb"
    with pytest.raises(ValueError, match="selected public release"):
        promote(assets, destination)
    assert not destination.exists()


@pytest.mark.parametrize("tag", ["v0.1.11-rc1", "0.1.10", "v01.1.10", "v0.1.10\n"])
def test_nonstable_tags_are_rejected(tmp_path, tag):
    with pytest.raises(ValueError, match="stable|start with v"):
        promote(tmp_path / "unused", tmp_path / "formula.rb", tag)


def test_downgrade_preserves_newer_formula(tmp_path):
    destination = tmp_path / "formula.rb"
    newer = release_assets(tmp_path / "newer", "0.1.11")
    promote(newer, destination, "v0.1.11")
    older = release_assets(tmp_path / "older")
    with pytest.raises(ValueError, match="downgrade"):
        promote(older, destination)
    assert destination.read_bytes() == (newer / "agentcoord.rb").read_bytes()


def test_changed_same_version_and_duplicate_checksums_are_rejected(tmp_path):
    assets = release_assets(tmp_path / "assets")
    destination = tmp_path / "formula.rb"
    promote(assets, destination)
    destination.write_bytes(destination.read_bytes() + b"# altered copy\n")
    preserved = destination.read_bytes()
    with pytest.raises(ValueError, match="immutable formula"):
        promote(assets, destination)
    checksums = assets / "SHA256SUMS"
    checksums.write_text(checksums.read_text() * 2)
    with pytest.raises(ValueError, match="duplicate"):
        promote(assets, destination)
    assert destination.read_bytes() == preserved
