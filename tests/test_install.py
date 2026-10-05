from __future__ import annotations

import json
import os
import plistlib
import shlex
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agentcoord import install


@pytest.fixture
def layout(tmp_path, monkeypatch):
    root, home = tmp_path / "workspace", tmp_path / "home"
    root.mkdir()
    home.mkdir()
    executable = tmp_path / "agentcoord"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    (tmp_path / "python3").symlink_to(sys.executable)
    state_root = home / "selected-private-state"
    from agentcoord.config import register_workspace
    monkeypatch.setenv("AGENTCOORD_STATE_HOME", str(state_root))
    workspace = register_workspace(root)
    return workspace, home, str(executable)


class Launchctl:
    def __init__(self, loaded=False, running=True, fail=None):
        self.loaded, self.running, self.fail = loaded, running, fail
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        operation = command[1]
        if operation == self.fail:
            return subprocess.CompletedProcess(command, 5, "", "failed")
        if operation == "print":
            return subprocess.CompletedProcess(command, 0 if self.loaded else 113, "state = running\n" if self.running else "state = waiting\n", "")
        if operation == "bootout":
            self.loaded = False
        if operation == "bootstrap":
            self.loaded, self.running = True, True
        return subprocess.CompletedProcess(command, 0, "", "")


class Maintenance:
    def __init__(self, workspace_id, *, uncertain=0, running=0):
        self.workspace_id, self.uncertain, self.running = workspace_id, uncertain, running
        self.drained = False
        self.activated = False

    def health(self):
        return {"ok": True, "data": {"workspace_id": self.workspace_id, "protocol": 1, "release": "0.1.0", "database_state": "ready", "maintenance_error": None, "service_state": "quiescent" if self.drained else "active", "running_effects": self.running, "uncertain_effects": self.uncertain}}

    def drain(self, *, key):
        self.drained = True
        return {"ok": True, "data": {}}

    def activate(self, *, key):
        self.activated, self.drained = True, False
        return {"ok": True, "data": {}}

    def close(self):
        pass


def test_check_init_creates_nothing_and_apply_preserves_existing_instructions(layout):
    workspace, _, _ = layout
    root = workspace.root
    original = "# Team instructions\n\nUse our tests.\n"
    (root / "AGENTS.md").write_text(original)
    report = install.init_project(root)
    assert not report["applied"]
    assert not (root / ".agentcoord.toml").exists()
    install.init_project(root, apply=True)
    content = (root / "AGENTS.md").read_text()
    assert content.startswith(original)
    assert content.count(install.BEGIN_MARKER) == 1
    first = {name: (root / name).read_bytes() for name in ("AGENTS.md", "CLAUDE.md", ".agentcoord.toml")}
    install.init_project(root, apply=True)
    assert first == {name: (root / name).read_bytes() for name in first}
    from agentcoord.config import load_config
    assert load_config(workspace).version is None


def test_existing_project_configuration_is_preserved(layout):
    workspace, _, _ = layout
    config = workspace.root / ".agentcoord.toml"
    config.write_text('[custom]\nsetting = "existing"\n')
    original = config.read_bytes()
    install.init_project(workspace.root, apply=True)
    assert config.read_bytes() == original


@pytest.mark.parametrize("text", [install.BEGIN_MARKER, install.END_MARKER, install.END_MARKER + install.BEGIN_MARKER, (install.BEGIN_MARKER + install.END_MARKER) * 2])
def test_ambiguous_instruction_markers_fail_before_any_write(layout, text):
    workspace, _, _ = layout
    (workspace.root / "AGENTS.md").write_text(text)
    with pytest.raises(install.InstallError):
        install.init_project(workspace.root, apply=True)
    assert not (workspace.root / ".agentcoord.toml").exists()


def test_symlink_instruction_target_preserved(layout, tmp_path):
    workspace, _, _ = layout
    foreign = tmp_path / "other.md"
    foreign.write_text("owned by someone else")
    (workspace.root / "AGENTS.md").symlink_to(foreign)
    with pytest.raises(install.InstallError, match="Symlink"):
        install.init_project(workspace.root, apply=True)
    assert foreign.read_text() == "owned by someone else"


def test_candidates_include_four_native_configs_but_activate_nothing(layout, tmp_path):
    workspace, _, executable = layout
    destination = tmp_path / "candidates"
    report = install.generate_candidates(workspace.root, destination, executable=executable)
    assert report["activated"] is False
    assert list(workspace.root.iterdir()) == []
    for harness in install.HARNESSES:
        suffix = "toml" if harness in {"codex", "grok"} else "json"
        file = destination / f"{harness}-mcp.{suffix}"
        value = tomllib.loads(file.read_text()) if suffix == "toml" else json.loads(file.read_text())
        entry = value["mcp_servers" if suffix == "toml" else "mcpServers"]["agentcoord"]
        assert entry["command"] == executable
        assert entry["args"] == ["--project", str(workspace.root), "mcp", "--harness", harness]
        hooks = json.loads((destination / f"{harness}-hooks.json").read_text())["hooks"]
        assert set(hooks) == set(install.EVENTS[harness])
        assert not any("Tool" in name or "Shell" in name for name in hooks)
    assert "@EXECUTABLE@" not in (destination / "herdr.sh").read_text()
    assert "@PROJECT@" not in (destination / "herdr.sh").read_text()
    with pytest.raises(install.InstallError, match="already exists"):
        install.generate_candidates(workspace.root, destination)


def test_candidates_cannot_overwrite_live_harness_directory(layout):
    workspace, _, _ = layout
    for target in (workspace.root, workspace.root / ".claude/candidate"):
        with pytest.raises(install.InstallError, match="separate"):
            install.generate_candidates(workspace.root, target)


@pytest.mark.parametrize("harness", install.HARNESSES)
def test_native_mcp_candidate_rediscovers_exact_private_routing(tmp_path, monkeypatch, harness, socket_directory):
    from agentcoord.config import register_workspace

    root = tmp_path / "selected workspace"
    root.mkdir()
    private = tmp_path / "selected private state"
    monkeypatch.setenv("AGENTCOORD_STATE_HOME", str(private))
    monkeypatch.setenv("AGENTCOORD_SOCKET_HOME", str(socket_directory))
    workspace = register_workspace(root)
    destination = tmp_path / "candidates"
    install.generate_candidates(root, destination)
    suffix = "toml" if harness in {"codex", "grok"} else "json"
    candidate = destination / f"{harness}-mcp.{suffix}"
    parsed = tomllib.loads(candidate.read_text()) if suffix == "toml" else json.loads(candidate.read_text())
    entry = parsed["mcp_servers" if suffix == "toml" else "mcpServers"]["agentcoord"]
    native_env = {key: value for key, value in os.environ.items()
                  if key not in {"AGENTCOORD_STATE_HOME", "AGENTCOORD_SOCKET_HOME", "AGENTCOORD_WORKSPACE"}}
    native_env.update(entry.get("env", {}))
    probe = subprocess.run([sys.executable, "-c", ("from agentcoord.config import discover_workspace; "
        "import json,sys; w=discover_workspace(explicit_root=sys.argv[1]); "
        "print(json.dumps([str(w.state_root),str(w.socket_path)]))"), str(root)],
        env=native_env, capture_output=True, text=True, timeout=10, check=False)
    assert probe.returncode == 0, probe.stderr
    assert json.loads(probe.stdout) == [str(workspace.state_root), str(workspace.socket_path)]


@pytest.fixture
def socket_directory():
    with tempfile.TemporaryDirectory(prefix="ac-route-", dir="/tmp") as directory:
        yield Path(directory).resolve()


@pytest.mark.parametrize("consumer", [*install.HARNESSES, "herdr"])
def test_generated_native_launchers_retain_private_routing(tmp_path, monkeypatch, socket_directory, consumer):
    from agentcoord.config import register_workspace

    root = tmp_path / "workspace with spaces"
    root.mkdir()
    monkeypatch.setenv("AGENTCOORD_STATE_HOME", str(tmp_path / "private state"))
    monkeypatch.setenv("AGENTCOORD_SOCKET_HOME", str(socket_directory))
    workspace = register_workspace(root)
    executable = tmp_path / "agentcoord"
    executable.write_text(f"#!{sys.executable}\nfrom agentcoord.config import discover_workspace\n"
                          "import json,sys\nfrom pathlib import Path\n"
                          "w=discover_workspace(explicit_root=Path(sys.argv[2]))\n"
                          "print(json.dumps([str(w.state_root),str(w.socket_path)]))\n")
    executable.chmod(0o700)
    (tmp_path / "python3").symlink_to(sys.executable)
    destination = tmp_path / "candidates"
    install.generate_candidates(root, destination, executable=str(executable))
    if consumer == "herdr":
        command = ["/bin/sh", str(destination / "herdr.sh")]
    else:
        hooks = json.loads((destination / f"{consumer}-hooks.json").read_text())["hooks"]
        groups = next(iter(hooks.values()))
        handler = groups[0] if consumer == "cursor" else groups[0]["hooks"][0]
        command = shlex.split(handler["command"])
    native_env = {key: value for key, value in os.environ.items()
                  if key not in {"AGENTCOORD_STATE_HOME", "AGENTCOORD_SOCKET_HOME", "AGENTCOORD_WORKSPACE"}}
    if consumer == "herdr":
        native_env.update(HERDR_WORKSPACE_ID="selected-workspace",
                          HERDR_PLUGIN_CONTEXT_JSON=json.dumps({"workspace_id": "selected-workspace", "workspace_cwd": str(root)}))
    probe = subprocess.run(command, env=native_env, capture_output=True, text=True, timeout=10, check=False)
    assert probe.returncode == 0, probe.stderr
    assert json.loads(probe.stdout) == [str(workspace.state_root), str(workspace.socket_path)]


def test_herdr_action_opens_declared_monitor_pane_with_exact_workspace(layout, tmp_path):
    workspace, _, executable = layout
    destination = tmp_path / "herdr candidates"
    install.generate_candidates(workspace.root, destination, executable=executable)
    manifest = tomllib.loads((destination / "herdr-plugin.toml").read_text())
    fake_herdr = tmp_path / "herdr binary"
    fake_herdr.write_text(f"#!{sys.executable}\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n")
    fake_herdr.chmod(0o700)
    action = manifest["actions"][0]
    pane = manifest["panes"][0]
    env = dict(os.environ, HERDR_BIN_PATH=str(fake_herdr), HERDR_WORKSPACE_ID="exact-herdr-workspace",
               HERDR_PLUGIN_CONTEXT_JSON=json.dumps({"workspace_id": "exact-herdr-workspace", "workspace_cwd": str(workspace.root)}))
    result = subprocess.run(action["command"], cwd=destination, env=env, text=True, capture_output=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["plugin", "pane", "open", "--plugin", manifest["id"],
                                        "--entrypoint", pane["id"], "--placement", pane["placement"],
                                        "--workspace", "exact-herdr-workspace", "--cwd", str(workspace.root), "--focus"]


def test_herdr_monitor_uses_plugin_root_from_selected_project_cwd(layout, tmp_path):
    workspace, _, _ = layout
    executable = tmp_path / "agentcoord with spaces"
    executable.write_text(f"#!{sys.executable}\nimport json,os,sys\nprint(json.dumps({{'argv':sys.argv[1:],'cwd':os.getcwd(),'project':os.environ['AGENTCOORD_WORKSPACE']}}))\n")
    executable.chmod(0o700)
    destination = tmp_path / "herdr candidates"
    install.generate_candidates(workspace.root, destination, executable=str(executable))
    manifest = tomllib.loads((destination / "herdr-plugin.toml").read_text())
    env = dict(os.environ, HERDR_PLUGIN_ROOT=str(destination), HERDR_WORKSPACE_ID="selected-workspace",
               HERDR_PLUGIN_CONTEXT_JSON=json.dumps({"workspace_id": "selected-workspace", "workspace_cwd": str(workspace.root)}))
    result = subprocess.run(manifest["panes"][0]["command"], cwd=workspace.root, env=env,
                            text=True, capture_output=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"argv": ["--project", str(workspace.root), "monitor"],
                                      "cwd": str(workspace.root), "project": str(workspace.root)}
    env.pop("HERDR_PLUGIN_ROOT")
    missing = subprocess.run(manifest["panes"][0]["command"], cwd=workspace.root, env=env,
                             text=True, capture_output=True, timeout=10, check=False)
    assert missing.returncode != 0
    assert "HERDR_PLUGIN_ROOT is required" in missing.stderr
    assert not missing.stdout


def test_herdr_action_missing_workspace_fails_before_any_launch(layout, tmp_path):
    workspace, _, executable = layout
    destination = tmp_path / "herdr candidates"
    install.generate_candidates(workspace.root, destination, executable=executable)
    env = {key: value for key, value in os.environ.items() if key != "HERDR_WORKSPACE_ID"}
    env["HERDR_BIN_PATH"] = "/bin/echo"
    env["HERDR_PLUGIN_CONTEXT_JSON"] = json.dumps({"workspace_id": "different", "workspace_cwd": str(workspace.root)})
    result = subprocess.run(["/bin/sh", str(destination / "herdr.sh"), "open"], env=env,
                            text=True, capture_output=True, timeout=10, check=False)
    assert result.returncode != 0
    assert not result.stdout
    assert "workspace" in result.stderr


@pytest.mark.parametrize("mode", ["open", "monitor"])
def test_generic_herdr_plugin_routes_two_registered_workspaces_without_pinned_project(layout, tmp_path, mode):
    from agentcoord.config import register_workspace
    first, _, _ = layout
    second_root = tmp_path / "other repo OBrien $(unevaluated)"
    second_root.mkdir()
    second = register_workspace(second_root)
    executable = tmp_path / "agentcoord"
    executable.write_text(f"#!{sys.executable}\nfrom agentcoord.config import discover_workspace\n"
                          "from pathlib import Path\nimport json,os,sys\n"
                          "workspace=discover_workspace(explicit_root=Path(sys.argv[2]))\n"
                          "print(json.dumps([workspace.id,os.environ['AGENTCOORD_WORKSPACE'],sys.argv[1:]]))\n")
    herdr = tmp_path / "herdr fixture"
    herdr.write_text(f"#!{sys.executable}\nimport json,os,sys\n"
                     "print(json.dumps([os.environ['AGENTCOORD_WORKSPACE'],sys.argv[1:]]))\n")
    herdr.chmod(0o700)
    destination = tmp_path / "generic plugin"
    install.generate_candidates(first.root, destination, executable=str(executable))
    for number, workspace in enumerate((first, second)):
        context_id = f"herdr-{number}"
        env = dict(os.environ, HERDR_BIN_PATH=str(herdr), HERDR_WORKSPACE_ID=context_id,
                   HERDR_PLUGIN_CONTEXT_JSON=json.dumps({"workspace_id": context_id, "workspace_cwd": str(workspace.root)}),
                   AGENTCOORD_WORKSPACE=str(first.root))
        result = subprocess.run(["/bin/sh", str(destination / "herdr.sh"), mode], env=env,
                                text=True, capture_output=True, timeout=10, check=False)
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        if mode == "monitor":
            assert data == [workspace.id, str(workspace.root), ["--project", str(workspace.root), "monitor"]]
        else:
            assert data == [str(workspace.root), ["plugin", "pane", "open", "--plugin", "agentcoord",
                                                "--entrypoint", "monitor", "--placement", "tab", "--workspace",
                                                context_id, "--cwd", str(workspace.root), "--focus"]]


@pytest.mark.parametrize("context", ["not JSON", "[]", '{"workspace_id":"wrong","workspace_cwd":"/"}',
                                    '{"workspace_id":"selected","workspace_cwd":"relative"}',
                                    '{"workspace_id":"selected","workspace_id":"selected","workspace_cwd":"/"}'])
def test_herdr_context_failure_never_launches_a_pane_or_monitor(layout, tmp_path, context):
    workspace, _, executable = layout
    destination = tmp_path / "generic plugin"
    install.generate_candidates(workspace.root, destination, executable=executable)
    env = dict(os.environ, HERDR_BIN_PATH="/bin/echo", HERDR_WORKSPACE_ID="selected", HERDR_PLUGIN_CONTEXT_JSON=context)
    for mode in ("open", "monitor"):
        result = subprocess.run(["/bin/sh", str(destination / "herdr.sh"), mode], env=env,
                                text=True, capture_output=True, timeout=10, check=False)
        assert result.returncode == 2
        assert not result.stdout


def test_herdr_custom_bin_without_sibling_python_uses_validated_path_runtime(layout, tmp_path, monkeypatch):
    workspace, _, _ = layout
    custom = tmp_path / "custom bin"
    custom.mkdir()
    executable = custom / "agentcoord"
    executable.write_text(f"#!{sys.executable}\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n")
    executable.chmod(0o700)
    assert not (custom / "python3").exists()
    monkeypatch.setattr(install.shutil, "which", lambda _: sys.executable)
    destination = tmp_path / "generic plugin"
    install.generate_candidates(workspace.root, destination, executable=str(executable))
    env = dict(os.environ, HERDR_WORKSPACE_ID="selected",
               HERDR_PLUGIN_CONTEXT_JSON=json.dumps({"workspace_id": "selected", "workspace_cwd": str(workspace.root)}))
    result = subprocess.run(["/bin/sh", str(destination / "herdr.sh"), "monitor"], env=env,
                            text=True, capture_output=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["--project", str(workspace.root), "monitor"]


def test_herdr_missing_interpreter_fails_before_candidate_publication(layout, tmp_path, monkeypatch):
    workspace, _, _ = layout
    monkeypatch.setattr(install.shutil, "which", lambda _: None)
    destination = tmp_path / "candidates"
    with pytest.raises(install.InstallError, match="validated Python"):
        install.generate_candidates(workspace.root, destination, executable=str(tmp_path / "different-bin/agentcoord"))
    assert not destination.exists()


def test_herdr_path_runtime_uses_homebrew_opt_alias_instead_of_versioned_keg(tmp_path, monkeypatch):
    prefix = tmp_path / "homebrew"
    keg = prefix / "Cellar/python@3.14/3.14.8/bin/python3"
    stable = prefix / "opt/python@3.14/bin/python3"
    for interpreter in (keg, stable):
        interpreter.parent.mkdir(parents=True)
        interpreter.symlink_to(sys.executable)
    monkeypatch.setattr(install.shutil, "which", lambda _: str(keg))
    assert install._herdr_python(str(tmp_path / "custom-bin/agentcoord")) == str(stable)


def test_service_inspection_is_read_only(layout):
    workspace, home, executable = layout
    runner = Launchctl()
    original = set(home.iterdir())
    result = install.install_service(workspace, home, executable, runner=runner, platform="darwin")
    assert not result["configured"]
    assert set(home.iterdir()) == original
    assert len(runner.calls) == 1


def test_install_private_workspace_unit_and_idempotent_running_service(layout):
    workspace, home, executable = layout
    runner = Launchctl()
    report = install.install_service(workspace, home, executable, apply=True, runner=runner, platform="darwin")
    path = Path(report["plist"])
    plist = plistlib.loads(path.read_bytes())
    assert plist["ProgramArguments"] == [executable, "--project", str(workspace.root), "serve"]
    assert plist["EnvironmentVariables"]["AGENTCOORD_STATE_HOME"] == str(workspace.state_root)
    assert plist["EnvironmentVariables"]["AGENTCOORD_SOCKET_HOME"] == str(workspace.socket_path.parent)
    assert plist["EnvironmentVariables"]["AGENTCOORD_WORKSPACE"] == str(workspace.root)
    assert path.stat().st_mode & 0o777 == 0o600
    assert Path(report["logs"]).stat().st_mode & 0o777 == 0o700
    assert (Path(report["logs"]) / "stdout.log").stat().st_mode & 0o777 == 0o600
    runner.calls.clear()
    install.install_service(workspace, home, executable, apply=True, runner=runner, platform="darwin")
    assert [command[1] for command in runner.calls] == ["print"]


def test_orphan_or_foreign_loaded_units_cannot_be_replaced(layout):
    workspace, home, executable = layout
    runner = Launchctl(loaded=True)
    with pytest.raises(install.InstallError, match="no owned"):
        install.install_service(workspace, home, executable, apply=True, runner=runner, platform="darwin")
    path, _, desired = install.launchd_configuration(workspace, home, executable)
    path.parent.mkdir(parents=True)
    desired["ProgramArguments"] = ["foreign"]
    path.write_bytes(plistlib.dumps(desired))
    original = path.read_bytes()
    with pytest.raises(install.InstallError, match="not owned"):
        install.install_service(workspace, home, executable, apply=True, runner=runner, platform="darwin")
    assert path.read_bytes() == original


def test_restart_drains_owned_service_without_killing_agents(layout):
    workspace, home, executable = layout
    runner = Launchctl()
    install.install_service(workspace, home, executable, apply=True, runner=runner, platform="darwin")
    client = Maintenance(workspace.id)
    runner.calls.clear()
    install.install_service(workspace, home, executable, apply=True, restart=True, client=client, runner=runner, platform="darwin")
    assert client.activated
    assert "bootout" in [call[1] for call in runner.calls]
    assert all(call[0] == "/bin/launchctl" for call in runner.calls)


def test_restart_preserves_preexisting_maintenance_fence(layout):
    workspace, home, executable = layout
    runner = Launchctl()
    install.install_service(workspace, home, executable, apply=True, runner=runner, platform="darwin")
    client = Maintenance(workspace.id)
    client.drained = True
    install.install_service(workspace, home, executable, apply=True, restart=True, client=client, runner=runner, platform="darwin")
    assert client.drained and not client.activated


def test_restart_does_not_activate_an_unverified_release(layout):
    workspace, home, executable = layout
    runner = Launchctl()
    install.install_service(workspace, home, executable, apply=True, runner=runner, platform="darwin")
    client = Maintenance(workspace.id)
    original = client.health
    client.health = lambda: {"ok": True, "data": {**original()["data"], "release": "unknown"}}
    with pytest.raises(install.InstallError, match="not healthy"):
        install.install_service(workspace, home, executable, apply=True, restart=True, client=client, runner=runner, platform="darwin")
    assert client.drained and not client.activated


def test_uncertain_effect_refuses_restart_before_bootout(layout):
    workspace, home, executable = layout
    runner = Launchctl()
    install.install_service(workspace, home, executable, apply=True, runner=runner, platform="darwin")
    runner.calls.clear()
    with pytest.raises(install.InstallError, match="Uncertain"):
        install.install_service(workspace, home, executable, apply=True, restart=True, client=Maintenance(workspace.id, uncertain=1), runner=runner, platform="darwin")
    assert [call[1] for call in runner.calls] == ["print"]


def test_health_missing_effect_count_fails_explicitly(layout):
    workspace, _, _ = layout
    client = Maintenance(workspace.id)
    original = client.health
    client.health = lambda: {"ok": True, "data": {key: value for key, value in original()["data"].items() if key != "running_effects"}}
    with pytest.raises(install.InstallError, match="missing or invalid"):
        install._drain(client, workspace)


def test_failed_bootout_preserves_existing_configuration(layout):
    workspace, home, executable = layout
    runner = Launchctl()
    report = install.install_service(workspace, home, executable, apply=True, runner=runner, platform="darwin")
    path = Path(report["plist"])
    original = path.read_bytes()
    runner.fail = "bootout"
    with pytest.raises(install.InstallError, match="Cannot unload"):
        install.remove_service(workspace, home, executable, apply=True, client=Maintenance(workspace.id), runner=runner, platform="darwin")
    assert path.read_bytes() == original


def test_removal_preserves_private_history_and_pending_job_storage(layout):
    workspace, home, executable = layout
    runner = Launchctl()
    report = install.install_service(workspace, home, executable, apply=True, runner=runner, platform="darwin")
    database = workspace.state_dir / "runtime.sqlite3"
    database.write_bytes(b"retained history")
    install.remove_service(workspace, home, executable, apply=True, client=Maintenance(workspace.id), runner=runner, platform="darwin")
    assert database.read_bytes() == b"retained history"
    assert not Path(report["plist"]).exists()


def test_linux_explicitly_uses_foreground_service(layout):
    workspace, home, executable = layout
    with pytest.raises(install.InstallError, match="foreground"):
        install.install_service(workspace, home, executable, platform="linux")
