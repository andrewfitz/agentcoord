"""Bounded diagnostics that keep configuration and observed execution distinct."""
from __future__ import annotations

import json
import os
import shlex
import stat
import tomllib
from importlib import metadata
from pathlib import Path

from .install import EVENTS, HARNESSES

CONFIG_LIMIT = 262_144
MCP_PATHS = {"claude": ".mcp.json", "codex": ".codex/config.toml", "cursor": ".cursor/mcp.json", "grok": ".grok/config.toml"}
HOOK_PATHS = {"claude": ".claude/settings.json", "codex": ".codex/hooks.json", "cursor": ".cursor/hooks.json", "grok": ".grok/hooks/agentcoord.json"}


def _json_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"Duplicate JSON key: {key}")
        value[key] = item
    return value


def _json_constant(value):
    raise ValueError(f"Invalid JSON constant: {value}")


def _configuration(path: Path) -> tuple[dict | None, str]:
    try:
        if not path.exists():
            return None, "missing"
        if path.stat().st_size > CONFIG_LIMIT:
            return None, "too_large"
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                return None, "invalid"
            if info.st_size > CONFIG_LIMIT:
                return None, "too_large"
            raw = stream.read(CONFIG_LIMIT + 1)
        if len(raw) > CONFIG_LIMIT:
            return None, "too_large"
        content = raw.decode("utf-8")
        value = tomllib.loads(content) if path.suffix == ".toml" else json.loads(content, object_pairs_hook=_json_pairs, parse_constant=_json_constant)
        if not isinstance(value, dict):
            return None, "invalid"
        return value, "parsed"
    except (OSError, ValueError, UnicodeError):
        return None, "invalid"


def harness_configuration(root: Path, harness: str) -> dict:
    mcp_path = root / MCP_PATHS[harness]
    config, state = _configuration(mcp_path)
    result = {"mcp": {"path": str(mcp_path), "state": state}, "configured": False}
    if config is not None:
        servers = config.get("mcp_servers" if harness in {"codex", "grok"} else "mcpServers", {})
        entry = servers.get("agentcoord") if isinstance(servers, dict) else None
        if isinstance(entry, dict):
            args = entry.get("args")
            configured = isinstance(entry.get("command"), str) and bool(entry["command"]) and isinstance(args, list) and all(isinstance(arg, str) for arg in args)
            configured = configured and "mcp" in args and "--harness" in args and args[args.index("--harness") + 1:] == [harness]
            configured = configured and "--project" in args and args.index("--project") + 1 < len(args) and Path(args[args.index("--project") + 1]).resolve() == root
            configured = configured and not entry.get("url") and entry.get("type", "stdio") == "stdio"
            result["mcp"]["state"] = "configured" if configured else "invalid_registration"
            result["configured"] = bool(configured)
        else:
            result["mcp"]["state"] = "not_registered"
    result["lifecycle"] = _lifecycle_configuration(root, harness)
    return result


def _lifecycle_source(path: Path, root: Path, harness: str) -> dict:
    """Inspect registration only; conditional hooks do not prove full coverage."""
    hooks, hook_state = _configuration(path)
    observed = set()
    if hooks is not None:
        if harness == "cursor" and "version" in hooks and (type(hooks["version"]) is not int or hooks["version"] != 1):
            hook_state = "invalid"
        event_map = hooks.get("hooks", {})
        if isinstance(event_map, dict):
            for native_event, groups in event_map.items():
                event = EVENTS[harness].get(native_event)
                if not isinstance(groups, list):
                    hook_state = "invalid"
                    continue
                for group in groups:
                    if not isinstance(group, dict):
                        hook_state = "invalid"
                        continue
                    unconditional = "matcher" not in group if harness == "cursor" else not any(key != "hooks" for key in group)
                    if harness != "cursor" and "hooks" not in group:
                        hook_state = "invalid"
                        continue
                    handlers = [group] if harness == "cursor" else group.get("hooks", [])
                    if not isinstance(handlers, list):
                        hook_state = "invalid"
                        continue
                    for handler in handlers:
                        if not isinstance(handler, dict):
                            hook_state = "invalid"
                            continue
                        if handler.get("type", "command") != "command":
                            continue
                        raw_command = handler.get("command", "")
                        if not isinstance(raw_command, str) or not raw_command.strip():
                            hook_state = "invalid"
                            continue
                        try:
                            command = shlex.split(raw_command)
                        except ValueError:
                            command = []
                            hook_state = "invalid"
                        if command and command[0] == "env":
                            command = command[1:]
                            while command and "=" in command[0]:
                                command = command[1:]
                        project = command.index("--project") if "--project" in command else -1
                        matches_project = (command.count("--project") == 1 and project + 1 < len(command)
                                           and Path(command[project + 1]).resolve() == root)
                        if unconditional and event is not None and matches_project and "hook" in command and command[command.index("hook") + 1:] == [harness, event] and Path(command[0]).name == "agentcoord":
                            observed.add(native_event)
            if hook_state != "invalid":
                hook_state = "configured" if observed == set(EVENTS[harness]) else "partial" if observed else "not_registered"
        else:
            hook_state = "invalid"
    return {"path": str(path), "state": hook_state, "events": sorted(observed)}


def _lifecycle_configuration(root: Path, harness: str) -> dict:
    paths = [root / HOOK_PATHS[harness]]
    if harness == "codex":
        global_path = Path.home() / HOOK_PATHS[harness]
        if global_path not in paths:
            paths.append(global_path)
    sources = [_lifecycle_source(path, root, harness) for path in paths]
    events = {event for source in sources for event in source["events"]}
    if any(source["state"] in {"invalid", "too_large"} for source in sources):
        state = "invalid"
    elif events == set(EVENTS[harness]):
        state = "configured"
    elif events:
        state = "partial"
    else:
        state = "missing" if all(source["state"] == "missing" for source in sources) else "not_registered"
    # Keep the familiar singular path for global-only installations while making
    # every source, including a missing or invalid layer, explicit.
    primary = next((source for source in sources if source["state"] != "missing"), sources[0])
    return {"path": primary["path"], "state": state, "events": sorted(events), "sources": sources}


def inspect(workspace, *, client=None, live: bool = False, root: Path | None = None) -> dict:
    """Inspect known config files; only --live probes this workspace's daemon."""
    root = Path(workspace.root if root is None else root).resolve()
    try:
        installed = metadata.version("agentcoord")
    except metadata.PackageNotFoundError:
        installed = "source"
    report = {"workspace_id": workspace.id, "root": str(root), "installed_release": installed,
              "configuration": [], "connection": {"state": "not_checked"},
              "binding": {"state": "not_checked"}, "database": {"state": "not_checked"},
              "observed": {"state": "not_checked"}, "issues": [], "status": "needs_verification"}
    for harness in HARNESSES:
        config = harness_configuration(root, harness)
        report["configuration"].append({"harness": harness, **config})
        if not config["configured"]:
            report["issues"].append({"code": "MCP_NOT_CONFIGURED", "harness": harness, "next_action": "Review and merge the generated MCP candidate for this harness."})
        if config["lifecycle"]["state"] == "invalid":
            report["issues"].append({"code": "LIFECYCLE_CONFIGURATION_INVALID", "harness": harness, "next_action": "Repair the malformed lifecycle source shown in configuration before treating registration as complete."})
    if live:
        if client is None:
            report["connection"] = {"state": "unavailable"}
            report["issues"].append({"code": "SERVICE_UNAVAILABLE", "next_action": "Start this workspace's installed foreground or managed service."})
        else:
            try:
                envelope = client.health()
                if not envelope.get("ok"):
                    raise ValueError("health rejected")
                health = envelope["data"]
                if health.get("workspace_id") != workspace.id or type(health.get("protocol")) is not int or health["protocol"] != 1:
                    report["connection"] = {"state": "identity_mismatch"}
                    report["issues"].append({"code": "SERVICE_IDENTITY_MISMATCH", "next_action": "Inspect the selected workspace and socket registration."})
                else:
                    report["connection"] = {"state": "reachable", "protocol": health["protocol"], "release": health.get("release"), "service_state": health.get("service_state")}
                    report["database"] = {"state": health.get("database_state", "unknown"), "schema_version": health.get("schema_version")}
                    if health.get("binding") is not None:
                        report["binding"] = health["binding"]
                    if health.get("observed") is not None:
                        report["observed"] = health["observed"]
            except (OSError, TimeoutError, ValueError, KeyError, TypeError):
                report["connection"] = {"state": "unavailable"}
                report["issues"].append({"code": "SERVICE_UNAVAILABLE", "next_action": "Inspect the owned service and its private logs; retry the same operation key after an uncertain mutation."})
    configured = sum(item["configured"] for item in report["configuration"])
    report["summary"] = f"{configured}/4 MCP configurations registered; connection, binding and lifecycle observations are separate evidence."
    if not report["issues"]:
        report["status"] = "configured" if not live else "reachable"
    return report
