"""Plan preserving project-local harness configuration and agent guidance."""
from __future__ import annotations

import hashlib
import json
import re
import shlex
from pathlib import Path

import tomlkit

from . import __version__
from .install import (
    HARNESSES,
    InstallError,
    hook_config,
    integration_text,
    mcp_config,
    no_links,
)

MCP_PATHS = {"claude": ".mcp.json", "codex": ".codex/config.toml", "cursor": ".cursor/mcp.json", "grok": ".grok/config.toml"}
HOOK_PATHS = {"claude": ".claude/settings.json", "codex": ".codex/hooks.json", "cursor": ".cursor/hooks.json", "grok": ".grok/hooks/agentcoord.json"}
SKILL_ROOT = ".agents/skills/agentcoord"
RESOURCE_MANIFEST = ".agentcoord/installation.json"
SKILL_FILES = ("SKILL.md", "references/commands.md", "references/workflows.md")


def skill_resources(harnesses=HARNESSES) -> dict[str, bytes]:
    """One shared skill and thin native discovery adapters, without repository paths."""
    resources = {f"{SKILL_ROOT}/{name}": integration_text(f"skill/{name}").encode() for name in SKILL_FILES}
    for harness in ("claude", "cursor"):
        if harness in harnesses:
            resources[f".{harness}/skills/agentcoord/SKILL.md"] = (
                b"---\nname: agentcoord\n"
                b"description: Coordinate shared coding work, real overlaps, dependencies and handoffs through Agentcoord.\n"
                b"---\n\n"
                b"Read the [shared Agentcoord skill](../../../.agents/skills/agentcoord/SKILL.md) "
                b"when coordination matters. It owns the workflow and command references; "
                b"repository instructions retain their authority. Ordinary reads and commands need no coordination.\n"
            )
    return resources


def _bytes(path: Path) -> bytes | None:
    no_links(path)
    if not path.exists():
        return None
    if not path.is_file():
        raise InstallError(f"Installation target is not a regular file: {path}")
    if path.stat().st_size > 1_048_576:
        raise InstallError(f"Installation target exceeds 1 MiB: {path}")
    return path.read_bytes()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _json(path: Path, original: bytes | None) -> dict:
    def reject_constant(value):
        raise ValueError(f"Non-finite JSON constant: {value}")

    try:
        result = {} if original is None else json.loads(original.decode(), object_pairs_hook=_pairs, parse_constant=reject_constant)
        if not isinstance(result, dict):
            raise TypeError("Expected an object")
        return result
    except (TypeError, ValueError, UnicodeError) as error:
        raise InstallError(f"Invalid JSON configuration: {path}: {error}") from error


def _encoded(value: dict, original: bytes | None, previous: dict) -> bytes:
    return original if original is not None and value == previous else (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def _merge_mcp(path: Path, harness: str, root: Path, executable: str) -> tuple:
    original = _bytes(path)
    table = "mcp_servers" if harness in {"codex", "grok"} else "mcpServers"
    desired = mcp_config(harness, root, executable)[table]["agentcoord"]
    if path.suffix == ".toml":
        try:
            document = tomlkit.parse(original.decode() if original is not None else "")
            previous = document.unwrap()
        except (ValueError, UnicodeError) as error:
            raise InstallError(f"Invalid TOML configuration: {path}: {error}") from error
        servers = previous.get(table, {})
        if not isinstance(servers, dict):
            raise InstallError(f"MCP servers must be a table: {path}")
        if "agentcoord" in servers and servers["agentcoord"] != desired:
            raise InstallError(f"Existing agentcoord MCP registration differs; preserve and review it: {path}")
        if "agentcoord" in servers:
            return path, original, original
        if table not in document:
            document[table] = tomlkit.table()
        document[table]["agentcoord"] = desired
        updated = tomlkit.dumps(document).encode()
        expected = dict(previous)
        expected[table] = {**servers, "agentcoord": desired}
        if tomlkit.parse(updated.decode()).unwrap() != expected:
            raise InstallError(f"MCP merge changed unrelated TOML settings: {path}")
    else:
        document = _json(path, original)
        previous = json.loads(json.dumps(document))
        servers = document.setdefault(table, {})
        if not isinstance(servers, dict):
            raise InstallError(f"MCP servers must be an object: {path}")
        if "agentcoord" in servers and servers["agentcoord"] != desired:
            raise InstallError(f"Existing agentcoord MCP registration differs; preserve and review it: {path}")
        servers["agentcoord"] = desired
        updated = _encoded(document, original, previous)
    return path, original, updated


def _tokens(command) -> list[str]:
    try:
        return shlex.split(command) if isinstance(command, str) else []
    except ValueError:
        return []


def _covers(groups, desired, harness) -> bool:
    """Only unconditional native handlers cover an unconditional generated hook."""
    if not isinstance(groups, list):
        raise InstallError("Lifecycle event handlers must be an array")
    for group in groups:
        if not isinstance(group, dict):
            raise InstallError("Lifecycle groups must be objects")
        unconditional = "matcher" not in group if harness == "cursor" else not any(key != "hooks" for key in group)
        handlers = [group] if harness == "cursor" else group.get("hooks", [])
        if not isinstance(handlers, list):
            raise InstallError("Lifecycle group hooks must be an array")
        if not unconditional:
            continue
        for handler in handlers:
            if isinstance(handler, dict) and handler.get("type", "command") == "command" and _tokens(handler.get("command")) == _tokens(desired["command"]):
                return True
    return False


def _merge_hooks(path: Path, harness: str, root: Path, executable: str, home: Path) -> tuple:
    original = _bytes(path)
    document = _json(path, original)
    previous = json.loads(json.dumps(document))
    hooks = document.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise InstallError(f"Lifecycle hooks must be an object: {path}")
    global_hooks = {}
    if harness == "codex":
        global_path = home / ".codex/hooks.json"
        # Global configuration is read-only. Invalid global files cannot silently
        # turn into missing coverage; preserve them and make the problem explicit.
        global_original = _bytes(global_path)
        if global_original is not None:
            global_hooks = _json(global_path, global_original).get("hooks", {})
            if not isinstance(global_hooks, dict):
                raise InstallError(f"Invalid global Codex lifecycle hooks: {global_path}")
    if harness == "cursor":
        version = document.get("version", 1)
        if type(version) is not int or version != 1:
            raise InstallError(f"Unsupported Cursor hook schema: {path}")
        document["version"] = 1
    generated = hook_config(harness, root, executable)["hooks"]
    for event, groups in generated.items():
        desired = groups[0] if harness == "cursor" else groups[0]["hooks"][0]
        existing = hooks.get(event, [])
        if _covers(existing, desired, harness):
            continue
        if harness == "codex" and _covers(global_hooks.get(event, []), desired, harness):
            continue
        hooks[event] = [*existing, *groups]
    return path, original, _encoded(document, original, previous)


def _resource_plan(root: Path, harnesses, *, instructions=True) -> list[tuple]:
    if not instructions:
        return []
    manifest_path = root / RESOURCE_MANIFEST
    manifest_bytes = _bytes(manifest_path)
    manifest = _json(manifest_path, manifest_bytes)
    if manifest and (type(manifest.get("schema_version")) is not int or manifest["schema_version"] not in {1, 2}
                     or not isinstance(manifest.get("resources"), dict)):
        raise InstallError(f"Invalid installed-resource manifest: {manifest_path}")
    if manifest.get("schema_version") == 2:
        release = manifest.get("package_version")
        if not isinstance(release, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", release):
            raise InstallError(f"Invalid installed package version: {manifest_path}")
        if tuple(int(part) for part in release.split(".")) > tuple(int(part) for part in __version__.split(".")):
            raise InstallError(f"Refuse instruction downgrade from {release} to {__version__}; install the current package")
    owned = dict(manifest.get("resources", {}))
    planned = []
    for relative, content in skill_resources(harnesses).items():
        path = root / relative
        original = _bytes(path)
        recorded = owned.get(relative)
        if original is not None and original != content and hashlib.sha256(original).hexdigest() != recorded:
            if relative.startswith((".claude/", ".cursor/")) and recorded is None:
                # A native, user-maintained discovery adapter wins. AGENTS still
                # routes all clients to the canonical shared skill.
                continue
            raise InstallError(f"Preserve modified or foreign skill resource; review it before updating: {path}")
        planned.append((path, original, content))
        owned[relative] = hashlib.sha256(content).hexdigest()
    # Older installers refuse schema 2 rather than accepting hashes and silently
    # restoring obsolete instructions. Schema 1 is upgraded on the first apply.
    updated = (json.dumps({"schema_version": 2, "package_version": __version__, "resources": owned}, indent=2, sort_keys=True) + "\n").encode()
    planned.append((manifest_path, manifest_bytes, updated))
    return planned


def plan_setup(root: Path, *, executable="agentcoord", harnesses=HARNESSES,
               home: Path | None = None, instructions=True) -> list[tuple]:
    """Validate every native target without creating directories or publishing files."""
    harnesses = tuple(harnesses)
    if len(set(harnesses)) != len(harnesses) or any(h not in HARNESSES for h in harnesses):
        raise InstallError("Select each supported harness at most once")
    home = Path.home() if home is None else Path(home)
    planned = _resource_plan(root, harnesses, instructions=instructions)
    for harness in harnesses:
        planned.append(_merge_mcp(root / MCP_PATHS[harness], harness, root, executable))
        planned.append(_merge_hooks(root / HOOK_PATHS[harness], harness, root, executable, home))
    return planned
