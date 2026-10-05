"""Behavioral coverage for portable, preserving native harness installation."""
from __future__ import annotations

import json
import re
import shutil
import sys
import tomllib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agentcoord import install

MCP_PATHS = {
    "claude": ".mcp.json",
    "codex": ".codex/config.toml",
    "cursor": ".cursor/mcp.json",
    "grok": ".grok/config.toml",
}
HOOK_PATHS = {
    "claude": ".claude/settings.json",
    "codex": ".codex/hooks.json",
    "cursor": ".cursor/hooks.json",
    "grok": ".grok/hooks/agentcoord.json",
}
SKILL = ".agents/skills/agentcoord"


@pytest.fixture
def setup_layout(tmp_path, monkeypatch):
    from agentcoord.config import register_workspace

    root = tmp_path / "workspace with spaces $(literal)"
    home = tmp_path / "native-home"
    root.mkdir()
    home.mkdir()
    monkeypatch.setenv("AGENTCOORD_STATE_HOME", str(tmp_path / "private-state"))
    monkeypatch.setattr(Path, "home", lambda: home)
    register_workspace(root)
    return root, home


def snapshot(root):
    """Capture the published tree, including dangling links and empty dirs."""
    if not root.exists():
        return {}
    result = {}
    for path in sorted(root.rglob("*")):
        name = path.relative_to(root).as_posix()
        result[name] = (
            ("link", str(path.readlink())) if path.is_symlink()
            else ("dir",) if path.is_dir()
            else ("file", path.read_bytes(), path.stat().st_mode & 0o777)
        )
    return result


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def config_value(path):
    content = path.read_text()
    return tomllib.loads(content) if path.suffix == ".toml" else json.loads(content)


def test_complete_setup_preview_has_no_filesystem_effects(setup_layout):
    root, home = setup_layout
    (root / "AGENTS.md").write_text("# Existing authority\n\nKeep our instructions.\n")
    before = snapshot(root), snapshot(home)
    report = install.init_project(root, home=home)
    assert report["applied"] is False
    assert (snapshot(root), snapshot(home)) == before
    planned = {Path(item["path"]).relative_to(root).as_posix() for item in report["files"]}
    assert set(MCP_PATHS.values()) <= planned
    assert set(HOOK_PATHS.values()) <= planned
    assert f"{SKILL}/SKILL.md" in planned


def test_complete_setup_routes_all_harnesses_and_is_idempotent(setup_layout):
    root, home = setup_layout
    prose = "# Existing authority\n\nKeep our instructions.\n"
    (root / "AGENTS.md").write_text(prose)
    (root / ".agentcoord.toml").write_text("# Preserve user configuration\n[custom]\nvalue = 42\n")
    original_config = (root / ".agentcoord.toml").read_bytes()
    install.init_project(root, apply=True, home=home)
    assert (root / "AGENTS.md").read_text().startswith(prose)
    assert (root / ".agentcoord.toml").read_bytes() == original_config
    for harness, relative in MCP_PATHS.items():
        value = config_value(root / relative)
        key = "mcp_servers" if harness in {"codex", "grok"} else "mcpServers"
        entry = value[key]["agentcoord"]
        assert entry["command"] == "agentcoord"
        assert entry["args"] == ["--project", str(root), "mcp", "--harness", harness]
        hooks = json.loads((root / HOOK_PATHS[harness]).read_text())["hooks"]
        assert set(install.EVENTS[harness]) <= hooks.keys()
        assert not any("Tool" in event or "Shell" in event for event in hooks)
    first = snapshot(root), snapshot(home)
    second = install.init_project(root, apply=True, home=home)
    assert (snapshot(root), snapshot(home)) == first
    assert all(not item["changed"] for item in second["files"])


def test_json_tools_settings_and_lifecycle_handlers_are_preserved(setup_layout):
    root, home = setup_layout
    foreign_server = {"command": "foreign-server", "args": ["--stdio"], "env": {"PROJECT": "ours"}}
    foreign_handler = {"type": "command", "command": "our-existing-hook --flag", "timeout": 9}
    for harness in ("claude", "cursor"):
        write_json(root / MCP_PATHS[harness], {"mcpServers": {"other": foreign_server}, "otherSettings": [1, 2]})
        group = foreign_handler if harness == "cursor" else {"matcher": "startup", "hooks": [foreign_handler]}
        event = next(iter(install.EVENTS[harness]))
        write_json(root / HOOK_PATHS[harness], {"hooks": {event: [group], "unrelatedEvent": [group]}, "custom": {"enabled": True}})
    install.init_project(root, apply=True, home=home)
    for harness in ("claude", "cursor"):
        mcp = config_value(root / MCP_PATHS[harness])
        assert mcp["mcpServers"]["other"] == foreign_server
        assert mcp["otherSettings"] == [1, 2]
        value = config_value(root / HOOK_PATHS[harness])
        group = foreign_handler if harness == "cursor" else {"matcher": "startup", "hooks": [foreign_handler]}
        assert group in value["hooks"][next(iter(install.EVENTS[harness]))]
        assert value["hooks"]["unrelatedEvent"] == [group]
        assert value["custom"] == {"enabled": True}


@pytest.mark.parametrize("harness", ["codex", "grok"])
def test_toml_comments_inline_tables_and_unrelated_tools_survive(setup_layout, harness):
    root, home = setup_layout
    original = (
        '# This project owns these settings.\n'
        'model = "ours" # retain this inline comment\n'
        'custom = { text = "[mcp_servers.agentcoord]", flag = true }\n\n'
        '[mcp_servers.other]\n'
        'command = "foreign-server" # useful local explanation\n'
        'args = ["--stdio"]\n\n'
        '[mcp_servers.other.env]\n'
        'KEY = "value"\n'
    )
    path = root / MCP_PATHS[harness]
    path.parent.mkdir(parents=True)
    path.write_text(original)
    install.init_project(root, apply=True, harnesses=(harness,), home=home)
    updated = path.read_text()
    assert original in updated
    value = tomllib.loads(updated)
    assert value["model"] == "ours"
    assert value["custom"] == {"text": "[mcp_servers.agentcoord]", "flag": True}
    assert value["mcp_servers"]["other"] == {"command": "foreign-server", "args": ["--stdio"], "env": {"KEY": "value"}}


@pytest.mark.parametrize("relative,content", [
    (".mcp.json", '{"mcpServers":{},"mcpServers":{}}'),
    (".mcp.json", '{"mcpServers":{},"unrelated":NaN}'),
    (".cursor/hooks.json", '{"hooks": []}'),
    (".cursor/hooks.json", '{"version":true,"hooks":{}}'),
    (".grok/config.toml", '[mcp_servers.other]\ncommand = '),
])
def test_invalid_configuration_refuses_before_any_publication(setup_layout, relative, content):
    root, home = setup_layout
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    before = snapshot(root), snapshot(home)
    with pytest.raises(install.InstallError):
        install.init_project(root, apply=True, home=home)
    assert (snapshot(root), snapshot(home)) == before


def test_foreign_reserved_mcp_entry_is_never_replaced(setup_layout):
    root, home = setup_layout
    write_json(root / ".mcp.json", {"mcpServers": {"agentcoord": {"command": "foreign-agentcoord", "args": ["custom"]}}})
    before = snapshot(root), snapshot(home)
    with pytest.raises(install.InstallError):
        install.init_project(root, apply=True, home=home)
    assert (snapshot(root), snapshot(home)) == before


def test_matching_mcp_with_custom_extra_settings_is_not_silently_adopted(setup_layout):
    root, home = setup_layout
    value = install.mcp_config("claude", root)
    value["mcpServers"]["agentcoord"]["customOwnerPolicy"] = {"enabled": True}
    write_json(root / ".mcp.json", value)
    before = snapshot(root)
    with pytest.raises(install.InstallError):
        install.init_project(root, apply=True, home=home)
    assert snapshot(root) == before


@pytest.mark.parametrize("relative,dangling", [
    (".cursor", False),
    (".mcp.json", False),
    (f"{SKILL}/SKILL.md", True),
])
def test_symlink_targets_and_ancestors_cannot_redirect_publication(setup_layout, tmp_path, relative, dangling):
    root, home = setup_layout
    target = root / relative
    foreign = tmp_path / "foreign"
    if not dangling:
        if relative == ".cursor":
            foreign.mkdir()
            (foreign / "owned.txt").write_text("preserve foreign directory")
        else:
            foreign.write_text("preserve foreign file")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(foreign, target_is_directory=relative == ".cursor")
    before = snapshot(root), snapshot(home), snapshot(tmp_path)
    with pytest.raises(install.InstallError):
        install.init_project(root, apply=True, home=home)
    assert (snapshot(root), snapshot(home), snapshot(tmp_path)) == before


def test_foreign_canonical_skill_is_not_overwritten(setup_layout):
    root, home = setup_layout
    path = root / SKILL / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nname: agentcoord\ndescription: Our private workflow\n---\n\nKeep this.\n")
    before = snapshot(root)
    with pytest.raises(install.InstallError):
        install.init_project(root, apply=True, home=home)
    assert snapshot(root) == before


def test_native_skill_adapter_customizations_are_preserved(setup_layout):
    root, home = setup_layout
    original = "---\nname: agentcoord\ndescription: Our tailored coordination entrypoint\n---\n\nKeep our additions.\n"
    for harness in ("claude", "cursor"):
        path = root / f".{harness}/skills/agentcoord/SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text(original)
        (path.parent / "team.md").write_text("Our supporting notes.\n")
    install.init_project(root, apply=True, home=home)
    for harness in ("claude", "cursor"):
        path = root / f".{harness}/skills/agentcoord/SKILL.md"
        assert path.read_text() == original
        assert (path.parent / "team.md").read_text() == "Our supporting notes.\n"
    assert (root / SKILL / "SKILL.md").is_file()


def test_modified_managed_resource_refuses_and_preserves_every_file(setup_layout):
    root, home = setup_layout
    install.init_project(root, apply=True, home=home)
    path = root / SKILL / "references/commands.md"
    path.write_text(path.read_text() + "\nUser-owned additions.\n")
    before = snapshot(root), snapshot(home)
    with pytest.raises(install.InstallError):
        install.init_project(root, apply=True, home=home)
    assert (snapshot(root), snapshot(home)) == before


def test_installed_skill_references_remain_portable_when_project_moves(setup_layout, tmp_path):
    root, home = setup_layout
    install.init_project(root, apply=True, home=home)
    moved = tmp_path / "relocated-project"
    shutil.copytree(root, moved)
    canonical = moved / SKILL
    assert (canonical / "SKILL.md").is_file()
    assert (canonical / "references/commands.md").is_file()
    assert (canonical / "references/workflows.md").is_file()
    for path in [*canonical.rglob("*.md"), moved / ".claude/skills/agentcoord/SKILL.md", moved / ".cursor/skills/agentcoord/SKILL.md"]:
        text = path.read_text()
        assert str(root) not in text
        assert "/Users/andrew/" not in text
        for target in re.findall(r"\[[^\]]*\]\(([^)]+)\)", text):
            if "://" in target or target.startswith("#"):
                continue
            target = target.split("#", 1)[0]
            assert (path.parent / target).is_file(), (path, target)


def test_existing_native_global_codex_hooks_remain_byte_exact(setup_layout):
    root, home = setup_layout
    existing = home / ".codex/hooks.json"
    write_json(existing, {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "foreign-global-hook"}]}]}, "custom": True})
    original = existing.read_bytes()
    install.init_project(root, apply=True, home=home)
    assert existing.read_bytes() == original
    assert (root / ".codex/hooks.json").is_file()


def test_identical_global_codex_hook_is_not_duplicated_locally(setup_layout):
    root, home = setup_layout
    existing = home / ".codex/hooks.json"
    generated = install.hook_config("codex", root)
    for groups in generated["hooks"].values():
        groups[0]["hooks"][0]["command"] = "  " + groups[0]["hooks"][0]["command"] + " \t"
    write_json(existing, generated)
    original = existing.read_bytes()
    install.init_project(root, apply=True, home=home)
    assert existing.read_bytes() == original
    local = root / ".codex/hooks.json"
    if local.exists():
        hooks = json.loads(local.read_text()).get("hooks", {})
        assert all(not groups for groups in hooks.values())


def test_restrictive_global_matcher_does_not_remove_broad_project_hook(setup_layout):
    root, home = setup_layout
    generated = install.hook_config("codex", root)
    generated["hooks"]["SessionStart"][0]["matcher"] = "startup"
    write_json(home / ".codex/hooks.json", generated)
    install.init_project(root, apply=True, home=home)
    local = json.loads((root / ".codex/hooks.json").read_text())
    groups = local["hooks"]["SessionStart"]
    assert groups
    assert any(group.get("matcher", "") in {"", "*"} for group in groups)


def test_candidates_contain_skills_without_live_project_configuration(setup_layout, tmp_path):
    root, home = setup_layout
    destination = tmp_path / "review-candidates"
    before = snapshot(root), snapshot(home)
    report = install.generate_candidates(root, destination)
    assert report["activated"] is False
    assert (snapshot(root), snapshot(home)) == before
    assert (destination / SKILL / "SKILL.md").is_file()
    assert (destination / SKILL / "references/commands.md").is_file()
    assert (destination / SKILL / "references/workflows.md").is_file()


def test_cli_preview_then_apply_on_genuinely_unregistered_project(tmp_path, monkeypatch, capsys):
    from agentcoord import cli
    from agentcoord.config import discover_workspace

    root = tmp_path / "unregistered workspace"
    home = tmp_path / "native-home"
    state = tmp_path / "private-state"
    root.mkdir()
    home.mkdir()
    monkeypatch.setenv("AGENTCOORD_STATE_HOME", str(state))
    monkeypatch.setattr(Path, "home", lambda: home)
    before = snapshot(tmp_path)
    assert cli.main(["--project", str(root), "init"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["ok"] is True
    assert preview["data"]["applied"] is False
    assert "workspace_id" not in preview["data"]
    assert snapshot(tmp_path) == before
    assert not state.exists()

    assert cli.main(["--project", str(root), "init", "--apply"]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["ok"] is True
    assert applied["data"]["applied"] is True
    workspace = discover_workspace(explicit_root=root)
    assert applied["data"]["workspace_id"] == workspace.id
    assert (root / SKILL / "SKILL.md").is_file()
    for path in MCP_PATHS.values():
        assert (root / path).is_file()
    first = snapshot(tmp_path)
    assert cli.main(["--project", str(root), "init", "--apply"]) == 0
    rerun = json.loads(capsys.readouterr().out)
    assert rerun["ok"] is True
    assert rerun["data"]["workspace_id"] == workspace.id
    assert snapshot(tmp_path) == first


def test_cli_candidate_selection_excludes_other_native_integrations(tmp_path, monkeypatch, capsys):
    from agentcoord import cli

    root = tmp_path / "unregistered workspace"
    home = tmp_path / "native-home"
    destination = tmp_path / "review-candidates"
    state = tmp_path / "private-state"
    root.mkdir()
    home.mkdir()
    monkeypatch.setenv("AGENTCOORD_STATE_HOME", str(state))
    monkeypatch.setattr(Path, "home", lambda: home)
    before = snapshot(root), snapshot(home)
    assert cli.main(["--project", str(root), "init", "--candidate-dir", str(destination), "--harnesses", "cursor"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is True
    assert result["data"]["activated"] is False
    assert (snapshot(root), snapshot(home)) == before
    assert not state.exists()
    assert (destination / "cursor-mcp.json").is_file()
    assert (destination / "cursor-hooks.json").is_file()
    assert (destination / ".cursor/skills/agentcoord/SKILL.md").is_file()
    assert (destination / SKILL / "SKILL.md").is_file()
    for harness in ("claude", "codex", "grok"):
        assert not list(destination.glob(f"{harness}-*"))
    assert not (destination / ".claude/skills/agentcoord/SKILL.md").exists()


def test_cli_apply_and_candidate_modes_are_mutually_exclusive(tmp_path, capsys):
    from agentcoord import cli

    before = snapshot(tmp_path)
    with pytest.raises(SystemExit) as error:
        cli.main(["--project", str(tmp_path), "init", "--apply", "--candidate-dir", str(tmp_path / "candidates")])
    assert error.value.code == 2
    assert snapshot(tmp_path) == before
    capsys.readouterr()
