import ast
import importlib.util
import io
import json
import sysconfig
import tomllib
import zipfile
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("agentcoord_build_release", Path(__file__).resolve().parents[1] / "scripts/build_release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def test_shipped_sources_parse_on_advertised_minimum_python():
    package = Path(__file__).resolve().parents[1]
    metadata = tomllib.loads((package / "pyproject.toml").read_text())["project"]
    minimum = tuple(int(part) for part in metadata["requires-python"].removeprefix(">=").split("."))
    for source in sorted((package / "src").rglob("*.py")):
        ast.parse(source.read_text(encoding="utf-8"), filename=str(source), feature_version=minimum)


def test_public_readiness_adapter_enqueues_with_retry_key(tmp_path):
    from agentcoord.application import build_service
    from agentcoord.cli import BY_TOOL, invoke
    from agentcoord.config import register_workspace
    from agentcoord.core import Call
    from agentcoord.identity import bind_native

    service = build_service(register_workspace(tmp_path, state_root=tmp_path / "state"))
    context = bind_native(service.store, {"harness": "codex", "native_session_id": "public-readiness"})["context"]

    class Client:
        def call(self, operation, arguments, key):
            return service.execute(context, Call(operation, arguments, key))

    queued = invoke(Client(), BY_TOOL["readiness"], {})
    assert queued["ok"], queued
    finished = service.run_operation(queued["data"]["operation_id"])
    assert finished["state"] == "succeeded", finished


def test_release_lock_keeps_314_and_adds_verified_315_target():
    lock = json.loads((Path(__file__).resolve().parents[1] / "release-lock.json").read_text())
    assert lock["targets"]["cpython-314-macosx-27.0-arm64"]["python_formula"] == "python@3.14"
    target = lock["targets"]["cpython-315-macosx-27.0-arm64"]
    assert target["python_formula"] == "python@3.15"
    assert target["abi"] == "cpython-315-darwin"
    assert next(item for item in target["wheels"] if item["name"] == "pydantic_core")["version"] == "2.50.0"


def test_uncertain_job_recovery_hint_names_implemented_domain_command(tmp_path):
    from agentcoord.application import build_service
    from agentcoord.cli import CATALOG
    from agentcoord.config import register_workspace
    from agentcoord.core import Call
    from agentcoord.identity import bind_native

    service = build_service(register_workspace(tmp_path, state_root=tmp_path / "state"))
    context = bind_native(service.store, {"harness": "codex", "native_session_id": "job-recovery"})["context"]
    with service.store.write() as tx:
        queued = service.enqueue(tx, context, "job.execute", {}, key="uncertain-effect")
        service.finish_operation(tx, queued["operation_id"], "uncertain")
    response = service.execute(context, Call("operation.reconcile", {"operation_id": queued["operation_id"]}, "resolve"))
    assert not response["ok"] and response["error"]["code"] == "RECONCILIATION_REQUIRED", response
    action = response["error"]["next_action"]
    assert action == "job.resolve"
    assert action in service.operations
    assert action in {spec.operation for spec in CATALOG}


def test_formula_installs_only_hash_checked_offline_wheels():
    rendered = release.formula("0.1.0", "file:///releases/abc/agentcoord-0.1.0.tar.gz", "a" * 64,
                               target="cpython-315-macosx-27.0-arm64", abi="cpython-315-darwin")
    assert 'url "file:///releases/abc/agentcoord-0.1.0.tar.gz"' in rendered
    assert 'sha256 "' + "a" * 64 + '"' in rendered
    assert 'depends_on "python@3.15"' in rendered
    assert "virtualenv_create" in rendered
    assert "--no-index" in rendered and "--only-binary=:all:" in rendered and "--require-hashes" in rendered
    assert "release target mismatch" in rendered and "release ABI mismatch" in rendered
    assert all(value not in rendered for value in ("resource ", "rust", "pkgconf", "--no-binary", "build_isolation"))
    assert "latest" not in rendered
    assert ".venv" not in rendered


def test_ruby_formula_literals_cannot_interpolate_release_url():
    literal = release._ruby('https://example.test/#{system("danger")}/source.tar.gz')
    assert "\\#{" in literal
    assert '\\"danger\\"' in literal


def test_machine_local_formula_cannot_be_published_into_package_source(tmp_path):
    package = tmp_path / "package"
    package.mkdir()
    with pytest.raises(RuntimeError, match="outside package source"):
        release.build_release(package, tmp_path / "release", formula_output=package / "Formula/agentcoord.rb")
    assert not (tmp_path / "release").exists()


def test_tested_formula_cannot_be_overwritten(tmp_path):
    package = tmp_path / "package"
    package.mkdir()
    formula = tmp_path / "agentcoord.rb"
    formula.write_text("tested immutable formula")
    with pytest.raises(RuntimeError, match="Formula output must be new"):
        release.build_release(package, tmp_path / "release", formula_output=formula)
    assert formula.read_text() == "tested immutable formula"


def test_source_release_contains_shipped_test_counterparts(tmp_path):
    import shutil
    import subprocess
    import sys
    import tarfile

    package = Path(__file__).resolve().parents[1]
    copied = tmp_path / "package"
    copied.mkdir()
    for name in ("pyproject.toml", "MANIFEST.in", "README.md", "protocol.md", "release-lock.json"):
        shutil.copy2(package / name, copied / name)
    for name in ("src", "scripts", "tests", "benchmarks"):
        shutil.copytree(package / name, copied / name,
                        ignore=shutil.ignore_patterns("*.egg-info", "__pycache__", "*.pyc"))
    output = tmp_path / "dist"
    output.mkdir()
    built = subprocess.run(
        [sys.executable, "-c", ("from setuptools.build_meta import build_sdist; "
                               "import sys; build_sdist(sys.argv[1])"), str(output)],
        cwd=copied, capture_output=True, text=True, timeout=60, check=False,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    with tarfile.open(next(output.glob("*.tar.gz"))) as archive:
        members = {name.split("/", 1)[1] for name in archive.getnames() if "/" in name}
    required = {"protocol.md", "scripts/build_release.py", "benchmarks/workload.py", "release-lock.json"}
    required.update(path.relative_to(package).as_posix() for path in (package / "tests").glob("test_*.py"))
    assert not required - members, sorted(required - members)


def _wheel(path, name="mcp", version="2.2.0", *, requires=None, body="original"):
    info = f"{name}-{version}.dist-info"
    with zipfile.ZipFile(path, "w") as archive:
        metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
        if requires:
            metadata += f"Requires-Dist: {requires}\n"
        archive.writestr(info + "/METADATA", metadata + "\n")
        archive.writestr(info + "/WHEEL", "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        archive.writestr(info + "/RECORD", "")
        archive.writestr(name + "/__init__.py", body)


@pytest.fixture
def binary_lock(tmp_path):
    package = tmp_path / "package"
    package.mkdir()
    wheel = tmp_path / "mcp-2.2.0-py3-none-any.whl"
    _wheel(wheel)
    selection = {"name": "mcp", "version": "2.2.0", "filename": wheel.name,
                 "url": "https://files.pythonhosted.org/packages/official/" + wheel.name, "sha256": release._digest(wheel)}
    lock = {"schema_version": 1, "requirements": ["mcp==2.2.0"],
            "dependencies": [{"name": "mcp", "version": "2.2.0", "url": "https://example.test/mcp.tar.gz", "sha256": "a" * 64}],
            "targets": {release.target_key(): {"abi": sysconfig.get_config_var("SOABI"), "python_formula": "python@3.14", "wheels": [selection]}}}
    (package / "release-lock.json").write_text(json.dumps(lock))
    (package / "pyproject.toml").write_text('[project]\nname="agentcoord"\nversion="0.1.0"\ndependencies=["mcp==2.2.0"]\n')
    return package, lock, wheel


def test_cold_cache_downloads_only_exact_wheel_then_hits_without_network(binary_lock, tmp_path, monkeypatch):
    _, lock, wheel = binary_lock
    requested = []

    def download(url, **kwargs):
        requested.append(url)
        return io.BytesIO(wheel.read_bytes())

    monkeypatch.setattr(release, "urlopen", download)
    paths, first = release.cached_wheels(lock, release.target_key(), tmp_path / "cache")
    assert first["hits"] == 0 and first["downloads"] == 1
    assert requested == [lock["targets"][release.target_key()]["wheels"][0]["url"]]
    monkeypatch.setattr(release, "urlopen", lambda *a, **k: pytest.fail("cache hit used network"))
    repeated, second = release.cached_wheels(lock, release.target_key(), tmp_path / "cache")
    assert repeated == paths and second["key"] == first["key"] and second["hits"] == 1 and second["downloads"] == 0
    paths[0].write_bytes(b"corrupted")
    with pytest.raises(RuntimeError, match="Cached wheel checksum mismatch"):
        release.cached_wheels(lock, release.target_key(), tmp_path / "cache")


def test_bad_download_never_populates_cache(binary_lock, tmp_path, monkeypatch):
    _, lock, _ = binary_lock
    monkeypatch.setattr(release, "urlopen", lambda *a, **k: io.BytesIO(b"bad checksum"))
    with pytest.raises(RuntimeError, match="Downloaded wheel checksum mismatch"):
        release.cached_wheels(lock, release.target_key(), tmp_path / "cache")
    assert not list((tmp_path / "cache").rglob("*.whl"))


@pytest.mark.parametrize("change, message", [
    ("requirements", "Dependency declarations changed"), ("target", "No binary release lock"),
    ("abi", "Python ABI"), ("missing", "exactly one wheel"), ("version", "exactly one wheel"),
    ("wheel", "Incompatible locked wheel"), ("sdist", "Invalid official wheel record"),
])
def test_lock_rejects_stale_or_unusable_inputs(binary_lock, change, message):
    package, lock, _ = binary_lock
    target = lock["targets"][release.target_key()]
    if change == "requirements":
        lock["requirements"] = ["mcp==1.0"]
    elif change == "target":
        lock["targets"] = {}
    elif change == "abi":
        target["abi"] = "other-abi"
    elif change == "missing":
        target["wheels"] = []
    elif change == "version":
        target["wheels"][0]["version"] = "1.0"
    elif change == "wheel":
        target["wheels"][0]["filename"] = "other-2.2.0-py3-none-any.whl"
    elif change == "sdist":
        target["wheels"][0]["url"] = "https://files.pythonhosted.org/mcp.tar.gz"
    (package / "release-lock.json").write_text(json.dumps(lock))
    with pytest.raises(RuntimeError, match=message):
        release.load_lock(package, {"dependencies": ["mcp==2.2.0"]}, python_formula="python@3.14")


@pytest.mark.parametrize("missing", [False, True])
def test_app_updates_reuse_wheels_and_offline_resolver_checks_closure(binary_lock, tmp_path, monkeypatch, missing):
    import tarfile

    package, lock, dependency = binary_lock
    monkeypatch.setattr(release, "urlopen", lambda *a, **k: io.BytesIO(dependency.read_bytes()))
    original_run = release._run
    commands = []
    body = ["original"]

    def run(arguments, *, log):
        commands.append(arguments)
        if arguments[1:3] == ["-m", "build"]:
            assert "--no-isolation" in arguments
            dist = Path(arguments[arguments.index("--outdir") + 1])
            dist.mkdir()
            _wheel(dist / "agentcoord-0.1.0-py3-none-any.whl", "agentcoord", "0.1.0",
                   requires="missing_dependency==1.0" if missing else "mcp==2.2.0", body=body[0])
            with tarfile.open(dist / "agentcoord-0.1.0.tar.gz", "w:gz") as archive:
                archive.add(package / "release-lock.json", arcname="agentcoord-0.1.0/release-lock.json")
        else:
            assert "--no-index" in arguments and "--only-binary=:all:" in arguments and "--require-hashes" in arguments
            original_run(arguments, log=log)

    monkeypatch.setattr(release, "_run", run)
    if missing:
        with pytest.raises(RuntimeError, match="Release command failed"):
            release.build_release(package, tmp_path / "first", formula_output=tmp_path / "first.rb", cache=tmp_path / "cache",
                                  python_formula="python@3.14")
        assert not (tmp_path / "first.rb").exists()
        return
    remote_url = 'https://example.test/immutable/#{system("danger")}/agentcoord-install.tar.gz'
    first = release.build_release(package, tmp_path / "first", formula_output=tmp_path / "first.rb", cache=tmp_path / "cache",
                                  python_formula="python@3.14", installation_url=remote_url, source_url="https://example.test/source/immutable.tar.gz")
    assert first["installation_url"] == remote_url
    assert first["source_url"] == "https://example.test/source/immutable.tar.gz"
    assert first["installation_sha256"] == release._digest(Path(first["installation_archive"]))
    formula = (tmp_path / "first.rb").read_text()
    assert "url " + release._ruby(remote_url) in formula and "\\#{" in formula
    assert first["installation_sha256"] in formula and first["source_sha256"] not in formula
    monkeypatch.setattr(release, "urlopen", lambda *a, **k: pytest.fail("application update redownloaded dependencies"))
    body[0] = "changed application"
    second = release.build_release(package, tmp_path / "second", formula_output=tmp_path / "second.rb", cache=tmp_path / "cache",
                                   python_formula="python@3.14")
    assert first["dependency_cache"]["key"] == second["dependency_cache"]["key"]
    assert second["dependency_cache"]["hits"] == 1 and second["dependency_cache"]["downloads"] == 0
    assert first["installation_sha256"] != second["installation_sha256"]
    with tarfile.open(second["installation_archive"]) as archive:
        names = set(archive.getnames())
        assert names == {"agentcoord-install/release-lock.json", "agentcoord-install/requirements.txt",
                         "agentcoord-install/wheels/mcp-2.2.0-py3-none-any.whl", "agentcoord-install/wheels/agentcoord-0.1.0-py3-none-any.whl"}
        requirements = archive.extractfile("agentcoord-install/requirements.txt").read().decode()
        assert lock["targets"][release.target_key()]["wheels"][0]["sha256"] in requirements
    assert all(command[1:3] in (["-m", "build"], ["-m", "pip"]) for command in commands)
