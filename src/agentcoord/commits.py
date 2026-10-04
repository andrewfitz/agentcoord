"""Exact-path Git admission, private-index publication and durable recovery."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from .core import CoordinationError, Operation

# Admission waits tolerate the measured short native handoff, but a stopped
# holder cannot retain a slow worker forever. This bound never expires a grant.
HANDOFF_WAIT_SECONDS = 30.0
HANDOFF_CHECK_SECONDS = 0.1

SCHEMA = (
    """CREATE TABLE commit_admissions (
        id TEXT PRIMARY KEY, actor_id TEXT NOT NULL REFERENCES actors(id),
        task_generation TEXT NOT NULL, mode TEXT NOT NULL CHECK(mode IN ('manual','native')),
        state TEXT NOT NULL CHECK(state IN ('pending','granted','released','cancelled','uncertain')),
        created_us INTEGER NOT NULL, expires_us INTEGER NOT NULL, grant_id TEXT)""",
    """CREATE TABLE commit_paths (
        admission_id TEXT NOT NULL REFERENCES commit_admissions(id), path TEXT NOT NULL,
        PRIMARY KEY(admission_id,path))""",
    """CREATE UNIQUE INDEX commit_one_live_admission ON commit_admissions(actor_id)
        WHERE state IN ('pending','granted','uncertain')""",
    """CREATE TABLE commit_grants (
        id TEXT PRIMARY KEY, admission_id TEXT NOT NULL REFERENCES commit_admissions(id),
        owner_identity_json TEXT NOT NULL CHECK(json_valid(owner_identity_json)),
        state TEXT NOT NULL CHECK(state IN ('active','released','uncertain')),
        created_us INTEGER NOT NULL, released_us INTEGER)""",
    """CREATE TABLE commit_execution (
        operation_id TEXT PRIMARY KEY REFERENCES operations(id),
        admission_id TEXT NOT NULL REFERENCES commit_admissions(id),
        base_commit TEXT, selection TEXT NOT NULL CHECK(selection IN ('files','patch')),
        patch_sha256 TEXT, prepared_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(prepared_json)),
        published_commit TEXT, reconciliation_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(reconciliation_json)))""",
    "CREATE INDEX commit_admission_order ON commit_admissions(state,created_us,id)",
)


def _json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _error(code: str, message: str) -> CoordinationError:
    return CoordinationError(code, message)


def _fields(
    arguments: dict, allowed: set[str], required: set[str] = frozenset()
) -> None:
    if set(arguments) - allowed or required - set(arguments):
        raise _error("INVALID_ARGUMENT", "Unknown or missing commit arguments")


def _paths(values: object) -> list[str]:
    if not isinstance(values, list) or not values or len(values) > 10000:
        raise _error(
            "INVALID_ARGUMENT", "Declare a nonempty bounded list of exact files"
        )
    result = []
    for value in values:
        if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
            raise _error(
                "INVALID_ARGUMENT", "Commit paths must be workspace-relative segments"
            )
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or ".." in path.parts
            or not path.parts
            or path.parts[0] == ".git"
        ):
            raise _error(
                "INVALID_ARGUMENT", "Git metadata and escaping paths cannot be selected"
            )
        result.append(path.as_posix())
    return sorted(set(result))


def _actor(service, context, tx) -> dict:
    actor = service.require_actor(tx, context, active=True)
    service.require_generation(
        tx, actor["id"], context.task_generation, context.execution_generation
    )
    if actor["reported_state"] in ("paused", "completed"):
        raise _error(
            "NOT_AUTHORIZED", "Paused or completed actors cannot acquire Git authority"
        )
    return actor


def _admission_paths(tx, admission_id: str) -> list[str]:
    return [
        row[0]
        for row in tx.connection.execute(
            "SELECT path FROM commit_paths WHERE admission_id=? ORDER BY path",
            (admission_id,),
        )
    ]


def _conflict(tx, left, right) -> bool:
    return (
        left["mode"] == "manual"
        or right["mode"] == "manual"
        or bool(
            set(_admission_paths(tx, left["id"]))
            & set(_admission_paths(tx, right["id"]))
        )
    )


def status(tx, actor_id: str) -> dict:
    rows = list(
        tx.connection.execute(
            "SELECT * FROM commit_admissions WHERE state IN ('pending','granted','uncertain') ORDER BY created_us,id"
        )
    )
    own = next((row for row in rows if row["actor_id"] == actor_id), None)
    blockers = []
    if own:
        for row in rows:
            if row["actor_id"] == actor_id or not _conflict(tx, own, row):
                continue
            held = row["state"] in ("granted", "uncertain")
            if not held and row["expires_us"] <= tx.now_us:
                continue
            if own["mode"] == "native" and row["mode"] == "manual" and not held:
                continue
            if held or (row["created_us"], row["id"]) < (own["created_us"], own["id"]):
                blockers.append(row["id"])
    owners = [
        {
            "actor_id": row["actor_id"],
            "admission_id": row["id"],
            "grant_id": row["grant_id"],
            "mode": row["mode"],
            "state": row["state"],
        }
        for row in rows
        if row["state"] in ("granted", "uncertain")
    ]
    return {
        "admission_id": own["id"] if own else None,
        "grant_id": own["grant_id"] if own else None,
        "held_by_you": bool(own and own["state"] == "granted"),
        "eligible_to_acquire": bool(
            own
            and own["state"] == "pending"
            and not blockers
            and own["expires_us"] > tx.now_us
        ),
        "blocking_requests": blockers,
        "owners": owners,
        "next_action": "reconcile_publication"
        if own and own["state"] == "uncertain"
        else "continue_independent_work"
        if blockers
        else "commit_or_release"
        if own and own["state"] == "granted"
        else "acquire_when_ready",
    }


def admit(tx, context, *, mode: str, paths: list[str], owner_identity: dict) -> dict:
    """Grant only conflicting content scope; granted owners never expire by TTL."""
    if mode not in ("native", "manual") or (mode == "manual" and paths):
        raise _error(
            "INVALID_ARGUMENT",
            "Manual admission is exclusive; native admission names exact paths",
        )
    own = tx.connection.execute(
        "SELECT * FROM commit_admissions WHERE actor_id=? AND state IN "
        "('pending','granted','uncertain')",
        (context.actor_id,),
    ).fetchone()
    if (
        own
        and own["state"] == "pending"
        and (
            own["expires_us"] <= tx.now_us
            or own["task_generation"] != context.task_generation
        )
    ):
        tx.connection.execute(
            "UPDATE commit_admissions SET state='cancelled' WHERE id=?", (own["id"],)
        )
        tx.event(
            "commits",
            "cancelled",
            own["id"],
            context.actor_id,
            {"reason": "obsolete_pending_request"},
        )
        own = None
    if own:
        if own["task_generation"] != context.task_generation:
            raise _error(
                "STALE_GENERATION",
                "Previous-task Git authority requires reconciliation",
            )
        if own["mode"] != mode or _admission_paths(tx, own["id"]) != paths:
            raise _error(
                "RECONCILIATION_REQUIRED",
                "Release the exact existing grant before changing its scope",
            )
        if own["state"] == "uncertain":
            raise _error(
                "RECONCILIATION_REQUIRED", "The existing Git effect is uncertain"
            )
        admission_id = own["id"]
        if own["state"] == "pending":
            tx.connection.execute(
                "UPDATE commit_admissions SET expires_us=? WHERE id=?",
                (tx.now_us + 600_000_000, admission_id),
            )
    else:
        admission_id = str(uuid.uuid4())
        tx.connection.execute(
            "INSERT INTO commit_admissions VALUES (?,?,?,?,?,?,?,?)",
            (
                admission_id,
                context.actor_id,
                context.task_generation,
                mode,
                "pending",
                tx.now_us,
                tx.now_us + 600_000_000,
                None,
            ),
        )
        tx.connection.executemany(
            "INSERT INTO commit_paths VALUES (?,?)",
            ((admission_id, path) for path in paths),
        )
        tx.event("commits", "pending", admission_id, context.actor_id, {"mode": mode})
    result = status(tx, context.actor_id)
    if result["held_by_you"]:
        if mode == "manual":
            previous_owner = json.loads(
                tx.connection.execute(
                    "SELECT owner_identity_json FROM commit_grants WHERE id=?",
                    (result["grant_id"],),
                ).fetchone()[0]
            )
            if (previous_owner.get("pid"), previous_owner.get("started")) != (
                owner_identity.get("pid"),
                owner_identity.get("started"),
            ):
                raise _error(
                    "RECONCILIATION_REQUIRED",
                    "Previous native process still owns this manual Git grant",
                )
        return {**result, "granted": True}
    if not result["eligible_to_acquire"]:
        return {**result, "granted": False}
    grant_id = str(uuid.uuid4())
    tx.connection.execute(
        "INSERT INTO commit_grants VALUES (?,?,?,?,?,?)",
        (grant_id, admission_id, _json(owner_identity), "active", tx.now_us, None),
    )
    tx.connection.execute(
        "UPDATE commit_admissions SET state='granted',grant_id=? WHERE id=?",
        (grant_id, admission_id),
    )
    tx.event(
        "commits",
        "granted",
        grant_id,
        context.actor_id,
        {"admission_id": admission_id, "mode": mode},
    )
    return {**status(tx, context.actor_id), "granted": True}


def release_exact(
    tx, actor_id: str, grant_id: str, *, reconciled: bool = False
) -> dict:
    row = tx.connection.execute(
        "SELECT g.*,a.actor_id FROM commit_grants g JOIN commit_admissions a "
        "ON a.id=g.admission_id WHERE g.id=?",
        (grant_id,),
    ).fetchone()
    if row is None or row["actor_id"] != actor_id:
        raise _error(
            "NOT_AUTHORIZED",
            "The exact grant belongs to another actor or does not exist",
        )
    if row["state"] == "released":
        return {"released": False, "grant_id": grant_id, "already_released": True}
    active = tx.connection.execute(
        "SELECT o.id FROM commit_execution e JOIN operations o ON o.id=e.operation_id "
        "WHERE e.admission_id=? AND o.state IN ('queued','running','uncertain') LIMIT 1",
        (row["admission_id"],),
    ).fetchone()
    if active and not reconciled:
        raise _error(
            "RECONCILIATION_REQUIRED",
            "An unresolved Git execution retains this exact grant",
        )
    tx.connection.execute(
        "UPDATE commit_grants SET state='released',released_us=? WHERE id=?",
        (tx.now_us, grant_id),
    )
    tx.connection.execute(
        "UPDATE commit_admissions SET state='released' WHERE id=?",
        (row["admission_id"],),
    )
    tx.event("commits", "released", grant_id, actor_id, {})
    released = tx.connection.execute(
        "SELECT * FROM commit_admissions WHERE id=?", (row["admission_id"],)
    ).fetchone()
    _notify_eligible(tx, released)
    return {"released": True, "grant_id": grant_id}


def _notify_eligible(tx, released):
    for pending in tx.connection.execute(
        "SELECT * FROM commit_admissions WHERE state='pending' AND expires_us>?",
        (tx.now_us,),
    ).fetchall():
        if (
            _conflict(tx, released, pending)
            and status(tx, pending["actor_id"])["eligible_to_acquire"]
        ):
            tx.event("commits", "eligible", pending["id"], pending["actor_id"], {})


def _status(service, context, arguments, tx):
    _fields(arguments, set())
    actor = service.require_actor(tx, context)
    return status(tx, actor["id"])


def _acquire(service, context, arguments, tx):
    _fields(arguments, set())
    actor = _actor(service, context, tx)
    execution = tx.connection.execute(
        "SELECT process_identity_json FROM executions WHERE generation=? AND actor_id=?",
        (context.execution_generation, actor["id"]),
    ).fetchone()
    owner = json.loads(execution[0]) if execution and execution[0] else {}
    if not owner.get("started"):
        raise _error(
            "NOT_AUTHORIZED",
            "Manual Git admission needs a verified native process-start identity",
        )
    return admit(tx, context, mode="manual", paths=[], owner_identity=owner)


def _release(service, context, arguments, tx):
    _fields(arguments, {"grant_id"}, {"grant_id"})
    actor = service.require_actor(tx, context)
    grant = arguments["grant_id"]
    if not isinstance(grant, str) or not grant:
        raise _error("INVALID_ARGUMENT", "An exact grant ID is required")
    return release_exact(tx, actor["id"], grant)


def _cancel(service, context, arguments, tx):
    """Cancel one ungranted request; exact live grants retain their own release."""
    from .core import identifier

    _fields(arguments, {"admission_id"}, {"admission_id"})
    admission_id = identifier(arguments["admission_id"], "admission_id")
    actor = service.require_actor(tx, context)
    if context.identity_mode == "shared_group":
        raise _error(
            "NOT_AUTHORIZED",
            "Pending Git cancellation requires a native-owner connection",
        )
    row = tx.connection.execute(
        "SELECT * FROM commit_admissions WHERE id=? AND actor_id=?",
        (admission_id, actor["id"]),
    ).fetchone()
    if row is None:
        raise _error(
            "NOT_AUTHORIZED",
            "Pending Git admission belongs to another actor or does not exist",
        )
    if row["state"] == "cancelled":
        return {
            "cancelled": False,
            "already_cancelled": True,
            "admission_id": admission_id,
        }
    if row["state"] != "pending" or row["grant_id"] is not None:
        raise _error(
            "RECONCILIATION_REQUIRED", "Release or reconcile the exact live Git grant"
        )
    tx.connection.execute(
        "UPDATE commit_admissions SET state='cancelled' WHERE id=? AND state='pending'",
        (admission_id,),
    )
    tx.event("commits", "cancelled", admission_id, actor["id"], {})
    _notify_eligible(tx, row)
    return {"cancelled": True, "admission_id": admission_id}


def _execute(service, context, arguments, tx):
    _fields(
        arguments,
        {
            "paths",
            "message",
            "bump_version",
            "adopt_staged",
            "patch",
            "patch_file",
            "patch_sha256",
            "base_commit",
        },
        {"paths", "message"},
    )
    _actor(service, context, tx)
    normalized = dict(arguments, paths=_paths(arguments["paths"]))
    message = normalized["message"]
    if (
        not isinstance(message, str)
        or not message.strip()
        or "\x00" in message
        or len(message.encode()) > 65536
    ):
        raise _error(
            "INVALID_ARGUMENT", "A nonempty bounded commit message is required"
        )
    for name in ("bump_version", "adopt_staged"):
        if name in normalized and type(normalized[name]) is not bool:
            raise _error("INVALID_ARGUMENT", name + " must be boolean")
    patch, patch_file, base = (
        normalized.get("patch"),
        normalized.get("patch_file"),
        normalized.get("base_commit"),
    )
    has_patch = patch is not None or patch_file is not None
    if has_patch != (base is not None) or (
        patch is not None and patch_file is not None
    ):
        raise _error(
            "INVALID_ARGUMENT", "A reviewed patch requires its full base commit ID"
        )
    if has_patch and (
        not isinstance(base, str)
        or not re.fullmatch("[0-9a-f]{40}|[0-9a-f]{64}", base)
        or normalized.get("adopt_staged")
    ):
        raise _error(
            "INVALID_ARGUMENT",
            "Invalid reviewed patch/base or incompatible staged adoption",
        )
    if patch is not None and (
        not isinstance(patch, str)
        or not patch
        or len(patch.encode()) > 16 * 1024 * 1024
    ):
        raise _error(
            "INVALID_ARGUMENT", "Inline patch must be nonempty bounded UTF-8 text"
        )
    if patch_file is not None:
        normalized["patch_file"] = _paths([patch_file])[0]
        if not isinstance(normalized.get("patch_sha256"), str) or not re.fullmatch(
            "[0-9a-f]{64}", normalized["patch_sha256"]
        ):
            raise _error("INVALID_ARGUMENT", "Patch file requires the reviewed SHA256")
    elif "patch_sha256" in normalized:
        raise _error("INVALID_ARGUMENT", "Reviewed file hash requires a patch file")
    grant = admit(
        tx, context, mode="native", paths=normalized["paths"], owner_identity={}
    )
    if not grant["granted"]:
        return grant
    existing = tx.connection.execute(
        "SELECT o.id FROM operations o JOIN commit_execution e ON e.operation_id=o.id "
        "WHERE e.admission_id=? AND o.state IN ('queued','running','uncertain') LIMIT 1",
        (grant["admission_id"],),
    ).fetchone()
    if existing:
        raise CoordinationError(
            "RECONCILIATION_REQUIRED",
            "The exact Git grant already owns an unresolved operation",
            details={"operation_id": existing["id"]},
            next_action="operation.get",
        )
    operation = service.enqueue(
        tx, context, "commit.execute", normalized, key=str(uuid.uuid4())
    )
    operation_id = operation["operation_id"]
    tx.connection.execute(
        "INSERT INTO commit_execution(operation_id,admission_id,selection,patch_sha256) VALUES (?,?,?,?)",
        (
            operation_id,
            grant["admission_id"],
            "patch" if has_patch else "files",
            normalized["patch_sha256"]
            if patch_file is not None
            else hashlib.sha256(patch.encode()).hexdigest()
            if patch is not None
            else None,
        ),
    )
    return {
        **operation,
        "grant_id": grant["grant_id"],
        "admission_id": grant["admission_id"],
    }


def _reconcile(service, context, arguments, tx):
    _fields(arguments, {"operation_id"}, {"operation_id"})
    actor = _actor(service, context, tx)
    row = tx.connection.execute(
        "SELECT actor_id,state FROM operations WHERE id=? AND kind=?",
        (arguments["operation_id"], "commit.execute"),
    ).fetchone()
    if row is None:
        raise _error("NOT_FOUND", "Git execution does not exist")
    if row["actor_id"] != actor["id"]:
        raise _error(
            "NOT_AUTHORIZED", "Only the originating actor may reconcile its Git effect"
        )
    return service.enqueue(
        tx,
        context,
        "commit.reconcile",
        {"operation_id": arguments["operation_id"]},
        key=str(uuid.uuid4()),
    )


def operations() -> tuple[Operation, ...]:
    return (
        Operation("commit.status", _status, False, False, True),
        Operation("commit.acquire", _acquire, True, True, True),
        Operation("commit.release", _release, True, True, True),
        Operation("commit.cancel", _cancel, True, True, True),
        Operation("commit.execute", _execute, True, True, True),
        Operation("commit.reconcile", _reconcile, True, True, True),
    )


def _action_query(context, filters, new_only):
    from .core import normalize_paths, validate_fields

    filters = dict(filters or {})
    validate_fields(filters, {"actor_id", "task", "path"})
    actor_id = filters.get("actor_id") if context.operator else context.actor_id
    if not context.operator and filters.get("actor_id", actor_id) != actor_id:
        return "SELECT NULL AS id WHERE 0", []
    path = (
        normalize_paths([filters["path"]], allow_root=True)[0]
        if filters.get("path")
        else None
    )
    if path == ".":
        path = None
    query = """SELECT q.* FROM (SELECT a.*,s.task,
        (SELECT COALESCE(MAX(e.sequence),0) FROM events e WHERE e.domain='commits'
         AND e.record_id IN (a.id,a.grant_id)) AS sequence FROM commit_admissions a
        JOIN assignments s ON s.generation=a.task_generation
        WHERE a.state IN ('pending','granted','uncertain')
        AND (a.state!='pending' OR a.expires_us>?)
        AND (? IS NULL OR a.actor_id=?) AND (? IS NULL OR s.task=?)
        AND (? IS NULL OR EXISTS (SELECT 1 FROM commit_paths p WHERE p.admission_id=a.id
            AND (p.path=? OR substr(p.path,1,length(?)+1)=?||'/')))) q WHERE 1=1"""
    arguments = [
        actor_id,
        actor_id,
        filters.get("task"),
        filters.get("task"),
        path,
        path,
        path,
        path,
    ]
    if new_only:
        query += """ AND NOT EXISTS (SELECT 1 FROM action_presentations v WHERE v.actor_id=?
            AND v.kind='commit_admission' AND v.record_id=q.id AND v.version=q.sequence)"""
        arguments.append(context.actor_id)
    return query, arguments


def select_actions(tx, context, *, after=None, limit=20, new_only=False, filters=None):
    """Admission actions complement core's terminal operation failures."""
    from .core import integer

    integer(limit, "limit", 1, 100)
    query, arguments = _action_query(context, filters, new_only)
    if query == "SELECT NULL AS id WHERE 0":
        return []
    query += " AND (? IS NULL OR q.sequence>?) ORDER BY q.sequence,q.id LIMIT ?"
    rows = tx.connection.execute(
        query, [tx.now_us, *arguments, after, after, limit]
    ).fetchall()
    result = []
    for row in rows:
        current = status(tx, row["actor_id"])
        result.append(
            {
                "kind": "commit_admission",
                "id": row["id"],
                "version": row["sequence"],
                "sequence": row["sequence"],
                "state": row["state"],
                "actor_id": row["actor_id"],
                "summary": "Git admission is "
                + ("eligible" if current["eligible_to_acquire"] else row["state"]),
                "message_ids": [],
                "next_action": current["next_action"],
            }
        )
    return result


def count_pending(tx, context, *, new_only=False, filters=None):
    query, arguments = _action_query(context, filters, new_only)
    if query == "SELECT NULL AS id WHERE 0":
        return {"commit_admissions": 0}
    return {
        "commit_admissions": tx.connection.execute(
            "SELECT COUNT(*) FROM (" + query + ")",
            [tx.now_us, *arguments],
        ).fetchone()[0]
    }


def _encode_records(records: dict[str, bytes]) -> dict[str, str]:
    return {
        name: base64.b64encode(value).decode("ascii") for name, value in records.items()
    }


def synthesize_version(content: bytes, rule) -> bytes:
    """Apply one configured anchored increment; no repository version authority."""
    pattern = rule.match
    if not isinstance(pattern, str) or "^" not in pattern or "$" not in pattern:
        raise ValueError("Version match must be anchored")
    text = content.decode("utf-8")
    matches = list(re.finditer(pattern, text, re.MULTILINE))
    if len(matches) != 1:
        raise ValueError("Version match must select exactly one record")
    match = matches[0]
    values = match.groupdict()
    increment = rule.increment
    if increment not in ("value", "major", "minor", "patch") or increment not in values:
        raise ValueError("Version increment must name an integer capture")
    for key, value in values.items():
        if value is None or not re.fullmatch("[0-9]+", value):
            raise ValueError("Version captures must be nonnegative integers")
        values[key] = int(value)
    values[increment] += 1
    if increment == "major":
        for key in ("minor", "patch"):
            if key in values:
                values[key] = 0
    elif increment == "minor" and "patch" in values:
        values["patch"] = 0
    replacement = rule.replacement.format(**values)
    if rule.validate is not None and re.fullmatch(rule.validate, replacement) is None:
        raise ValueError("Synthesized version fails its validation rule")
    return (text[: match.start()] + replacement + text[match.end() :]).encode("utf-8")


class _Control:
    def __init__(self, service, operation, fault):
        self.service, self.operation, self.fault = service, operation, fault

    def reserve(self):
        with self.service.store.read() as tx:
            row = tx.connection.execute(
                "SELECT a.* FROM commit_execution e JOIN commit_admissions a "
                "ON a.id=e.admission_id WHERE e.operation_id=?",
                (self.operation["id"],),
            ).fetchone()
            if row is None or row["state"] != "granted":
                raise _error(
                    "RECONCILIATION_REQUIRED",
                    "Git execution has no current exact grant",
                )
            return {"granted": True, "grant_id": row["grant_id"]}

    def status(self):
        with self.service.store.read() as tx:
            return status(tx, self.operation["actor_id"])

    def check_wait(self, grant_id):
        with self.service.store.read() as tx:
            self.service.require_effect(tx, self.operation)
            actor = tx.connection.execute(
                "SELECT archived,reported_state FROM actors WHERE id=?",
                (self.operation["actor_id"],),
            ).fetchone()
            if (
                actor is None
                or actor["archived"]
                or actor["reported_state"] in {"paused", "completed"}
            ):
                raise _error(
                    "NOT_AUTHORIZED", "Git admission requires a current active actor"
                )
            admission = tx.connection.execute(
                "SELECT a.state,a.grant_id FROM commit_execution e "
                "JOIN commit_admissions a ON a.id=e.admission_id WHERE e.operation_id=?",
                (self.operation["id"],),
            ).fetchone()
            if (
                admission is None
                or admission["state"] != "granted"
                or admission["grant_id"] != grant_id
            ):
                raise _error(
                    "RECONCILIATION_REQUIRED",
                    "Exact Git grant changed while waiting for handoff",
                )

    def release(self, grant_id):
        with self.service.store.write() as tx:
            row = tx.connection.execute(
                "SELECT prepared_json FROM commit_execution WHERE operation_id=?",
                (self.operation["id"],),
            ).fetchone()
            prepared = json.loads(row[0])
            if prepared.get("phase") in ("publishing", "published"):
                raise _error(
                    "RECONCILIATION_REQUIRED",
                    "Publication must reconcile before releasing its grant",
                )
            return release_exact(
                tx, self.operation["actor_id"], grant_id, reconciled=True
            )

    def progress(self, phase, values):
        with self.service.store.write() as tx:
            current = tx.connection.execute(
                "SELECT * FROM operations WHERE id=?", (self.operation["id"],)
            ).fetchone()
            if (
                current is None
                or current["state"] != "running"
                or current["claim_token"] != self.operation["claim_token"]
            ):
                raise _error("RECONCILIATION_REQUIRED", "Git execution claim changed")
            if phase in ("preparing", "publishing"):
                self.service.require_effect(tx, dict(current))
                actor = tx.connection.execute(
                    "SELECT archived,reported_state FROM actors WHERE id=?",
                    (current["actor_id"],),
                ).fetchone()
                if (
                    actor is None
                    or actor["archived"]
                    or actor["reported_state"] in {"paused", "completed"}
                ):
                    raise _error(
                        "NOT_AUTHORIZED",
                        "Git publication requires a current active actor",
                    )
            row = tx.connection.execute(
                "SELECT prepared_json FROM commit_execution WHERE operation_id=?",
                (self.operation["id"],),
            ).fetchone()
            prepared = dict(json.loads(row[0]), **values, phase=phase)
            tx.connection.execute(
                "UPDATE commit_execution SET prepared_json=?,base_commit=COALESCE(?,base_commit),"
                "published_commit=COALESCE(?,published_commit) WHERE operation_id=?",
                (
                    _json(prepared),
                    values.get("base_commit"),
                    values.get("published_commit"),
                    self.operation["id"],
                ),
            )
            if phase in ("preparing", "publishing"):
                tx.connection.execute(
                    "UPDATE operations SET effect_started_us=?,updated_us=? WHERE id=? AND claim_token=?",
                    (
                        tx.now_us,
                        tx.now_us,
                        self.operation["id"],
                        self.operation["claim_token"],
                    ),
                )
            tx.event(
                "commits",
                phase,
                self.operation["id"],
                self.operation["actor_id"],
                {"prepared_commit": prepared.get("prepared_commit")},
            )
        if self.fault:
            self.fault(phase)


def execute_operation(
    service, operation_record: dict, *, fault: Callable | None = None
) -> dict:
    """Run already-claimed slow work; no subprocess occupies a writer transaction."""
    operation = dict(operation_record)
    if operation["kind"] == "commit.reconcile":
        arguments = json.loads(operation["arguments_json"])
        result = reconcile(
            service, arguments["operation_id"], actor_id=operation["actor_id"]
        )
        with service.store.write() as tx:
            service.finish_operation(
                tx,
                operation["id"],
                "succeeded",
                result=result,
                claim_token=operation["claim_token"],
            )
        return result
    if operation["kind"] != "commit.execute" or operation["state"] != "running":
        raise _error("INVALID_ARGUMENT", "A claimed Git operation is required")
    args = json.loads(operation["arguments_json"])
    control = _Control(service, operation, fault)
    result = None
    failure = None
    try:
        patch = _read_patch(service.workspace.root, args)
        result = _execute_git(
            service.workspace.root,
            args["paths"],
            args["message"],
            args.get("bump_version", False),
            control,
            control.progress,
            service.config.version,
            adopt_staged=args.get("adopt_staged", False),
            patch=patch,
            base_commit=args.get("base_commit"),
            handoff_timeout=HANDOFF_WAIT_SECONDS,
        )
    except (OSError, ValueError, CoordinationError) as error:
        # Failure prose may include hook commands; persist a stable category only.
        failure = {
            "code": getattr(error, "code", "OPERATION_FAILED"),
            "message": error.message
            if isinstance(error, CoordinationError) and error.code == "SERVICE_BUSY"
            else "Git execution failed; inspect its recorded phase",
            "retryable": isinstance(error, CoordinationError)
            and error.code == "SERVICE_BUSY"
            and error.retryable,
        }
        if failure["retryable"]:
            failure.update(
                details={"phase": "handoff_wait", "effect_started": False},
                next_action=error.next_action,
            )
    with service.store.write() as tx:
        row = tx.connection.execute(
            "SELECT prepared_json FROM commit_execution WHERE operation_id=?",
            (operation["id"],),
        ).fetchone()
        prepared = json.loads(row[0])
        prepared["worker_finished"] = True
        tx.connection.execute(
            "UPDATE commit_execution SET prepared_json=? WHERE operation_id=?",
            (_json(prepared), operation["id"]),
        )
        uncertain = prepared.get("phase") in ("publishing", "published")
        state = (
            "uncertain"
            if uncertain
            else "failed"
            if failure or not result or not result.get("committed")
            else "succeeded"
        )
        if uncertain:
            tx.connection.execute(
                "UPDATE commit_admissions SET state='uncertain' WHERE id=(SELECT admission_id FROM commit_execution WHERE operation_id=?)",
                (operation["id"],),
            )
            tx.connection.execute(
                "UPDATE commit_grants SET state='uncertain' WHERE admission_id=(SELECT admission_id FROM commit_execution WHERE operation_id=?) AND state='active'",
                (operation["id"],),
            )
            admission = tx.connection.execute(
                "SELECT admission_id FROM commit_execution WHERE operation_id=?",
                (operation["id"],),
            ).fetchone()[0]
            tx.event("commits", "uncertain", admission, operation["actor_id"], {})
        elif failure:
            grant = tx.connection.execute(
                "SELECT a.grant_id FROM commit_execution e JOIN commit_admissions a "
                "ON a.id=e.admission_id WHERE e.operation_id=?",
                (operation["id"],),
            ).fetchone()
            release_exact(tx, operation["actor_id"], grant[0], reconciled=True)
        receipt = result or {
            "committed": bool(prepared.get("published_commit")),
            "commit": prepared.get("published_commit"),
            "reconciliation_required": uncertain,
        }
        service.finish_operation(
            tx,
            operation["id"],
            state,
            result=receipt,
            error=failure,
            claim_token=operation["claim_token"],
        )
    return receipt


def _read_patch(root, arguments: dict) -> bytes | None:
    if arguments.get("patch_file") is None:
        return (
            arguments["patch"].encode() if arguments.get("patch") is not None else None
        )
    root = Path(root).resolve()
    relative = _paths([arguments["patch_file"]])[0]
    source = root / relative
    if any(
        (root / Path(*Path(relative).parts[:depth])).is_symlink()
        for depth in range(1, len(Path(relative).parts) + 1)
    ):
        raise _error(
            "INVALID_ARGUMENT", "Reviewed patch reference must not traverse symlinks"
        )
    if not source.parent.resolve().is_relative_to(root):
        raise _error(
            "INVALID_ARGUMENT", "Reviewed patch reference escapes its workspace"
        )
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 16 * 1024 * 1024:
            raise _error(
                "INVALID_ARGUMENT",
                "Reviewed patch must be a regular file of 1 byte to 16 MiB",
            )
        content = stream.read(16 * 1024 * 1024 + 1)
    if (
        not 0 < len(content) <= 16 * 1024 * 1024
        or hashlib.sha256(content).hexdigest() != arguments["patch_sha256"]
    ):
        raise _error(
            "STALE_VERSION",
            "Reviewed patch bytes changed; review before submitting again",
        )
    return content


def reconcile(service, operation_id: str, *, actor_id: str) -> dict:
    """Recover the recorded ref/index effect; never create or publish another commit."""
    from .identity import process_status

    with service.store.read() as tx:
        operation = tx.connection.execute(
            "SELECT * FROM operations WHERE id=?", (operation_id,)
        ).fetchone()
        execution = tx.connection.execute(
            "SELECT e.*,a.grant_id FROM commit_execution e JOIN commit_admissions a "
            "ON a.id=e.admission_id WHERE e.operation_id=?",
            (operation_id,),
        ).fetchone()
        if operation is None or execution is None:
            raise _error("NOT_FOUND", "Recorded Git operation does not exist")
        if operation["actor_id"] != actor_id:
            raise _error("NOT_AUTHORIZED", "Git recovery belongs to another actor")
        operation, execution = dict(operation), dict(execution)
        prepared = json.loads(execution["prepared_json"])
        if operation["state"] == "succeeded":
            return json.loads(operation["result_json"])
    if (
        not prepared.get("worker_finished")
        and process_status(json.loads(operation["owner_identity_json"] or "{}"))
        != "gone"
    ):
        raise _error(
            "RECONCILIATION_REQUIRED",
            "Original Git worker is active or cannot be proven gone",
        )
    if not prepared.get("git_dir"):
        receipt = {
            "committed": False,
            "reconciled": True,
            "reason": "no_publication_intent",
        }
    else:
        receipt = _reconcile_git(service.workspace.root, prepared)
    with service.store.write() as tx:
        current = tx.connection.execute(
            "SELECT state,claim_token FROM operations WHERE id=?", (operation_id,)
        ).fetchone()
        if current["claim_token"] != operation["claim_token"]:
            raise _error(
                "RECONCILIATION_REQUIRED",
                "Original Git execution claim changed during recovery",
            )
        tx.connection.execute(
            "UPDATE commit_execution SET published_commit=?,reconciliation_json=? WHERE operation_id=?",
            (receipt.get("commit"), _json(receipt), operation_id),
        )
        release = release_exact(tx, actor_id, execution["grant_id"], reconciled=True)
        receipt["release"] = release
        service.finish_operation(
            tx,
            operation_id,
            "succeeded" if receipt["committed"] else "failed",
            result=receipt,
            claim_token=current["claim_token"],
        )
        tx.event(
            "commits",
            "recovery",
            operation_id,
            actor_id,
            {"committed": receipt["committed"]},
        )
    return receipt


def _reconcile_git(root, prepared: dict) -> dict:
    root = Path(root).resolve()
    env = dict(os.environ, GIT_LITERAL_PATHSPECS="1")
    if any(env.get(name) for name in ("GIT_INDEX_FILE", "GIT_DIR", "GIT_WORK_TREE")):
        raise _error("INVALID_ARGUMENT", "Remove Git overrides before reconciliation")
    executable = shutil.which("git")
    if executable is None:
        raise _error("OPERATION_FAILED", "Git is unavailable")

    def git(*arguments, index=None, data=None, check=True):
        return subprocess.run(
            [executable, *arguments],
            cwd=root,
            env=env if index is None else dict(env, GIT_INDEX_FILE=str(index)),
            input=data,
            capture_output=True,
            check=check,
        )

    git_dir = Path(os.fsdecode(git("rev-parse", "--absolute-git-dir").stdout).strip())
    if str(git_dir) != prepared["git_dir"]:
        raise _error(
            "WRONG_WORKSPACE", "Git recovery target no longer matches recorded metadata"
        )
    fd = os.open(
        git_dir / "agentcoord-commit.lock",
        os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
        0o600,
    )
    with os.fdopen(fd, "a") as lock:
        info = os.fstat(lock.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise _error(
                "NOT_AUTHORIZED", "Git handoff lock is not owned regular state"
            )
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise _error(
                "SERVICE_BUSY", "Another Git writer owns the handoff"
            ) from error
        commit = prepared.get("prepared_commit")
        reachable = (
            git("merge-base", "--is-ancestor", commit, "HEAD", check=False).returncode
            if commit
            else 1
        )
        if reachable not in (0, 1):
            raise _error(
                "RECONCILIATION_REQUIRED",
                "Cannot establish recorded commit reachability",
            )
        if reachable == 1:
            if (
                commit
                and git("rev-parse", "HEAD").stdout.decode().strip()
                != prepared["base_commit"]
            ):
                raise _error(
                    "RECONCILIATION_REQUIRED",
                    "Git history changed; inspect the exact prepared commit",
                )
            committed = False
        else:
            committed = True
        shared_lock = git_dir / "index.lock"
        if shared_lock.exists():
            info = shared_lock.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_ino != prepared["index_lock_inode"]
            ):
                raise _error(
                    "RECONCILIATION_REQUIRED",
                    "An unrelated Git index lock must be preserved",
                )
            shared_lock.unlink()
        if committed:
            descriptor = os.open(
                shared_lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            os.close(descriptor)
            try:
                shutil.copyfile(git_dir / "index", shared_lock)
                actual = {}
                for record in git(
                    "ls-files",
                    "--stage",
                    "-z",
                    "--",
                    *prepared["affected"],
                    index=shared_lock,
                ).stdout.split(b"\0"):
                    if record:
                        _metadata, name = record.split(b"\t", 1)
                        actual[os.fsdecode(name)] = base64.b64encode(
                            record + b"\0"
                        ).decode()
                if actual != prepared["target"]:
                    if actual != prepared["before"]:
                        raise _error(
                            "RECONCILIATION_REQUIRED",
                            "Selected peer staging changed after interrupted publication",
                        )
                    fmt = git("rev-parse", "--show-object-format").stdout.strip()
                    zero = b"0" * (
                        40 if fmt == b"sha1" else 64 if fmt == b"sha256" else 0
                    )
                    if not zero:
                        raise _error(
                            "OPERATION_FAILED", "Unsupported Git object format"
                        )
                    records = b"".join(
                        base64.b64decode(prepared["target"][name])
                        if name in prepared["target"]
                        else b"0 " + zero + b"\t" + os.fsencode(name) + b"\0"
                        for name in prepared["affected"]
                    )
                    git(
                        "update-index",
                        "-z",
                        "--index-info",
                        index=shared_lock,
                        data=records,
                    )
                version_path = prepared.get("version_path")
                if version_path:
                    version = root / version_path
                    if (
                        version.is_symlink()
                        or not version.parent.resolve().is_relative_to(root)
                    ):
                        raise _error(
                            "RECONCILIATION_REQUIRED", "Version recovery path is unsafe"
                        )
                    before, after = (
                        base64.b64decode(prepared["version_before"]),
                        base64.b64decode(prepared["version_after"]),
                    )
                    current = version.read_bytes()
                    if current not in (before, after):
                        raise _error(
                            "RECONCILIATION_REQUIRED",
                            "Authored version edits must be preserved",
                        )
                    if current == before:
                        version.write_bytes(after)
                shared_lock.replace(git_dir / "index")
            finally:
                shared_lock.unlink(missing_ok=True)
        return {
            "committed": committed,
            "commit": commit if committed else None,
            "reconciled": True,
            "reviewed_tree_matches": committed,
        }


def _execute_git(
    root,
    paths,
    message,
    bump_version,
    control,
    progress,
    version_rule,
    *,
    adopt_staged=False,
    patch=None,
    base_commit=None,
    handoff_timeout=HANDOFF_WAIT_SECONDS,
):
    root = Path(root).resolve()
    if not isinstance(message, str) or not message.strip() or not paths:
        raise ValueError("commit execute requires --paths FILE ... and --message TEXT")
    if type(adopt_staged) is not bool:
        raise ValueError("adopt_staged must be a boolean")
    if (patch is None) != (base_commit is None):
        raise ValueError("A reviewed patch requires its full base commit ID")
    if patch is not None:
        if not isinstance(patch, bytes) or not 0 < len(patch) <= 16 * 1024 * 1024:
            raise ValueError("Reviewed patch must contain 1 byte to 16 MiB")
        if not isinstance(base_commit, str) or not re.fullmatch(
            r"[0-9a-f]{40}|[0-9a-f]{64}", base_commit
        ):
            raise ValueError("Use the full reviewed base commit ID")
        if adopt_staged:
            raise ValueError("Patch commits cannot adopt already staged owned hunks")
    if any(os.environ.get(k) for k in ("GIT_INDEX_FILE", "GIT_DIR", "GIT_WORK_TREE")):
        raise ValueError("Remove Git repository/index overrides before commit execute")
    selected = []
    for path in paths:
        p = Path(path)
        if p.is_absolute() or ".." in p.parts or not p.parts or p.parts[0] == ".git":
            raise ValueError(
                "Declare exact repository-relative files, not directories or Git metadata"
            )
        if (root / p).is_dir() or not (root / p).parent.resolve().is_relative_to(root):
            raise ValueError("Declare exact files within this repository")
        selected.append(p.as_posix())
    selected = sorted(set(selected))
    version = version_rule.path if version_rule is not None else None
    if bump_version and version is None:
        raise ValueError("Automatic version synthesis is not configured")
    if version is not None:
        _paths([version])
    if bump_version and version in selected:
        raise ValueError(
            "Automatic version bump cannot also select authored version edits"
        )
    env = dict(os.environ, GIT_LITERAL_PATHSPECS="1")
    git_binary = shutil.which("git")
    if not git_binary:
        raise ValueError("Git is unavailable")

    def git(*args, index=None, data=None):
        try:
            return subprocess.run(
                [git_binary, *args],
                cwd=root,
                env=env if index is None else dict(env, GIT_INDEX_FILE=str(index)),
                input=data,
                check=True,
                capture_output=True,
            ).stdout
        except subprocess.CalledProcessError as error:
            raise ValueError(
                f"Git {args[0]} failed: {error.stderr.decode(errors='replace')[:2000]}"
            ) from error

    def entries(index, names):
        records = git("ls-files", "--stage", "-z", "--", *names, index=index)
        result = {}
        for record in records.split(b"\0"):
            if record:
                metadata, name = record.split(b"\t", 1)
                if metadata.split()[-1] != b"0":
                    raise ValueError(
                        "Resolve unmerged index entries before commit execute"
                    )
                result[os.fsdecode(name)] = metadata + b"\t" + name + b"\0"
        return result

    def stage_selected(index):
        # Explicit paths beneath ignored directories need -f even when tracked.
        # Force only existing index entries; new ignored paths still fail.
        tracked = entries(index, selected)
        other = [name for name in selected if name not in tracked]
        if other:
            git("add", "--", *other, index=index)
        if tracked:
            git("add", "-f", "--", *tracked, index=index)

    object_format = git("rev-parse", "--show-object-format").decode().strip()
    zero_oid = b"0" * ({"sha1": 40, "sha256": 64}.get(object_format) or 0)
    if not zero_oid:
        raise ValueError("Unsupported Git object format: " + object_format)

    def apply(index, names, records):
        data = b"".join(
            records.get(name, b"0 " + zero_oid + b"\t" + os.fsencode(name) + b"\0")
            for name in names
        )
        git("update-index", "-z", "--index-info", index=index, data=data)

    def staged_renames(index, head):
        fields = iter(
            git(
                "diff",
                "--cached",
                "--name-status",
                "-z",
                "--find-renames",
                head,
                "--",
                index=index,
            ).split(b"\0")
        )
        renames = []
        for status in fields:
            if not status:
                break
            source = next(fields)
            if status.startswith(b"R"):
                renames.append((os.fsdecode(source), os.fsdecode(next(fields))))
        return renames

    git_dir = Path(os.fsdecode(git("rev-parse", "--absolute-git-dir")).strip())

    def check_sequencer():
        if any(
            (git_dir / name).exists()
            for name in (
                "MERGE_HEAD",
                "CHERRY_PICK_HEAD",
                "REVERT_HEAD",
                "rebase-merge",
                "rebase-apply",
                "sequencer",
            )
        ):
            raise ValueError(
                "Finish the existing Git merge/sequencer through its reviewed manual workflow"
            )

    check_sequencer()
    grant = control.reserve()
    if grant.get("granted") is not True:
        return {
            "code": 3,
            "committed": False,
            **grant,
            "next_action": "continue_independent_work",
        }
    grant_id = grant.get("grant_id")
    if not isinstance(grant_id, str) or not grant_id:
        raise ValueError("Grant ID missing; inspect commit status before recovery")
    outcome = None
    published = False
    try:
        with tempfile.TemporaryDirectory(
            prefix="agentcoord-index-", dir=git_dir
        ) as temporary:
            private = Path(temporary) / "index"
            base = git("rev-parse", "HEAD").decode().strip()
            git("read-tree", base, index=private)
            if patch is None:
                stage_selected(private)
            else:
                if (
                    git("rev-parse", "--verify", base_commit + "^{commit}")
                    .decode()
                    .strip()
                    != base_commit
                ):
                    raise ValueError("Reviewed patch base is not a commit")
                if git("diff", "--name-only", base_commit, base, "--", *selected):
                    raise ValueError(
                        "Selected committed paths changed since the reviewed patch base"
                    )
                git(
                    "apply",
                    "--cached",
                    "--whitespace=error",
                    "-",
                    index=private,
                    data=patch,
                )
                changed = {
                    os.fsdecode(name)
                    for name in git(
                        "diff",
                        "--cached",
                        "--name-only",
                        "--no-renames",
                        "-z",
                        base,
                        "--",
                        index=private,
                    ).split(b"\0")
                    if name
                }
                if changed != set(selected):
                    raise ValueError(
                        "Reviewed patch must change exactly the declared paths"
                    )
            snapshot = entries(private, selected)
            work_snapshot = snapshot
            if patch is not None:
                # Prove that the selected repair is already present in the
                # working files without rewriting them or staging peer hunks.
                working = Path(temporary) / "working"
                git("read-tree", base, index=working)
                stage_selected(working)
                work_snapshot = entries(working, selected)
                git(
                    "apply",
                    "--cached",
                    "--check",
                    "--reverse",
                    "-",
                    index=working,
                    data=patch,
                )
            descriptor = os.open(
                git_dir / "agentcoord-commit.lock",
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                0o600,
            )
            with os.fdopen(descriptor, "a") as lock:
                lock_info = os.fstat(lock.fileno())
                if (
                    not stat.S_ISREG(lock_info.st_mode)
                    or lock_info.st_uid != os.getuid()
                ):
                    raise ValueError("Git handoff lock is not owned regular state")
                # Waiting never holds SQLite or expires the holder's grant.
                # Authority is rechecked between bounded kernel lock attempts;
                # the existing preparation is then rebased against current HEAD.
                deadline = time.monotonic() + handoff_timeout
                while True:
                    control.check_wait(grant_id)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise CoordinationError(
                            "SERVICE_BUSY",
                            "Git handoff deadline expired before any external effect",
                            retryable=True,
                            next_action="Inspect the current Git holder, then submit a new reviewed commit request",
                        )
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        time.sleep(min(HANDOFF_CHECK_SECONDS, remaining))
                check_sequencer()
                status = control.status()
                if not status.get("held_by_you") or status.get("grant_id") != grant_id:
                    raise ValueError("Commit reservation changed before publication")
                head = git("rev-parse", "HEAD").decode().strip()
                if git("diff", "--name-only", base, head, "--", *selected):
                    raise ValueError(
                        "Selected committed paths changed during preparation; review before retrying"
                    )
                # Re-stage only into a second private index to verify the exact
                # selected content still matches the prepared snapshot.
                verification = Path(temporary) / "verification"
                git("read-tree", head, index=verification)
                stage_selected(verification)
                if entries(verification, selected) != work_snapshot:
                    raise ValueError(
                        "Selected working files changed during preparation; review before retrying"
                    )
                # Disjoint commits can advance HEAD while this private index is prepared.
                git("read-tree", head, index=private)
                affected = [*selected, *([version] if bump_version else [])]
                original = entries(private, affected)
                apply(private, selected, snapshot)
                version_before = version_after = None
                if bump_version:
                    if (root / version).is_symlink() or not (
                        root / version
                    ).parent.resolve().is_relative_to(root):
                        raise ValueError(
                            "Version file must remain within the repository without symlink escape"
                        )
                    version_before = git("show", head + ":" + version)
                    if (root / version).read_bytes() != version_before:
                        raise ValueError(
                            "Version already has edits; coordinate its ownership before automatic bump"
                        )
                    version_after = synthesize_version(version_before, version_rule)
                    blob = git(
                        "hash-object", "-w", "--stdin", data=version_after
                    ).strip()
                    mode = original[version].split()[0]
                    git(
                        "update-index",
                        "--add",
                        "--cacheinfo",
                        mode.decode(),
                        blob.decode(),
                        version,
                        index=private,
                    )
                git("diff", "--cached", "--check", index=private)
                tree = git("write-tree", index=private).decode().strip()
                if tree == git("rev-parse", head + "^{tree}").decode().strip():
                    raise ValueError("No staged changes to commit")
                target = entries(private, affected)
                shared_lock = git_dir / "index.lock"
                # Git's own index lock also excludes ordinary concurrent git add.
                fd = os.open(shared_lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(fd)
                owns_shared_lock = True
                try:
                    shared = git_dir / "index"
                    shutil.copyfile(shared, shared_lock)
                    before = entries(shared_lock, affected)
                    staged_selected = [
                        name
                        for name in selected
                        if before.get(name) != original.get(name)
                    ]
                    if bump_version and before.get(version) != original.get(version):
                        raise ValueError(
                            "Selected paths already contain staged edits; preserve them and review ownership"
                        )
                    if patch is not None:
                        # Applying the owned patch to a private copy of the
                        # shared index retains disjoint peer staging, including
                        # hunks in the same file. Overlaps fail before publication.
                        git(
                            "apply",
                            "--cached",
                            "--whitespace=error",
                            "-",
                            index=shared_lock,
                            data=patch,
                        )
                    elif staged_selected and not adopt_staged:
                        raise ValueError(
                            "Selected paths already contain staged edits; preserve them and review ownership"
                        )
                    if staged_selected and patch is None:
                        if any(
                            before.get(name) != target.get(name)
                            for name in staged_selected
                        ):
                            raise ValueError(
                                "Selected staged content does not match the reviewed working snapshot"
                            )
                        selected_set = set(selected)
                        for source, destination in staged_renames(shared_lock, head):
                            if selected_set.intersection(
                                (source, destination)
                            ) and not {
                                source,
                                destination,
                            }.issubset(selected_set):
                                raise ValueError(
                                    "Select both sides of staged rename before adopting staged entries"
                                )
                    marker = "commit: agentcoord-" + uuid.uuid4().hex
                    hook_env = dict(
                        env,
                        GIT_INDEX_FILE=str(private),
                        GIT_REFLOG_ACTION=marker,
                        GIT_EDITOR=":",
                        AGENTCOORD_COMMIT_HOOK_ACTIVE="1",
                        AGENTCOORD_COMMIT_GRANT_ID=grant_id,
                        AGENTCOORD_COMMIT_LOCK_FD=str(lock.fileno()),
                    )

                    def run_hook(name, *arguments):
                        hook = Path(
                            os.fsdecode(
                                git("rev-parse", "--git-path", "hooks/" + name)
                            ).strip()
                        )
                        if not hook.is_absolute():
                            hook = root / hook
                        if not os.access(hook, os.X_OK) or not hook.is_file():
                            return 0
                        return subprocess.run(
                            [str(hook), *arguments],
                            cwd=root,
                            env=hook_env,
                            check=False,
                            # Native stdout is a machine-readable commit receipt.
                            # Stream hook diagnostics to stderr without buffering.
                            stdout=2,
                            pass_fds=(lock.fileno(),),
                        ).returncode

                    message_path = Path(temporary) / "COMMIT_EDITMSG"
                    message_path.write_text(message + "\n")
                    # Hooks and configured signing programs are external effects.
                    # Fence them before launch and retain the exact owned lock if
                    # the worker dies before it can prepare a publication receipt.
                    progress(
                        "preparing",
                        {
                            "base_commit": head,
                            "git_dir": str(git_dir),
                            "index_lock_inode": shared_lock.stat().st_ino,
                        },
                    )
                    for hook, arguments in (
                        ("pre-commit", ()),
                        ("prepare-commit-msg", (str(message_path), "message")),
                        ("commit-msg", (str(message_path),)),
                    ):
                        hook_code = run_hook(hook, *arguments)
                        if hook_code:
                            return {
                                "code": hook_code,
                                "committed": False,
                                "grant_id": grant_id,
                                "next_action": "Review Git hook failure; shared staging and working files were preserved.",
                            }
                    if git("write-tree", index=private).decode().strip() != tree:
                        raise ValueError(
                            "Commit hooks changed the reviewed private index; review before retrying"
                        )
                    commit_message = git("stripspace", data=message_path.read_bytes())
                    if not commit_message.strip():
                        raise ValueError("Commit message is empty after hooks")
                    sign = git(
                        "config",
                        "--type=bool",
                        "--default=false",
                        "--get",
                        "commit.gpgsign",
                    ).strip()
                    signing = ["-S"] if sign == b"true" else []
                    receipt = (
                        git(
                            "commit-tree",
                            tree,
                            "-p",
                            head,
                            *signing,
                            data=commit_message,
                        )
                        .decode()
                        .strip()
                    )
                    if not re.fullmatch(r"[0-9a-f]{40,64}", receipt):
                        raise ValueError("Exact commit-tree receipt is unavailable")
                    subject = commit_message.decode(errors="replace").splitlines()[0]
                    # Publish only if HEAD still has the parent used to build the
                    # reviewed tree. No porcelain commit or worktree refresh.
                    intended = (
                        entries(shared_lock, affected) if patch is not None else target
                    )
                    if patch is not None and bump_version:
                        intended = dict(intended, **{version: target[version]})
                    progress(
                        "publishing",
                        {
                            "base_commit": head,
                            "prepared_commit": receipt,
                            "tree": tree,
                            "affected": affected,
                            "before": _encode_records(before),
                            "target": _encode_records(intended),
                            "git_dir": str(git_dir),
                            "index_lock_inode": shared_lock.stat().st_ino,
                            "version_path": version if bump_version else None,
                            "version_before": base64.b64encode(version_before).decode()
                            if bump_version
                            else None,
                            "version_after": base64.b64encode(version_after).decode()
                            if bump_version
                            else None,
                        },
                    )
                    git(
                        "update-ref",
                        "-m",
                        marker + ": " + subject,
                        "HEAD",
                        receipt,
                        head,
                    )
                    published = True
                    outcome = {
                        "code": 0,
                        "committed": True,
                        "grant_id": grant_id,
                        "commit": receipt,
                        "reviewed_tree_matches": True,
                    }
                    progress("published", {"published_commit": receipt})
                    if patch is None:
                        apply(shared_lock, affected, target)
                    else:
                        outcome.update(
                            selection="patch",
                            base=base_commit,
                            patch_sha256=hashlib.sha256(patch).hexdigest(),
                        )
                        if bump_version:
                            apply(shared_lock, [version], target)
                    shared_lock.replace(shared)
                    owns_shared_lock = False
                    if bump_version:
                        if (root / version).read_bytes() != version_before:
                            raise ValueError(
                                "Version worktree changed during publication; Git succeeded but reconciliation requires review"
                            )
                        (root / version).write_bytes(version_after)
                    # The post hook sees the published index and version state.
                    # Git ignores its exit status; retain it as evidence.
                    progress("reconciled", {"published_commit": receipt})
                    outcome["post_commit_code"] = run_hook("post-commit")
                finally:
                    if owns_shared_lock:
                        shared_lock.unlink(missing_ok=True)
    except (OSError, ValueError, CoordinationError) as error:
        if not published:
            raise
        outcome = {
            **(outcome or {}),
            "code": 2,
            "committed": True,
            "grant_id": grant_id,
            "receipt_error": str(error),
            "next_action": "Git succeeded. Review its receipt and shared index; do not retry the commit.",
        }
    finally:
        try:
            release = control.release(grant_id)
            if outcome is not None:
                outcome["release"] = release
        except (OSError, ValueError, CoordinationError) as error:
            if outcome is None:
                raise
            outcome.update(
                code=2,
                release_pending=True,
                release_error=str(error),
                next_action=(
                    f"Git succeeded. Review the receipt and release only --grant-id {grant_id}; "
                    "do not retry the commit."
                ),
            )
    return outcome
