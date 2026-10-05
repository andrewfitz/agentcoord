import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agentcoord import doctor, install


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("AGENTCOORD_STATE_HOME", str(tmp_path / "state"))
    from agentcoord.config import register_workspace
    return register_workspace(tmp_path)


def configure(workspace, harness):
    mcp = workspace.root / doctor.MCP_PATHS[harness]
    hooks = workspace.root / doctor.HOOK_PATHS[harness]
    mcp.parent.mkdir(parents=True, exist_ok=True)
    hooks.parent.mkdir(parents=True, exist_ok=True)
    registration = install.mcp_config(harness, workspace.root)
    mcp.write_text(install._toml_mcp(registration) if mcp.suffix == ".toml" else json.dumps(registration))
    hooks.write_text(json.dumps(install.hook_config(harness, workspace.root)))


def test_read_only_doctor_separates_configuration_from_connection_and_activation(workspace):
    for harness in install.HARNESSES:
        configure(workspace, harness)
    report = doctor.inspect(workspace)
    assert report["status"] == "configured"
    assert all(item["configured"] for item in report["configuration"])
    assert all(item["lifecycle"]["state"] == "configured" for item in report["configuration"])
    assert report["connection"]["state"] == "not_checked"
    assert report["binding"]["state"] == "not_checked"
    assert report["observed"]["state"] == "not_checked"


def test_live_doctor_does_not_infer_binding_or_presence_from_socket_health(workspace):
    class Service:
        def health(self):
            return {"ok": True, "data": {"workspace_id": workspace.id, "protocol": 1, "schema_version": 1, "release": "0.1.0", "service_state": "active", "database_state": "healthy"}}
    report = doctor.inspect(workspace, client=Service(), live=True)
    assert report["connection"]["state"] == "reachable"
    assert report["binding"]["state"] == "not_checked"
    assert report["observed"]["state"] == "not_checked"


def test_wrong_workspace_socket_is_explicit(workspace):
    class Service:
        def health(self):
            return {"ok": True, "data": {"workspace_id": "another-workspace", "protocol": 1}}
    report = doctor.inspect(workspace, client=Service(), live=True)
    assert report["connection"]["state"] == "identity_mismatch"


def test_unknown_service_error_does_not_echo_private_payload(workspace):
    class Service:
        def health(self):
            return {"ok": False, "error": {"message": "secret binding token"}}
    report = doctor.inspect(workspace, client=Service(), live=True)
    assert "secret binding token" not in json.dumps(report)
    assert report["connection"]["state"] == "unavailable"


def test_duplicate_or_oversized_config_is_not_a_success(workspace):
    file = workspace.root / ".mcp.json"
    file.write_text('{"mcpServers":{},"mcpServers":{}}')
    assert doctor.harness_configuration(workspace.root, "claude")["mcp"]["state"] == "invalid"
    file.write_text(" " * (doctor.CONFIG_LIMIT + 1))
    assert doctor.harness_configuration(workspace.root, "claude")["mcp"]["state"] == "too_large"


def test_another_workspace_registration_is_not_configured(workspace, tmp_path):
    configure(workspace, "claude")
    file = workspace.root / ".mcp.json"
    value = json.loads(file.read_text())
    value["mcpServers"]["agentcoord"]["args"][1] = str(tmp_path / "another")
    file.write_text(json.dumps(value))
    assert not doctor.harness_configuration(workspace.root, "claude")["configured"]


def test_codex_lifecycle_uses_native_global_hooks(workspace, tmp_path, monkeypatch):
    configure(workspace, "codex")
    home = tmp_path / "native-home"
    hooks = home / ".codex/hooks.json"
    hooks.parent.mkdir(parents=True)
    hooks.write_text(json.dumps(install.hook_config("codex", workspace.root)))
    (workspace.root / ".codex/hooks.json").unlink()
    monkeypatch.setattr(Path, "home", lambda: home)
    lifecycle = doctor.harness_configuration(workspace.root, "codex")["lifecycle"]
    assert lifecycle["path"] == str(hooks)
    assert lifecycle["state"] == "configured"


def test_global_lifecycle_for_other_workspace_is_not_configured(workspace, tmp_path):
    configure(workspace, "codex")
    hooks = Path.home() / ".codex/hooks.json"
    from agentcoord.config import register_workspace
    other = tmp_path / "other-workspace"
    other.mkdir()
    register_workspace(other)
    hooks.write_text(json.dumps(install.hook_config("codex", other)))
    lifecycle = doctor.harness_configuration(workspace.root, "codex")["lifecycle"]
    assert lifecycle["state"] == "not_registered"
    assert lifecycle["events"] == []


@pytest.mark.parametrize("command", [123, {"unexpected": "object"}])
def test_non_text_hook_command_has_explicit_invalid_diagnostic(workspace, command):
    hooks = workspace.root / doctor.HOOK_PATHS["claude"]
    hooks.parent.mkdir(parents=True)
    hooks.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [{"command": command}]}]}}))
    lifecycle = doctor.harness_configuration(workspace.root, "claude")["lifecycle"]
    assert lifecycle["state"] == "invalid"


def test_codex_project_lifecycle_is_configured_without_global_hooks(workspace, tmp_path, monkeypatch):
    configure(workspace, "codex")
    home = tmp_path / "separate-native-home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    lifecycle = doctor.harness_configuration(workspace.root, "codex")["lifecycle"]
    project = workspace.root / ".codex/hooks.json"
    global_path = home / ".codex/hooks.json"
    assert lifecycle["state"] == "configured"
    assert lifecycle["path"] == str(project)
    assert lifecycle["events"] == sorted(install.EVENTS["codex"])
    assert lifecycle["sources"] == [
        {"path": str(project), "state": "configured", "events": sorted(install.EVENTS["codex"])},
        {"path": str(global_path), "state": "missing", "events": []},
    ]


def test_codex_lifecycle_merges_project_and_global_event_coverage(workspace, tmp_path, monkeypatch):
    configure(workspace, "codex")
    home = tmp_path / "separate-native-home"
    hooks = home / ".codex/hooks.json"
    hooks.parent.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    generated = install.hook_config("codex", workspace.root)["hooks"]
    events = list(generated)
    project_events, global_events = events[:2], events[2:]
    (workspace.root / ".codex/hooks.json").write_text(json.dumps({"hooks": {event: generated[event] for event in project_events}}))
    hooks.write_text(json.dumps({"hooks": {event: generated[event] for event in global_events}}))
    lifecycle = doctor.harness_configuration(workspace.root, "codex")["lifecycle"]
    assert lifecycle["state"] == "configured"
    assert lifecycle["events"] == sorted(events)
    assert [source["state"] for source in lifecycle["sources"]] == ["partial", "partial"]
    assert lifecycle["sources"][0]["events"] == sorted(project_events)
    assert lifecycle["sources"][1]["events"] == sorted(global_events)


@pytest.mark.parametrize("invalid_layer", ["project", "global"])
@pytest.mark.parametrize("invalid_content", ['{"hooks":{},"hooks":{}}', '{"hooks":{},"custom":NaN}'])
def test_invalid_codex_layer_prevents_clean_combined_lifecycle(workspace, tmp_path, monkeypatch, invalid_layer, invalid_content):
    configure(workspace, "codex")
    home = tmp_path / "separate-native-home"
    global_path = home / ".codex/hooks.json"
    global_path.parent.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    global_path.write_text(json.dumps(install.hook_config("codex", workspace.root)))
    invalid = global_path if invalid_layer == "global" else workspace.root / ".codex/hooks.json"
    invalid.write_text(invalid_content)
    lifecycle = doctor.harness_configuration(workspace.root, "codex")["lifecycle"]
    assert lifecycle["state"] == "invalid"
    assert next(source for source in lifecycle["sources"] if source["path"] == str(invalid))["state"] == "invalid"


def test_restrictive_codex_matcher_is_not_unconditional_coverage(workspace, tmp_path, monkeypatch):
    configure(workspace, "codex")
    home = tmp_path / "separate-native-home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    hooks = install.hook_config("codex", workspace.root)
    hooks["hooks"]["SessionStart"][0]["matcher"] = "startup"
    (workspace.root / ".codex/hooks.json").write_text(json.dumps(hooks))
    lifecycle = doctor.harness_configuration(workspace.root, "codex")["lifecycle"]
    assert lifecycle["state"] == "partial"
    assert "SessionStart" not in lifecycle["events"]


def test_malformed_conditional_group_is_invalid_not_missing_coverage(workspace):
    configure(workspace, "codex")
    path = workspace.root / ".codex/hooks.json"
    hooks = install.hook_config("codex", workspace.root)
    hooks["hooks"]["SessionStart"].append({"matcher": "startup", "hooks": {"command": "foreign-hook"}})
    path.write_text(json.dumps(hooks))
    lifecycle = doctor.harness_configuration(workspace.root, "codex")["lifecycle"]
    assert lifecycle["state"] == "invalid"


def test_malformed_codex_layer_is_reported_even_when_all_mcp_servers_registered(workspace, tmp_path, monkeypatch):
    for harness in install.HARNESSES:
        configure(workspace, harness)
    home = tmp_path / "separate-native-home"
    path = home / ".codex/hooks.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"hooks":[]}')
    monkeypatch.setattr(Path, "home", lambda: home)
    report = doctor.inspect(workspace)
    assert all(item["configured"] for item in report["configuration"])
    assert report["status"] == "needs_verification"
    assert any(issue["code"] == "LIFECYCLE_CONFIGURATION_INVALID" and issue["harness"] == "codex" for issue in report["issues"])
    assert report["observed"]["state"] == "not_checked"


@pytest.mark.parametrize("version", [True, "1", 2])
def test_unsupported_cursor_hook_schema_is_invalid_even_with_complete_events(workspace, version):
    configure(workspace, "cursor")
    path = workspace.root / ".cursor/hooks.json"
    value = json.loads(path.read_text())
    value["version"] = version
    path.write_text(json.dumps(value))
    assert doctor.harness_configuration(workspace.root, "cursor")["lifecycle"]["state"] == "invalid"
