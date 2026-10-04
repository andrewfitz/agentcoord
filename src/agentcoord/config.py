"""Explicit workspace discovery and portable package configuration."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import string
import sys
import tempfile
import tomllib
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .core import (
    CoordinationError,
    bounded_text,
    canonical_json,
    identifier,
    integer,
    validate_fields,
)


@dataclass(frozen=True)
class Workspace:
    id: str
    root: Path
    state_dir: Path
    database_path: Path
    socket_path: Path
    authority_marker: Path

    @property
    def state_root(self) -> Path:
        return self.state_dir.parent.parent


@dataclass(frozen=True)
class VersionRule:
    path: str
    match: str
    replacement: str
    increment: str = "patch"
    validate: str | None = None


@dataclass(frozen=True)
class Config:
    version: VersionRule | None = None
    fast_workers: int = 16
    fast_queue: int = 128
    slow_workers: int = 4
    slow_queue: int = 32
    frame_bytes: int = 262144
    action_bytes: int = 8192
    native_executables: dict[str, str] = field(default_factory=dict)
    jobs_log_max_bytes: int = 262144


def native_environment(environ=None) -> dict[str, str]:
    source = os.environ if environ is None else environ
    names = ("CLAUDE_SESSION_ID", "CODEX_THREAD_ID", "GROK_SESSION_ID", "CURSOR_SESSION_ID",
             "AGENTCOORD_CHILD_ID", "AGENTCOORD_NATIVE_RUN_ID", "AGENTCOORD_HARNESS")
    return {name: value.strip() for name in names if isinstance(value := source.get(name), str) and value.strip()}


def daemon_environment(workspace: Workspace) -> dict[str, str]:
    """Retain the exact discovery routing when the managed service restarts."""
    return {"AGENTCOORD_STATE_HOME": str(workspace.state_root),
            "AGENTCOORD_SOCKET_HOME": str(workspace.socket_path.parent),
            "AGENTCOORD_WORKSPACE": str(workspace.root)}


def state_home(*, explicit: Path | None = None) -> Path:
    if explicit is not None:
        return Path(explicit).expanduser().absolute()
    configured = os.environ.get("AGENTCOORD_STATE_HOME", "").strip()
    if configured:
        return Path(configured).expanduser().absolute()
    xdg = os.environ.get("XDG_STATE_HOME", "").strip()
    return ((Path(xdg).expanduser() / "agentcoord") if xdg else
            Path.home() / ".local/state/agentcoord").absolute()


def _registry(root: Path) -> dict:
    path = root / "registry.json"
    if any(item.is_symlink() for item in (root, *root.parents)):
        raise CoordinationError("INVALID_ARGUMENT", "Registry path must not contain symlinks")
    if not path.exists():
        return {"version": 1, "workspaces": {}}
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2_000_000:
        raise CoordinationError("INVALID_ARGUMENT", "Workspace registry is invalid")
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise CoordinationError("STORAGE_UNAVAILABLE", "Workspace registry cannot be read") from error
    if (not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1 or
            not isinstance(data.get("workspaces"), dict)):
        raise CoordinationError("SCHEMA_MISMATCH", "Workspace registry requires a compatible release")
    for key, entry in data["workspaces"].items():
        identifier(key, "registered workspace ID")
        validate_fields(entry, {"root"}, {"root"})
        registered = bounded_text(entry["root"], "registered workspace root", 4096)
        if not Path(registered).is_absolute():
            raise CoordinationError("INVALID_ARGUMENT", "Registered workspace root must be absolute")
    return data


def _workspace(root: Path, workspace_id: str, state_root: Path) -> Workspace:
    identifier(workspace_id, "workspace_id")
    directory = state_root / "workspaces" / workspace_id
    socket_home = os.environ.get("AGENTCOORD_SOCKET_HOME", "").strip()
    socket_root = (Path(socket_home).expanduser().absolute() if socket_home else
                   Path("/private/tmp" if sys.platform == "darwin" else "/tmp") / f"agentcoord-{os.getuid()}")
    socket = socket_root / (hashlib.sha256((str(state_root) + ":" + workspace_id).encode()).hexdigest()[:24] + ".sock")
    if len(os.fsencode(socket)) > 103:
        raise CoordinationError("INVALID_ARGUMENT", "Socket path is too long; select a shorter state root")
    return Workspace(workspace_id, root, directory, directory / "runtime.sqlite3", socket,
                     directory / "authority.json")


def register_workspace(root: Path, *, state_root: Path | None = None,
                       workspace_id: str | None = None, relocate: bool = False) -> Workspace:
    from .store import private_directory
    root = Path(root).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise CoordinationError("INVALID_ARGUMENT", "Workspace root must be a directory")
    home = state_home(explicit=state_root)
    private_directory(home)
    descriptor = os.open(home / ".registry.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        data = _registry(home)
        entries = data["workspaces"]
        matches = [key for key, value in entries.items() if value["root"] == str(root)]
        if len(matches) > 1:
            raise CoordinationError("STORAGE_UNAVAILABLE", "Workspace root has conflicting registry identities")
        if matches:
            if workspace_id is not None and matches[0] != workspace_id:
                raise CoordinationError("WRONG_WORKSPACE", "Root already has another workspace identity")
            return _workspace(root, matches[0], home)
        workspace_id = identifier(workspace_id, "workspace_id") if workspace_id else str(uuid.uuid4())
        previous = entries.get(workspace_id)
        if previous and not relocate:
            raise CoordinationError("WRONG_WORKSPACE", "Relocation requires explicit registration")
        # Relocation is configuration only; daemon/drain ownership is checked
        # by the explicit install/maintenance command before this API call.
        if (previous and (home / "workspaces" / workspace_id / "runtime.sqlite3").exists()
                and Path(previous["root"]).exists()):
            raise CoordinationError("NOT_AUTHORIZED", "Previous workspace root still exists; resolve relocation first")
        entries[workspace_id] = {"root": str(root)}
        fd, temporary = tempfile.mkstemp(prefix=".registry-", dir=home)
        try:
            with os.fdopen(fd, "w") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(canonical_json(data))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, home / "registry.json")
        finally:
            Path(temporary).unlink(missing_ok=True)
        workspace = _workspace(root, workspace_id, home)
        private_directory(workspace.state_dir)
        private_directory(workspace.socket_path.parent)
        return workspace
    finally:
        os.close(descriptor)


def workspace_from_id(workspace_id: str, *, state_root: Path | None = None) -> Workspace:
    home = state_home(explicit=state_root)
    entry = _registry(home)["workspaces"].get(identifier(workspace_id, "workspace_id"))
    if entry is None:
        raise CoordinationError("NOT_FOUND", "Workspace is not registered; run agentcoord init")
    root = Path(entry["root"])
    if not root.is_dir() or root.resolve() != root:
        raise CoordinationError("WRONG_WORKSPACE", "Registered workspace moved; explicitly register its relocation")
    return _workspace(root, workspace_id, home)


def discover_workspace(start: Path | None = None, *, explicit_root: Path | None = None,
                       state_root: Path | None = None, use_environment: bool = True) -> Workspace:
    """Select registered authority; observed native paths can bypass ambient routing."""
    home = state_home(explicit=state_root)
    configured = os.environ.get("AGENTCOORD_WORKSPACE", "").strip()
    if use_environment and explicit_root is None and configured:
        explicit_root = Path(configured)
    target = Path(explicit_root or start or Path.cwd()).expanduser().resolve()
    matches = []
    for key, entry in _registry(home)["workspaces"].items():
        registered = Path(entry["root"])
        if target == registered or (explicit_root is None and registered in target.parents):
            matches.append((len(registered.parts), key))
    if not matches:
        raise CoordinationError("NOT_FOUND", "Workspace is not registered; run agentcoord init for its exact root")
    matches.sort(reverse=True)
    if len(matches) > 1 and matches[0][0] == matches[1][0]:
        raise CoordinationError("WRONG_WORKSPACE", "Workspace discovery is ambiguous")
    return workspace_from_id(matches[0][1], state_root=home)


def load_config(workspace: Workspace) -> Config:
    path = workspace.root / ".agentcoord.toml"
    if not path.exists():
        return Config()
    if path.is_symlink() or path.stat().st_size > 65536:
        raise CoordinationError("INVALID_ARGUMENT", "Configuration must be a bounded regular file")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
                raise CoordinationError("INVALID_ARGUMENT", "Configuration must be a bounded regular file")
            raw = stream.read(65537)
            if len(raw) > 65536:
                raise CoordinationError("INVALID_ARGUMENT", "Configuration must be a bounded regular file")
            data = tomllib.loads(raw.decode("utf-8"))
    except (OSError, ValueError, UnicodeError) as error:
        raise CoordinationError("INVALID_ARGUMENT", "Configuration is invalid TOML") from error
    validate_fields(data, {"version", "limits", "native"})
    values = {}
    limits = data.get("limits", {})
    validate_fields(limits, {"fast_workers", "fast_queue", "slow_workers", "slow_queue", "frame_bytes", "action_bytes", "jobs_log_max_bytes"})
    for name, value in limits.items():
        minimum, maximum = ((4096, 16777216) if name == "jobs_log_max_bytes" else (4096, 262144) if name == "frame_bytes" else
                            (1024, 8192) if name == "action_bytes" else (1, 4096))
        values[name] = integer(value, name, minimum, maximum)
    if data.get("version") is not None:
        rule = data["version"]
        validate_fields(rule, {"path", "match", "replacement", "increment", "validate"}, {"path", "match", "replacement"})
        from .core import normalize_paths
        normalized = normalize_paths([rule["path"]])[0]
        match = bounded_text(rule["match"], "version match", 4096)
        replacement = bounded_text(rule["replacement"], "version replacement", 4096)
        try:
            pattern = re.compile(match, re.MULTILINE)
            if not match.startswith("^") or not match.endswith("$") or not {"major", "minor", "patch"} <= pattern.groupindex.keys():
                raise ValueError("match must be anchored and contain major/minor/patch groups")
            names = {name for _, name, _, _ in string.Formatter().parse(replacement) if name is not None}
            if not names <= set(pattern.groupindex):
                raise ValueError("replacement has unknown groups")
            if rule.get("validate") is not None:
                re.compile(bounded_text(rule["validate"], "version validation", 4096), re.MULTILINE)
        except (re.error, ValueError) as error:
            raise CoordinationError("INVALID_ARGUMENT", "Version synthesis rule is invalid") from error
        increment = rule.get("increment", "patch")
        if increment not in {"major", "minor", "patch"}:
            raise CoordinationError("INVALID_ARGUMENT", "Version increment must be major/minor/patch")
        values["version"] = VersionRule(normalized, match, replacement, increment, rule.get("validate"))
    native = data.get("native", {})
    validate_fields(native, {"executables"})
    executables = native.get("executables", {})
    validate_fields(executables, {"claude", "codex", "cursor", "grok"})
    values["native_executables"] = {key: bounded_text(value, f"{key} executable", 4096) for key, value in executables.items()}
    config = Config(**values)
    if config.action_bytes > config.frame_bytes - 1024:
        raise CoordinationError("INVALID_ARGUMENT", "Action budget must fit the transport frame")
    return config
