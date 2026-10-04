import ast
import importlib.util
import tomllib
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


def test_formula_uses_immutable_archive_and_pinned_dependency_resources():
    rendered = release.formula("0.1.0", "file:///releases/abc/agentcoord-0.1.0.tar.gz", "a" * 64,
                               [{"name": "mcp", "url": "https://files.pythonhosted.org/mcp-2.2.0.tar.gz", "sha256": "b" * 64}])
    assert 'url "file:///releases/abc/agentcoord-0.1.0.tar.gz"' in rendered
    assert 'sha256 "' + "a" * 64 + '"' in rendered
    assert 'resource "mcp"' in rendered
    assert 'sha256 "' + "b" * 64 + '"' in rendered
    assert 'depends_on "python@3.14"' in rendered
    assert "virtualenv_install_with_resources" in rendered
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
    for name in ("pyproject.toml", "MANIFEST.in", "README.md", "protocol.md"):
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
    required = {"protocol.md", "scripts/build_release.py", "benchmarks/workload.py"}
    required.update(path.relative_to(package).as_posix() for path in (package / "tests").glob("test_*.py"))
    assert not required - members, sorted(required - members)
