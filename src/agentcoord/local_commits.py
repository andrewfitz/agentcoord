"""Service-independent exact-file commits with durable local retry receipts."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import tempfile
from pathlib import Path

from . import commits
from .config import load_config
from .core import CoordinationError
from .store import private_directory
from .transport import error_envelope


def _regular(path, flags):
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        os.close(fd)
        raise ValueError("Local commit receipt must be private owned regular state")
    return fd


def _write(path, value):
    # A sibling temporary file and directory fsync make every phase durable.
    fd, name = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        if path.is_symlink():
            raise ValueError("Local commit receipt must not be a symlink")
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


def _reviewed_arguments(arguments):
    normalized = dict(arguments, paths=commits._paths(arguments["paths"]))
    for field in ("bump_version", "adopt_staged"):
        normalized.setdefault(field, False)
    return normalized


def _native_risk(workspace, paths, key, warn, arguments):
    """Read existing authority only; absence of a service is not an admission."""
    path = workspace.database_path
    if path is None:
        return
    if not path.exists():
        if path.is_symlink():
            raise ValueError("Coordination database symlink cannot establish Git safety")
        return
    if path.is_symlink() or not path.is_file():
        raise ValueError("Coordination database is not regular state; inspect accepted native Git work")
    try:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"operations", "commit_admissions", "commit_paths"} <= tables:
                warn("Existing database has no current native commit admission tables; continuing independent Git checks.")
                return
            rows = db.execute("SELECT id,state,result_json,arguments_json FROM operations WHERE kind='commit.execute' AND retry_key=?", (key,)).fetchall()
            if "idempotency" in tables:
                known_ids = {row[0] for row in rows}
                for (receipt_json,) in db.execute("SELECT result_json FROM idempotency WHERE operation='commit.execute' AND retry_key=?", (key,)):
                    accepted = json.loads(receipt_json)
                    operation_id = accepted.get("operation_id")
                    if operation_id and operation_id not in known_ids:
                        operation = db.execute("SELECT id,state,result_json,arguments_json FROM operations WHERE id=? AND kind='commit.execute'", (operation_id,)).fetchone()
                        if operation is None:
                            raise CoordinationError("RECONCILIATION_REQUIRED", "Native commit retry receipt has no recoverable operation", next_action="Inspect the native receipt and Git publication before local execution")
                        rows.append(operation)
                        known_ids.add(operation_id)
                    elif not operation_id and accepted.get("admission_id"):
                        raise CoordinationError("RECONCILIATION_REQUIRED", "Native retry receipt holds a commit grant without a settled operation", next_action="Inspect the native admission and exact selected Git state")
            if len(rows) == 1 and rows[0][1] == "succeeded" and rows[0][2]:
                native_args = json.loads(rows[0][3])
                if _reviewed_arguments(native_args) != _reviewed_arguments(arguments):
                    raise CoordinationError("IDEMPOTENCY_CONFLICT", "Native retry key names different reviewed commit arguments")
                result = json.loads(rows[0][2])
                if result.get("committed") and result.get("commit"):
                    return {"ok": True, "protocol": 1, "data": {"state": "succeeded", "result": result, "mode": "native-recovered"}}
            if rows and "commit_execution" in tables:
                rejected = True
                for operation_id, status, _, _ in rows:
                    execution = db.execute("SELECT published_commit,prepared_json FROM commit_execution WHERE operation_id=?", (operation_id,)).fetchone()
                    prepared = json.loads(execution[1]) if execution else {}
                    if (status not in {"failed", "cancelled"} or not execution
                            or execution[0] or not prepared.get("worker_finished")
                            or prepared.get("phase") in {"publishing", "published", "reconciled"}):
                        rejected = False
                if rejected:
                    rows = []
            if rows:
                raise CoordinationError(
                    "RECONCILIATION_REQUIRED", "The retry key already has a native commit receipt",
                    details={"operation_ids": [row[0] for row in rows]},
                    next_action="Inspect the native operation receipt and Git publication; do not create a second commit",
                )
            selected = set(paths)
            for admission, path in db.execute(
                "SELECT a.id,p.path FROM commit_admissions a JOIN commit_paths p ON p.admission_id=a.id "
                "WHERE a.state IN ('pending','granted','uncertain')"
            ):
                if path in selected:
                    raise CoordinationError(
                        "RECONCILIATION_REQUIRED", "A native commit admission overlaps an exact selected path",
                        details={"admission_id": admission, "path": path},
                        next_action="Resolve the overlapping Git owner before publishing these files locally",
                    )
    except sqlite3.Error:
        warn("Existing coordination database could not be read; this is not evidence about prior publication. Continuing independent exact-file Git checks without changing that database.")


class _Control:
    def __init__(self, check, key):
        self.check, self.grant_id = check, "local-" + hashlib.sha256(key.encode()).hexdigest()

    def reserve(self):
        self.check()
        return {"granted": True, "grant_id": self.grant_id, "mode": "local"}

    def status(self):
        self.check()
        return {"held_by_you": True, "grant_id": self.grant_id}

    def check_wait(self, grant_id):
        if grant_id != self.grant_id:
            raise ValueError("Local commit control changed")
        self.check()

    def release(self, grant_id):
        if grant_id != self.grant_id:
            raise ValueError("Local commit control changed")
        return {"released": True, "mode": "local"}


def execute(workspace, args, key, report=None):
    """Return an envelope; report receives human-readable Git progress strings."""
    emit = report or (lambda message: None)
    receipt = None
    state = None
    try:
        if any(os.environ.get(name) for name in ("GIT_INDEX_FILE", "GIT_DIR", "GIT_WORK_TREE")):
            raise ValueError("Remove Git repository/index overrides before local commit execute")
        if not isinstance(key, str) or not key or len(key.encode()) > 256:
            raise ValueError("Local commit requires a stable retry key of 1 to 256 bytes")
        commits._fields(args, {"paths", "message", "bump_version", "adopt_staged", "patch", "patch_file", "patch_sha256", "base_commit"}, {"paths", "message"})
        paths = commits._paths(args["paths"])
        config = load_config(workspace)
        patch = commits._read_patch(workspace.root, args)
        fingerprint = hashlib.sha256(commits._json({
            "arguments": dict(args, paths=paths),
            "patch_sha256": hashlib.sha256(patch).hexdigest() if patch is not None else None,
            "version": vars(config.version) if config.version is not None else None,
        }).encode()).hexdigest()
        git_dir = Path(subprocess.run(
            ["git", "rev-parse", "--absolute-git-dir"], cwd=workspace.root,
            capture_output=True, check=True,
        ).stdout.decode().strip())
        directory = git_dir / "agentcoord-local-commits"
        private_directory(directory)
        receipt = directory / (hashlib.sha256(key.encode()).hexdigest() + ".json")
        lock_fd = _regular(receipt.with_suffix(".lock"), os.O_CREAT | os.O_RDWR)
        with os.fdopen(lock_fd, "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise CoordinationError("RECONCILIATION_REQUIRED", "This local retry key is already executing", next_action="Inspect the local Git receipt; do not start another publication") from None
            if receipt.exists() or receipt.is_symlink():
                with os.fdopen(_regular(receipt, os.O_RDONLY)) as stream:
                    prior = json.load(stream)
                if prior.get("fingerprint") != fingerprint:
                    raise CoordinationError("IDEMPOTENCY_CONFLICT", "Local retry key was reused for different reviewed content")
                if "envelope" in prior:
                    emit("Reusing durable local commit receipt; no Git operation repeated.")
                    return prior["envelope"]
                return error_envelope("RECONCILIATION_REQUIRED", "Interrupted local commit requires Git inspection", details={"phase": prior.get("phase"), "receipt": str(receipt), "prepared_commit": prior.get("prepared_commit"), "published_commit": prior.get("published_commit")}, next_action="Inspect HEAD, reflog and the selected shared-index entries against this receipt; do not repeat publication")
            warned = False

            def warn(message):
                nonlocal warned
                if not warned:
                    emit(message)
                    warned = True

            def check():
                if warned:
                    return
                recovered = _native_risk(workspace, paths, key, warn, dict(args, paths=paths))
                if recovered is not None:
                    raise CoordinationError("RECONCILIATION_REQUIRED", "Native publication appeared during local preparation", next_action="Use the native published receipt; do not create another commit")

            recovered = _native_risk(workspace, paths, key, warn, dict(args, paths=paths))
            if recovered is not None:
                emit("Reusing succeeded native Git publication; no local Git operation repeated.")
                return recovered
            state = {"fingerprint": fingerprint, "phase": "selected", "paths": paths}
            _write(receipt, state)
            emit("Local commit: selected exact files; preserving peer staging through a private Git index.")

            def progress(phase, values):
                state.update(values, phase=phase)
                _write(receipt, state)
                emit(f"Local Git phase {phase}: " + (str(values.get("published_commit") or values.get("prepared_commit")) if values.get("published_commit") or values.get("prepared_commit") else "reviewed private-index preparation"))

            try:
                result = commits._execute_git(
                    workspace.root, paths, args["message"], args.get("bump_version", False),
                    _Control(check, key), progress, config.version,
                    adopt_staged=args.get("adopt_staged", False), patch=patch,
                    base_commit=args.get("base_commit"),
                )
                result["mode"] = "local"
                result["receipt"] = str(receipt)
                if not result.get("committed"):
                    envelope = error_envelope(
                        "GIT_HOOK_FAILED" if result.get("hook") else "COMMIT_REJECTED",
                        "Git commit hook rejected the commit" if result.get("hook") else "Git did not publish the selected commit",
                        details={"hook": result.get("hook"), "exit_code": result.get("code"), "publication": "not_published", "receipt": str(receipt)},
                        next_action=result.get("next_action"),
                    )
                    envelope["data"] = {"state": "failed", "result": result, "mode": "local", "receipt": str(receipt)}
                elif result.get("code"):
                    envelope = error_envelope(
                        "RECONCILIATION_REQUIRED", "Git published the commit but local completion requires inspection",
                        details={"commit": result.get("commit"), "publication": "published", "receipt": str(receipt), "receipt_error": result.get("receipt_error")},
                        next_action="Git already published this commit. Inspect its receipt and shared index; do not retry publication.",
                    )
                    envelope["data"] = {"state": "uncertain", "result": result, "mode": "local", "receipt": str(receipt)}
                else:
                    envelope = {"ok": True, "protocol": 1, "data": {"state": "succeeded", "result": result, "mode": "local", "receipt": str(receipt)}}
                state.update(envelope=envelope, phase="finished")
                _write(receipt, state)
                emit(f"Local Git result: commit {result['commit']} (exit {result.get('code')}); durable receipt {receipt}." if result.get("committed") else f"Local Git result: no publication; {result.get('hook', 'Git selection')} refused (exit {result.get('code')}); durable receipt {receipt}.")
                return envelope
            except (OSError, ValueError, CoordinationError) as error:
                if state.get("published_commit") or state.get("phase") in {"publishing", "published", "reconciled", "finished"}:
                    return error_envelope("RECONCILIATION_REQUIRED", str(error), details={"receipt": str(receipt), "phase": state["phase"]}, next_action="Inspect HEAD, reflog and the shared index using the durable local receipt; do not repeat publication")
                envelope = error_envelope(error.code if isinstance(error, CoordinationError) else "COMMIT_REJECTED", str(error), next_action="Review selected Git paths and correct the concrete failure before a new reviewed request")
                state.update(envelope=envelope, phase="rejected")
                _write(receipt, state)
                return envelope
    except (OSError, ValueError, CoordinationError, subprocess.CalledProcessError) as error:
        return error_envelope(error.code if isinstance(error, CoordinationError) else "COMMIT_REJECTED", str(error), next_action=getattr(error, "next_action", None) or "Inspect selected Git state and any accepted native receipts before local publication", details=getattr(error, "details", None))
