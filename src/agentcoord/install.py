"""Portable project setup and explicitly managed macOS service installation."""
from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from importlib import resources
from pathlib import Path

HARNESSES = ("claude", "codex", "cursor", "grok")
EVENTS = {
    "claude": {"SessionStart": "start", "Stop": "stop", "StopFailure": "failure", "SessionEnd": "end", "SubagentStart": "child_start", "SubagentStop": "child_stop"},
    "codex": {"SessionStart": "start", "Stop": "stop", "Interrupt": "failure", "SessionEnd": "end"},
    "cursor": {"sessionStart": "start", "stop": "stop", "sessionEnd": "end"},
    "grok": {"SessionStart": "start", "Stop": "stop", "StopFailure": "failure", "StopCancelled": "failure", "SessionEnd": "end"},
}
BEGIN_MARKER = "<!-- agentcoord:begin -->"
END_MARKER = "<!-- agentcoord:end -->"


class InstallError(RuntimeError):
    """An installation boundary could not be verified safely."""


def no_links(path: Path) -> None:
    """State and publication targets cannot redirect writes through symlinks."""
    for part in (path, *path.parents):
        if part.is_symlink():
            raise InstallError(f"Symlink is not allowed for installation state: {part}")


def _publish(path: Path, content: bytes, original: bytes | None, mode: int = 0o600) -> None:
    no_links(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    current = path.read_bytes() if path.exists() else None
    if current != original:
        raise InstallError(f"File changed during installation: {path}")
    if current == content:
        return
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if (path.read_bytes() if path.exists() else None) != original:
            raise InstallError(f"File changed during installation: {path}")
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def integration_text(name: str) -> str:
    if name not in {"agents.md", "claude.md", "project.toml", "herdr-plugin.toml", "herdr.sh"}:
        raise InstallError(f"Unknown integration resource: {name}")
    return resources.files("agentcoord").joinpath("integrations", name).read_text(encoding="utf-8")


def instruction_content(original: str, kind: str = "agents") -> str:
    """Replace one generated block while retaining every other byte of prose."""
    body = integration_text(f"{kind}.md").rstrip("\n")
    block = f"{BEGIN_MARKER}\n{body}\n{END_MARKER}"
    starts, ends = original.count(BEGIN_MARKER), original.count(END_MARKER)
    if starts != ends or starts > 1:
        raise InstallError("Ambiguous agentcoord instruction markers; preserve the file and repair them")
    if starts:
        start, end = original.index(BEGIN_MARKER), original.index(END_MARKER)
        if end < start:
            raise InstallError("Reversed agentcoord instruction markers")
        return original[:start] + block + original[end + len(END_MARKER):]
    separator = "" if not original else "\n" if original.endswith("\n") else "\n\n"
    return original + separator + block + "\n"


def init_project(root: Path, *, apply: bool = False, instructions: bool = True) -> dict:
    """Initialize only requested project files; service and harness activation are separate."""
    root = Path(root).resolve(strict=True)
    targets = [(root / ".agentcoord.toml", integration_text("project.toml"))]
    if instructions:
        for name, kind in (("AGENTS.md", "agents"), ("CLAUDE.md", "claude")):
            path = root / name
            no_links(path)
            original = path.read_text(encoding="utf-8") if path.exists() else ""
            targets.append((path, instruction_content(original, kind)))
    planned = []
    for path, text in targets:
        no_links(path)
        original = path.read_bytes() if path.exists() else None
        # Existing project configuration is the user's authority, not a template to replace.
        updated = original if path.name == ".agentcoord.toml" and original is not None else text.encode("utf-8")
        planned.append((path, original, updated))
    result = {"root": str(root), "applied": apply, "files": [{"path": str(p), "changed": old != new} for p, old, new in planned]}
    if apply:
        # Validate all targets before publishing any file.
        for path, old, _ in planned:
            if (path.read_bytes() if path.exists() else None) != old:
                raise InstallError(f"File changed during initialization: {path}")
        for path, old, new in planned:
            _publish(path, new, old)
    return result


def mcp_config(harness: str, root: Path, executable: str = "agentcoord") -> dict:
    if harness not in HARNESSES:
        raise InstallError(f"Unsupported harness: {harness}")
    from .config import daemon_environment, discover_workspace
    workspace = discover_workspace(explicit_root=root)
    entry = {"command": executable, "args": ["--project", str(workspace.root), "mcp", "--harness", harness],
             "env": daemon_environment(workspace)}
    if harness == "codex":
        entry["env_vars"] = ["CODEX_THREAD_ID", "AGENTCOORD_CHILD_ID", "AGENTCOORD_NATIVE_RUN_ID"]
    return {"mcp_servers" if harness in {"codex", "grok"} else "mcpServers": {"agentcoord": entry}}


def hook_config(harness: str, root: Path, executable: str = "agentcoord") -> dict:
    if harness not in HARNESSES:
        raise InstallError(f"Unsupported harness: {harness}")
    from .config import daemon_environment, discover_workspace
    workspace = discover_workspace(explicit_root=root)
    environment = [f"{key}={value}" for key, value in daemon_environment(workspace).items()]
    hooks = {}
    for native_event, event in EVENTS[harness].items():
        command = shlex.join(["env", *environment, executable, "--project", str(workspace.root), "hook", harness, event])
        handler = {"type": "command", "command": command, "timeout": 15}
        hooks[native_event] = [handler] if harness == "cursor" else [{"hooks": [handler]}]
    return {"version": 1, "hooks": hooks} if harness == "cursor" else {"hooks": hooks}


def _toml_mcp(config: dict) -> str:
    entry = config["mcp_servers"]["agentcoord"]
    content = "[mcp_servers.agentcoord]\n" + "".join(
        f"{key} = {json.dumps(value, ensure_ascii=False)}\n" for key, value in entry.items() if key != "env")
    if "env" in entry:
        content += "\n[mcp_servers.agentcoord.env]\n" + "".join(
            f"{json.dumps(key)} = {json.dumps(value, ensure_ascii=False)}\n" for key, value in entry["env"].items())
    return content


def _herdr_python(executable: str) -> str:
    """Validate a Python runtime without pinning a disposable Homebrew keg."""
    candidates = []
    if Path(executable).is_absolute():
        candidates.append(Path(executable).parent / "python3")
    discovered = shutil.which("python3")
    if discovered:
        candidates.append(Path(discovered).absolute())
    for candidate in candidates:
        parts = candidate.parts
        if "Cellar" in parts:
            index = parts.index("Cellar")
            if len(parts) <= index + 3:
                continue
            candidate = Path(*parts[:index]) / "opt" / parts[index + 1] / Path(*parts[index + 3:])
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            continue
        try:
            probe = subprocess.run([str(candidate), "-I", "-c", "import json,sys;print(json.dumps(list(sys.version_info[:2])))"],
                                   capture_output=True, text=True, timeout=5, check=False)
            version = json.loads(probe.stdout)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0 and isinstance(version, list) and len(version) == 2 and all(type(part) is int for part in version) and version[0] == 3 and version[1] >= 11:
            return str(candidate)
    raise InstallError("Herdr requires a validated Python 3.11+ executable on PATH or beside agentcoord")


def generate_candidates(root: Path, destination: Path, *, executable: str = "agentcoord") -> dict:
    """Write inert fragments into an explicit separate directory, never live configuration."""
    from .config import daemon_environment, discover_workspace
    root = root.resolve(strict=True)
    workspace = discover_workspace(explicit_root=root)
    destination = destination.absolute()
    no_links(destination)
    destination = destination.resolve()
    live_dirs = {root / name for name in (".claude", ".codex", ".cursor", ".grok")}
    if destination == root or any(destination == path or path in destination.parents for path in live_dirs):
        raise InstallError("Candidate directory must be separate from active harness configuration")
    candidates: dict[str, bytes] = {}
    for harness in HARNESSES:
        config = mcp_config(harness, root, executable)
        suffix = "toml" if harness in {"codex", "grok"} else "json"
        content = _toml_mcp(config) if suffix == "toml" else json.dumps(config, ensure_ascii=False, indent=2) + "\n"
        candidates[f"{harness}-mcp.{suffix}"] = content.encode()
        candidates[f"{harness}-hooks.json"] = (json.dumps(hook_config(harness, root, executable), indent=2) + "\n").encode()
    for name in ("agents.md", "claude.md", "project.toml", "herdr-plugin.toml", "herdr.sh"):
        content = integration_text(name)
        if name == "herdr.sh":
            routing = daemon_environment(workspace)
            routing.pop("AGENTCOORD_WORKSPACE")
            environment = shlex.join(["env", *[f"{key}={value}" for key, value in routing.items()]])
            python = _herdr_python(executable)
            content = content.replace("@ENVIRONMENT@", environment).replace("@PYTHON@", shlex.quote(python)).replace("@EXECUTABLE@", shlex.quote(executable))
        candidates[name] = content.encode()
    # Exclusive publication prevents reruns silently overwriting review candidates.
    for name in candidates:
        path = destination / name
        no_links(path)
        if path.exists():
            raise InstallError(f"Candidate already exists: {path}")
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name, content in candidates.items():
        _publish(destination / name, content, None, 0o700 if name.endswith(".sh") else 0o600)
    return {"root": str(root), "destination": str(destination), "files": sorted(candidates), "activated": False}


def launchd_configuration(workspace, home: Path, executable: str) -> tuple[Path, Path, dict]:
    """Build a private unit for the installed executable, independent of source checkout layout."""
    root = Path(workspace.root).resolve(strict=True)
    home = home.absolute()
    located = shutil.which(executable) if not Path(executable).is_absolute() else executable
    executable_path = Path(located or executable).absolute()
    if not executable_path.is_file() or not os.access(executable_path, os.X_OK):
        raise InstallError(f"Missing installed executable: {executable_path}")
    # Homebrew's stable opt/bin symlink is intentionally allowed for executable resolution.
    identifier = hashlib.sha256(str(workspace.id).encode()).hexdigest()[:24]
    label = f"org.agentcoord.workspace.{identifier}"
    path = home / "Library/LaunchAgents" / f"{label}.plist"
    logs = Path(workspace.state_dir) / "logs"
    for target in (path, logs / "stdout.log", logs / "stderr.log"):
        no_links(target)
    from .config import daemon_environment
    desired = {"Label": label, "ProgramArguments": [str(executable_path), "--project", str(root), "serve"],
               "WorkingDirectory": str(root), "RunAtLoad": True,
               "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 30, "ExitTimeOut": 120,
               "EnvironmentVariables": {"PYTHONDONTWRITEBYTECODE": "1", "HOME": str(home), **daemon_environment(workspace)},
               "StandardOutPath": str(logs / "stdout.log"), "StandardErrorPath": str(logs / "stderr.log")}
    return path, logs, desired


def _running(probe) -> bool:
    return probe.returncode == 0 and re.search(r"(?m)^\s*state = running\s*$", probe.stdout) is not None


def _launch(runner, *args):
    return runner(["/bin/launchctl", *args], capture_output=True, text=True, timeout=15)


def _owned(path: Path, desired: dict) -> bytes | None:
    original = path.read_bytes() if path.exists() else None
    if original is not None:
        try:
            existing = plistlib.loads(original)
        except (ValueError, plistlib.InvalidFileException) as error:
            raise InstallError(f"Invalid managed service configuration: {path}") from error
        if not isinstance(existing, dict) or existing.get("Label") != desired["Label"] or existing.get("ProgramArguments") != desired["ProgramArguments"]:
            raise InstallError("Existing unit is not owned by this workspace and installed executable")
    return original


def _health(client, workspace) -> dict:
    result = client.health()
    if not isinstance(result, dict) or not result.get("ok") or not isinstance(result.get("data"), dict):
        raise InstallError("Cannot verify this workspace's service health")
    health = result["data"]
    if health.get("workspace_id") != workspace.id or type(health.get("protocol")) is not int or health["protocol"] != 1:
        raise InstallError("Service workspace/protocol identity does not match")
    if health.get("service_state") not in {"active", "draining", "quiescent", "upgrading"}:
        raise InstallError("Service lifecycle state is missing or invalid")
    for key in ("running_effects", "uncertain_effects"):
        if type(health.get(key)) is not int or health[key] < 0:
            raise InstallError(f"Service {key} count is missing or invalid")
    return health


def _drain(client, workspace, timeout: float = 15) -> bool:
    resume_active = _health(client, workspace)["service_state"] == "active"
    reply = client.drain(key=os.urandom(16).hex())
    if not reply.get("ok"):
        raise InstallError("Service refused safe drain; preserve the existing unit")
    deadline = time.monotonic() + timeout
    while True:
        health = _health(client, workspace)
        if health["uncertain_effects"]:
            raise InstallError("Uncertain external effects require reconciliation before service changes")
        if health.get("service_state") == "quiescent" and health.get("running_effects") == 0:
            return resume_active
        if time.monotonic() >= deadline:
            raise InstallError("Service did not become quiescent; inspect owned work before retrying")
        time.sleep(0.1)


def _require_macos(platform: str | None) -> None:
    if (sys.platform if platform is None else platform) != "darwin":
        raise InstallError("Managed services use macOS launchd; on Linux run agentcoord serve in the foreground")


def service_status(workspace, home: Path, executable: str, *, runner=subprocess.run, uid: int | None = None, platform: str | None = None) -> dict:
    _require_macos(platform)
    path, logs, desired = launchd_configuration(workspace, home, executable)
    original = _owned(path, desired)
    target = f"gui/{os.getuid() if uid is None else uid}/{desired['Label']}"
    probe = _launch(runner, "print", target)
    if probe.returncode not in {0, 113}:
        raise InstallError(f"Cannot inspect owned launchd service (exit {probe.returncode})")
    loaded = probe.returncode == 0
    if loaded and original is None:
        raise InstallError("Loaded unit has no owned on-disk configuration")
    return {"label": desired["Label"], "plist": str(path), "logs": str(logs), "loaded": loaded,
            "running": _running(probe), "configured": original is not None, "target": target}


def install_service(workspace, home: Path, executable: str, *, apply: bool = False, restart: bool = False,
                    client=None, runner=subprocess.run, uid: int | None = None, platform: str | None = None) -> dict:
    if restart and not apply:
        raise InstallError("Restart requires explicit application")
    report = service_status(workspace, home, executable, runner=runner, uid=uid, platform=platform)
    path, logs, desired = launchd_configuration(workspace, home, executable)
    original = _owned(path, desired)
    content = plistlib.dumps(desired)
    changed = original is None or plistlib.loads(original) != desired
    report.update(changed=changed, applied=apply)
    if not apply or (report["running"] and not changed and not restart):
        return report
    resume_active = False
    if report["loaded"]:
        if client is None:
            raise InstallError("A bound maintenance connection is required to drain the loaded service")
        resume_active = _drain(client, workspace)
        result = _launch(runner, "bootout", report["target"])
        if result.returncode:
            raise InstallError(f"Cannot unload owned service: {result.stderr.strip()}")
        _await_absent(runner, report["target"])
        client.close()
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(logs, 0o700)
    for name in ("stdout.log", "stderr.log"):
        fd = os.open(logs / name, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
    _publish(path, content, original)
    domain = report["target"].rsplit("/", 1)[0]
    result = _launch(runner, "bootstrap", domain, str(path))
    if result.returncode:
        raise InstallError(f"Cannot load owned service (exit {result.returncode}): {result.stderr.strip()}")
    deadline = time.monotonic() + 5
    while not _running(_launch(runner, "print", report["target"])):
        if time.monotonic() >= deadline:
            raise InstallError("Owned service is registered but did not become running; inspect its logs")
        time.sleep(0.1)
    report.update(configured=True, loaded=True, running=True)
    if resume_active:
        from . import __version__
        deadline = time.monotonic() + 15
        while True:
            try:
                health = _health(client, workspace)
            except InstallError:
                if time.monotonic() >= deadline:
                    raise InstallError("Restarted service did not provide verified workspace health")
                time.sleep(0.1)
                continue
            if health.get("release") != __version__ or health.get("database_state") != "ready" or health.get("maintenance_error"):
                raise InstallError("Restarted release is not healthy; preserve its maintenance fence")
            if health["uncertain_effects"] or health["running_effects"] or health["service_state"] != "quiescent":
                raise InstallError("Restarted service is not quiescent; preserve its maintenance fence")
            reply = client.activate(key=os.urandom(16).hex())
            if not reply.get("ok") or _health(client, workspace)["service_state"] != "active":
                raise InstallError("Restarted service refused activation; preserve its maintenance fence")
            report["reactivated"] = True
            break
    return report


def _await_absent(runner, target: str) -> None:
    deadline = time.monotonic() + 5
    while True:
        probe = _launch(runner, "print", target)
        if probe.returncode == 113:
            return
        if probe.returncode and probe.returncode != 113:
            raise InstallError("Cannot verify that the owned service unloaded")
        if time.monotonic() >= deadline:
            raise InstallError("Owned service did not finish unloading")
        time.sleep(0.1)


def remove_service(workspace, home: Path, executable: str, *, apply: bool = False, client=None,
                   runner=subprocess.run, uid: int | None = None, platform: str | None = None) -> dict:
    report = service_status(workspace, home, executable, runner=runner, uid=uid, platform=platform)
    path, _, desired = launchd_configuration(workspace, home, executable)
    original = _owned(path, desired)
    report["applied"] = apply
    if not apply:
        return report
    if report["loaded"]:
        if client is None:
            raise InstallError("A maintenance connection is required before service removal")
        _drain(client, workspace)
        result = _launch(runner, "bootout", report["target"])
        if result.returncode:
            raise InstallError(f"Cannot unload owned service: {result.stderr.strip()}")
        _await_absent(runner, report["target"])
    no_links(path)
    if (path.read_bytes() if path.exists() else None) != original:
        raise InstallError("Unit changed during removal; preserve it")
    if original is not None:
        path.unlink()
    report.update(configured=False, loaded=False, running=False)
    # Persistent data/logs remain available for explicit backup/recovery.
    return report
