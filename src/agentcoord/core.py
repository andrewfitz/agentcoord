"""Typed application dispatch and shared validation; adapters supply composition."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import PurePosixPath

UNSET = object()


def now_us() -> int:
    return time.time_ns() // 1000


class CoordinationError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False,
                 details: dict | None = None, next_action: str | None = None):
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable
        self.details, self.next_action = dict(details or {}), next_action

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "retryable": self.retryable,
                "details": self.details, "next_action": self.next_action}


def canonical_json(value: object) -> str:
    try:
        def string_keys(item, parents):
            if not isinstance(item, (dict, list, tuple)):
                return
            if id(item) in parents:
                raise ValueError("circular JSON value")
            parents = {*parents, id(item)}
            if isinstance(item, dict):
                if any(not isinstance(key, str) for key in item):
                    raise ValueError("JSON object keys must be strings")
                children = item.values()
            else:
                children = item
            for child in children:
                string_keys(child, parents)
        string_keys(value, set())
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False)
        encoded.encode("utf-8", errors="strict")
        return encoded
    except (TypeError, ValueError, RecursionError) as error:
        raise CoordinationError("INVALID_ARGUMENT", "Value must be finite JSON data") from error


def validate_fields(arguments: dict, allowed, required=()) -> None:
    if not isinstance(arguments, dict) or any(not isinstance(k, str) for k in arguments):
        raise CoordinationError("INVALID_ARGUMENT", "Arguments must be an object with string keys")
    unknown, missing = set(arguments) - set(allowed), set(required) - set(arguments)
    if unknown or missing:
        raise CoordinationError("INVALID_ARGUMENT", "Invalid operation fields",
                                details={"unknown": sorted(unknown), "missing": sorted(missing)})


def bounded_text(value, label: str, max_bytes: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise CoordinationError("INVALID_ARGUMENT", f"{label} must be text without NUL")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise CoordinationError("INVALID_ARGUMENT", f"{label} must be valid UTF-8") from error
    if len(encoded) > max_bytes or (not allow_empty and not value.strip()):
        raise CoordinationError("INVALID_ARGUMENT", f"{label} exceeds its bounds")
    return value


def identifier(value, label: str = "ID") -> str:
    if not isinstance(value, str):
        raise CoordinationError("INVALID_ARGUMENT", f"{label} must be a UUID string")
    try:
        parsed = uuid.UUID(value)
    except ValueError as error:
        raise CoordinationError("INVALID_ARGUMENT", f"{label} must be a UUID string") from error
    if str(parsed) != value:
        raise CoordinationError("INVALID_ARGUMENT", f"{label} must use canonical UUID spelling")
    return value


def integer(value, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise CoordinationError("INVALID_ARGUMENT", f"{label} must be an integer in {minimum}..{maximum}")
    return value


def normalize_paths(values, *, allow_root: bool = False) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)) or len(values) > 10000:
        raise CoordinationError("INVALID_ARGUMENT", "Paths must be a bounded list")
    result = set()
    for value in values:
        value = bounded_text(value, "path", 4096)
        if value in {".", ""} and allow_root:
            result.add(".")
            continue
        p = PurePosixPath(value)
        if (p.is_absolute() or ".." in p.parts or not p.parts or
                p.parts[0] == ".git" or "\\" in value or p.as_posix() != value):
            raise CoordinationError("INVALID_ARGUMENT", "Paths must be normalized workspace-relative segments")
        result.add(p.as_posix())
    return tuple(sorted(result))


@dataclass(frozen=True)
class Context:
    workspace_id: str
    actor_id: str | None
    connection_id: str = ""
    transport: str = "cli"
    task_generation: str | None = None
    execution_generation: str | None = None
    operator: bool = False
    identity_mode: str = "native_owner"


@dataclass(frozen=True)
class Call:
    operation: str
    arguments: dict
    key: str | None = None
    expected_task_generation: str | None | object = UNSET
    expected_execution_generation: str | None | object = UNSET


@dataclass(frozen=True)
class Operation:
    name: str
    handler: Callable
    mutation: bool = False
    keyed: bool = False
    actor_required: bool = True


def _operation_get(service, context, arguments, tx):
    validate_fields(arguments, {"operation_id"}, {"operation_id"})
    row = tx.connection.execute("SELECT * FROM operations WHERE id=?", (
        identifier(arguments["operation_id"], "operation_id"),)).fetchone()
    if row is None or (not context.operator and row["actor_id"] != context.actor_id):
        raise CoordinationError("NOT_FOUND", "Operation is unavailable to this actor")
    return service.public_operation_record(row)


def _receipt_get(service, context, arguments, tx):
    validate_fields(arguments, {"key"}, {"key"})
    key = bounded_text(arguments["key"], "key", 256)
    row = tx.connection.execute("SELECT operation,result_json FROM idempotency WHERE actor_id=? AND retry_key=?",
                                (context.actor_id, key)).fetchone()
    if row is None:
        raise CoordinationError("NOT_FOUND", "No committed receipt exists for this key")
    return {"operation": row["operation"], "result": json.loads(row["result_json"])}


def _operation_list(service, context, arguments, tx):
    validate_fields(arguments, {"limit", "after", "state"})
    limit = integer(arguments.get("limit", 20), "limit", 1, 100)
    after = integer(arguments.get("after", 0), "after", 0, 2**63-1)
    state = arguments.get("state")
    if state is not None and state not in {"queued", "running", "succeeded", "failed", "cancelled", "uncertain"}:
        raise CoordinationError("INVALID_ARGUMENT", "Invalid operation state")
    rows = tx.connection.execute("SELECT * FROM operations WHERE (? OR actor_id=?) AND sequence>? AND (? IS NULL OR state=?) ORDER BY sequence LIMIT ?",
        (int(context.operator), context.actor_id, after, state, state, limit+1)).fetchall()
    return {"operations": [service.public_operation_record(row) for row in rows[:limit]],
            "after": rows[min(len(rows),limit)-1]["sequence"] if rows else after, "more": len(rows)>limit}


def _operation_reconcile(service, context, arguments, tx):
    validate_fields(arguments, {"operation_id"}, {"operation_id"})
    record = _operation_get(service, context, arguments, tx)
    handler = service.adapters.get("reconcile_handlers", {}).get(record["kind"])
    if handler is None:
        command = ("commit.reconcile" if record["kind"].startswith("commit.") else
                   "job.resolve" if record["kind"] == "job.execute" else
                   "operation.ack" if record["state"] == "failed" else "operation.reconcile")
        raise CoordinationError("RECONCILIATION_REQUIRED", "Use this operation's domain reconciliation command",
                                details={"kind": record["kind"], "operation_id": record["id"]}, next_action=command)
    return handler(service, context, record, tx)


def _operation_ack(service, context, arguments, tx):
    validate_fields(arguments, {"operation_id", "version"}, {"operation_id", "version"})
    operation_id = identifier(arguments["operation_id"], "operation_id")
    version = integer(arguments["version"], "version", 1, 2**63-1)
    row = tx.connection.execute("SELECT * FROM operations WHERE id=? AND actor_id=?", (operation_id, context.actor_id)).fetchone()
    if row is None:
        raise CoordinationError("NOT_FOUND", "Operation is unavailable to this actor")
    if row["sequence"] != version:
        raise CoordinationError("STALE_VERSION", "Operation outcome changed; retrieve the current receipt")
    if row["state"] != "failed":
        raise CoordinationError("RECONCILIATION_REQUIRED", "Only a failed outcome can be acknowledged; uncertain effects require domain reconciliation",
                                next_action="operation.reconcile")
    changed = row["acknowledged_sequence"] != version
    if changed:
        tx.connection.execute("UPDATE operations SET acknowledged_sequence=?,acknowledged_us=? WHERE id=?",
                              (version, tx.now_us, operation_id))
        tx.event("operation", "acknowledged", operation_id, context.actor_id, {"version": version})
    return {"operation_id": operation_id, "version": version, "acknowledged": True, "changed": changed}


def operations() -> tuple[Operation, ...]:
    return (Operation("operation.get", _operation_get, actor_required=False),
            Operation("operation.list", _operation_list, actor_required=False),
            Operation("operation.reconcile", _operation_reconcile, True, True),
            Operation("operation.ack", _operation_ack, True, True),
            Operation("receipt.get", _receipt_get))


def _pending_operation_query(context, *, new_only=False, filters=None):
    filters = dict(filters or {})
    validate_fields(filters, {"actor_id", "task", "path"})
    values = {"actor": context.actor_id, "operator": int(context.operator)}
    query = """SELECT o.*,a.task FROM operations o JOIN assignments a ON a.generation=o.task_generation
        WHERE o.state IN ('failed','uncertain') AND o.kind!='job.execute'
        AND (o.acknowledged_sequence IS NULL OR o.acknowledged_sequence!=o.sequence)
        AND (:operator OR o.actor_id=:actor)"""
    if "actor_id" in filters:
        values["filter_actor"] = identifier(filters["actor_id"], "actor_id")
        query += " AND o.actor_id=:filter_actor"
    if "task" in filters:
        values["task"] = bounded_text(filters["task"], "task", 1024, allow_empty=True)
        query += " AND a.task=:task"
    if "path" in filters:
        values["path"] = normalize_paths([filters["path"]], allow_root=True)[0]
        query += """ AND (:path='.' OR EXISTS (SELECT 1 FROM json_each(CASE
            WHEN json_type(o.arguments_json,'$.paths')='array' THEN json_extract(o.arguments_json,'$.paths')
            WHEN json_type(o.arguments_json,'$.request.paths')='array' THEN json_extract(o.arguments_json,'$.request.paths')
            WHEN json_type(o.arguments_json,'$.snapshot.paths')='array' THEN json_extract(o.arguments_json,'$.snapshot.paths')
            ELSE '[]' END) p WHERE p.type='text' AND
            (p.value=:path OR p.value='.' OR instr(p.value,:path||'/')=1 OR instr(:path,p.value||'/')=1)))"""
    if new_only and not context.operator:
        query += """ AND NOT EXISTS (SELECT 1 FROM action_presentations p WHERE p.actor_id=:actor
            AND p.kind='operation_failure' AND p.record_id=o.id AND p.version=o.sequence)"""
    return query, values


def select_actions(tx, context, *, after=None, limit=20, new_only=False, filters=None):
    query, values = _pending_operation_query(context, new_only=new_only, filters=filters)
    values.update(after=integer(0 if after is None else after, "after", 0, 2**63-1),
                  limit=integer(limit, "limit", 1, 100))
    rows = tx.connection.execute(query + " AND o.sequence>:after ORDER BY o.sequence LIMIT :limit", values).fetchall()
    return [{"kind": "operation_failure", "id": row["id"], "actor_id": row["actor_id"],
             "version": row["sequence"], "sequence": row["sequence"], "state": row["state"],
             "summary": f"{row['kind']} is {row['state']}", "message_ids": [], "notice_count": 0,
             "message_ids_more": False, "next_action": "operation.get"} for row in rows]


def count_pending(tx, context, *, new_only=False, filters=None):
    query, values = _pending_operation_query(context, new_only=new_only, filters=filters)
    return {"operations": tx.connection.execute("SELECT COUNT(*) FROM (" + query + ")", values).fetchone()[0]}


class Service:
    """Single workspace service, with explicit operation and adapter injection."""

    def __init__(self, store, workspace, config, operations=(), adapters=None):
        self.store, self.workspace, self.config = store, workspace, config
        self.adapters = dict(adapters or {})
        self.operations = {}
        for operation in operations:
            if operation.name in self.operations:
                raise CoordinationError("INVALID_ARGUMENT", f"Duplicate operation: {operation.name}")
            if operation.keyed and not operation.mutation:
                raise CoordinationError("INVALID_ARGUMENT", "Only mutations use retry receipts")
            self.operations[operation.name] = operation

    def require_actor(self, transaction, context: Context, *, active: bool = False) -> dict:
        from .identity import actor_for_context
        actor = actor_for_context(transaction, context)
        if active and (actor["archived"] or actor["reported_state"] in {"paused", "completed"}):
            raise CoordinationError("NOT_AUTHORIZED", "Operation requires an active actor")
        return actor

    def require_generation(self, transaction, actor_id, expected_task,
                           expected_execution=UNSET) -> dict:
        from .identity import require_generation
        return require_generation(transaction, actor_id, expected_task, expected_execution)

    def pending(self, transaction, context, **options) -> dict:
        callback = self.adapters.get("pending")
        if callback is None:
            raise CoordinationError("NOT_CONFIGURED", "Pending-action composition is not configured")
        return callback(transaction, context, **options)

    def execute(self, context: Context, call: Call) -> dict:
        request_id = str(uuid.uuid4())
        try:
            if not isinstance(context, Context) or not isinstance(call, Call):
                raise CoordinationError("INVALID_ARGUMENT", "Dispatch requires Context and Call")
            if context.workspace_id != self.workspace.id or context.workspace_id != self.store.workspace_id:
                raise CoordinationError("WRONG_WORKSPACE", "Connection belongs to another workspace")
            bounded_text(call.operation, "operation", 128)
            operation = self.operations.get(call.operation)
            if operation is None:
                raise CoordinationError("INVALID_ARGUMENT", "Unknown operation", details={"operation": call.operation})
            if not isinstance(call.arguments, dict):
                raise CoordinationError("INVALID_ARGUMENT", "Operation arguments must be an object")
            expected_task = context.task_generation if call.expected_task_generation is UNSET else call.expected_task_generation
            expected_execution = context.execution_generation if call.expected_execution_generation is UNSET else call.expected_execution_generation
            for value, label in ((expected_task, "expected_task_generation"), (expected_execution, "expected_execution_generation")):
                if value is not None:
                    identifier(value, label)
            encoded_call = canonical_json({"operation": call.operation, "arguments": call.arguments, "key": call.key,
                "expected_task_generation": expected_task,
                "expected_execution_generation": expected_execution})
            if len(encoded_call.encode()) > self.config.frame_bytes - 1024:
                raise CoordinationError("INVALID_ARGUMENT", "Request exceeds the configured transport frame")
            if context.operator and operation.mutation:
                raise CoordinationError("NOT_AUTHORIZED", "Operator connections cannot perform actor mutations")
            if context.identity_mode == "shared_group" and (
                call.operation in {"identity.checkpoint", "identity.complete", "identity.delegate", "identity.event",
                                   "commit.acquire", "commit.cancel", "commit.release", "commit.execute", "commit.reconcile"}
                or (call.operation == "job.schedule" and call.arguments.get("kind") == "resume")):
                raise CoordinationError("NOT_AUTHORIZED", "Use independently bound native-owner CLI for this operation")
            if operation.keyed:
                bounded_text(call.key, "retry key", 256)
            elif call.key is not None:
                raise CoordinationError("INVALID_ARGUMENT", "This operation does not accept a retry key")
            scope = self.store.write() if operation.mutation else self.store.read()
            with scope as tx:
                actor = self.require_actor(tx, context) if context.actor_id is not None else None
                if operation.actor_required and actor is None:
                    raise CoordinationError("UNBOUND_ACTOR", "Operation requires a bound native actor")
                if operation.keyed and not context.actor_id:
                    raise CoordinationError("UNBOUND_ACTOR", "Mutation receipt requires a bound actor")
                payload_digest = hashlib.sha256(canonical_json({"workspace": context.workspace_id,
                    "actor": context.actor_id, "operation": call.operation,
                    "arguments": call.arguments}).encode()).hexdigest()
                digest = hashlib.sha256(canonical_json({"workspace": context.workspace_id,
                    "actor": context.actor_id, "operation": call.operation,
                    "expected_task_generation": expected_task,
                    "expected_execution_generation": expected_execution,
                    "arguments": call.arguments}).encode()).hexdigest()
                prior = None
                if operation.keyed:
                    prior = tx.connection.execute("SELECT * FROM idempotency WHERE actor_id=? AND retry_key=?",
                                                  (context.actor_id, call.key)).fetchone()
                if prior:
                    if prior["payload_sha256"] != payload_digest or prior["operation"] != call.operation:
                        raise CoordinationError("IDEMPOTENCY_CONFLICT", "Retry key was used for different content")
                    result = json.loads(prior["result_json"])
                else:
                    if operation.mutation and actor is not None and call.operation != "identity.event":
                        self.require_generation(tx, actor["id"], expected_task, expected_execution)
                    effective_context = replace(context, task_generation=expected_task,
                                                execution_generation=expected_execution)
                    result = operation.handler(self, effective_context, dict(call.arguments), tx)
                    encoded = canonical_json(result)
                    if len(encoded.encode()) > self.config.frame_bytes - 1024:
                        raise CoordinationError("INVALID_ARGUMENT", "Response exceeds transport frame; request a smaller page")
                    if operation.keyed:
                        tx.connection.execute("INSERT INTO idempotency(actor_id,retry_key,operation,input_sha256,result_json,payload_sha256,context_json,created_us) VALUES (?,?,?,?,?,?,?,?)",
                            (context.actor_id, call.key, call.operation, digest, encoded, payload_digest,
                             canonical_json({"task_generation": expected_task, "execution_generation": expected_execution}), tx.now_us))
                next_context = None
                if context.actor_id:
                    current = tx.connection.execute("SELECT current_task_generation,current_execution_generation FROM actors WHERE id=?", (context.actor_id,)).fetchone()
                    if current:
                        next_context = {"task_generation": current[0], "execution_generation": current[1]}
            envelope = {"ok": True, "protocol": 1, "request_id": request_id,
                        "data": result, "action_digest": None, "next_context": next_context}
            if prior:
                envelope["replayed"] = True
                envelope["receipt_context"] = json.loads(prior["context_json"])
            boundary = call.operation in {"work.activity", "identity.checkpoint", "identity.complete", "commit.execute"}
            if call.operation == "operation.get" and isinstance(result, dict):
                boundary = (result.get("kind") in {"commit.execute", "job.execute"} and
                            result.get("state") in {"succeeded", "failed", "cancelled", "uncertain"} and
                            not context.operator)
            if not prior and isinstance(result, dict) and (
                    (result.get("operation_id") is not None and result.get("state") == "queued") or
                    call.operation == "job.schedule"):
                callback = self.adapters.get("work_available")
                if callback:
                    try:
                        callback()
                    except Exception:  # noqa: BLE001 - callback failure cannot invalidate the committed receipt.
                        envelope["wake_error"] = {"code": "NOT_AVAILABLE", "message": "Request committed; background dispatcher will discover it at the next scheduled sweep"}
            if boundary and not prior and isinstance(result, dict) and result.get("changed", True):
                callback = self.adapters.get("boundary_digest")
                if callback:
                    try:
                        refreshed = replace(context, **(next_context or {}))
                        envelope["action_digest"] = callback(refreshed)
                    except (CoordinationError, sqlite3.Error):
                        envelope["digest_error"] = {"code": "STORAGE_UNAVAILABLE", "message": "Primary operation succeeded; retrieve pending actions at the next boundary"}
            return envelope
        except CoordinationError as error:
            return {"ok": False, "protocol": 1, "request_id": request_id, "error": error.as_dict()}
        except sqlite3.Error as error:
            return {"ok": False, "protocol": 1, "request_id": request_id,
                    "error": CoordinationError("STORAGE_UNAVAILABLE", "Database operation failed",
                                               details={"sqlite_error": type(error).__name__}).as_dict()}

    def enqueue(self, tx, context, kind: str, arguments: dict, *, key: str) -> dict:
        bounded_text(key, "operation key", 256)
        if kind not in self.adapters.get("slow_handlers", {}):
            raise CoordinationError("NOT_CONFIGURED", "Slow operation handler is not registered", details={"kind": kind})
        state = tx.connection.execute("SELECT value_json FROM meta WHERE key='service_state'").fetchone()
        if state and json.loads(state[0]) != "active":
            raise CoordinationError("AUTHORITY_FENCED", "Service is not accepting new external executions")
        actor = self.require_actor(tx, context)
        encoded = canonical_json(arguments)
        if len(encoded.encode()) > self.config.frame_bytes - min(8192, self.config.frame_bytes // 4):
            raise CoordinationError("INVALID_ARGUMENT", "Prepared execution exceeds the bounded receipt budget; reduce scope or use domain pagination")
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        prior = tx.connection.execute("SELECT * FROM operations WHERE actor_id=? AND kind=? AND retry_key=?",
                                      (context.actor_id, kind, key)).fetchone()
        if prior:
            if prior["arguments_sha256"] != digest:
                raise CoordinationError("IDEMPOTENCY_CONFLICT", "Operation key was reused for different content")
            return {"operation_id": prior["id"], "kind": kind, "state": prior["state"]}
        operation_id = str(uuid.uuid4())
        authority = json.loads(tx.connection.execute("SELECT value_json FROM meta WHERE key='authority_generation'").fetchone()[0])
        tx.connection.execute("""INSERT INTO operations(id,actor_id,kind,task_generation,
            execution_generation,authority_generation,state,arguments_json,arguments_sha256,
            retry_key,created_us,updated_us) VALUES (?,?,?,?,?,?,'queued',?,?,?,?,?)""",
            (operation_id, context.actor_id, kind, actor["current_task_generation"],
             actor["current_execution_generation"], authority, encoded, digest, key, tx.now_us, tx.now_us))
        sequence = tx.event("operation", "queued", operation_id, context.actor_id, {"kind": kind})
        tx.connection.execute("UPDATE operations SET sequence=? WHERE id=?", (sequence, operation_id))
        return {"operation_id": operation_id, "kind": kind, "state": "queued"}

    @staticmethod
    def operation_record(row) -> dict:
        result = dict(row)
        for field, name in (("arguments_json", "arguments"), ("owner_identity_json", "owner_identity"),
                            ("result_json", "result"), ("error_json", "error")):
            result[name] = json.loads(result[field]) if result.get(field) is not None else None
        return result

    @classmethod
    def public_operation_record(cls, row) -> dict:
        return {key: value for key, value in cls.operation_record(row).items()
                if not key.endswith("_json") and key != "claim_token"}

    def finish_operation(self, tx, operation_id: str, state: str, *, result=None,
                         error=None, claim_token=None) -> dict:
        if state not in {"succeeded", "failed", "cancelled", "uncertain"}:
            raise CoordinationError("INVALID_ARGUMENT", "Invalid terminal operation state")
        row = tx.connection.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
        if row is None:
            raise CoordinationError("NOT_FOUND", "Operation does not exist")
        if claim_token is not None and row["claim_token"] != claim_token:
            raise CoordinationError("NOT_AUTHORIZED", "Operation claim changed")
        candidate = dict(row)
        candidate.update(state=state, result_json=canonical_json(result) if result is not None else None,
                         error_json=canonical_json(error) if error is not None else None)
        if len(canonical_json(self.public_operation_record(candidate)).encode()) > self.config.frame_bytes - 1024:
            raise CoordinationError("INVALID_ARGUMENT", "Operation receipt exceeds transport frame; publish a bounded domain record")
        if row["state"] not in {"queued", "running", "uncertain"}:
            if row["state"] != state:
                raise CoordinationError("STALE_VERSION", "Operation already has another terminal result")
            return self.operation_record(row)
        tx.connection.execute("UPDATE operations SET state=?,result_json=?,error_json=?,updated_us=?,lease_until_us=NULL WHERE id=?",
            (state, canonical_json(result) if result is not None else None,
             canonical_json(error) if error is not None else None, tx.now_us, operation_id))
        sequence = tx.event("operation", state, operation_id, row["actor_id"], {"kind": row["kind"]})
        tx.connection.execute("UPDATE operations SET sequence=?,acknowledged_sequence=NULL,acknowledged_us=NULL WHERE id=?", (sequence, operation_id))
        return self.operation_record(tx.connection.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone())

    def require_effect(self, tx, operation_record, *, continuing=False) -> dict:
        row = tx.connection.execute("SELECT * FROM operations WHERE id=?", (operation_record["id"],)).fetchone()
        if row is None or row["state"] != "running" or row["claim_token"] != operation_record["claim_token"]:
            raise CoordinationError("NOT_AUTHORIZED", "External execution claim changed")
        if continuing and row["effect_started_us"] is None:
            raise CoordinationError("NOT_AUTHORIZED", "External effect has not started")
        self.require_generation(tx, row["actor_id"], row["task_generation"], row["execution_generation"])
        metadata = {r[0]: json.loads(r[1]) for r in tx.connection.execute("SELECT key,value_json FROM meta WHERE key IN ('service_state','authority_generation')")}
        allowed_states = {"active", "draining"} if continuing else {"active"}
        if metadata.get("service_state") not in allowed_states or metadata.get("authority_generation") != row["authority_generation"]:
            raise CoordinationError("AUTHORITY_FENCED", "Service cannot start another external effect")
        return self.operation_record(row)

    def run_operation(self, operation_id: str, *, owner_identity=None) -> dict:
        identifier(operation_id, "operation_id")
        claim = str(uuid.uuid4())
        with self.store.write() as tx:
            row = tx.connection.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            if row is None:
                raise CoordinationError("NOT_FOUND", "Operation does not exist")
            if row["state"] != "queued":
                return self.operation_record(row)
            metadata = {r[0]: json.loads(r[1]) for r in tx.connection.execute("SELECT key,value_json FROM meta WHERE key IN ('service_state','authority_generation')")}
            if metadata.get("service_state") != "active":
                return self.operation_record(row)
            if metadata.get("authority_generation") != row["authority_generation"]:
                return self.finish_operation(tx, operation_id, "failed", error=CoordinationError("AUTHORITY_FENCED", "Accepted execution belongs to an earlier authority").as_dict())
            try:
                self.require_generation(tx, row["actor_id"], row["task_generation"], row["execution_generation"])
            except CoordinationError as error:
                return self.finish_operation(tx, operation_id, "failed", error=error.as_dict())
            handler = self.adapters.get("slow_handlers", {}).get(row["kind"])
            if handler is None:
                raise CoordinationError("NOT_CONFIGURED", "Slow operation handler is not registered")
            changed = tx.connection.execute("UPDATE operations SET state='running',claim_token=?,owner_identity_json=?,lease_until_us=?,updated_us=? WHERE id=? AND state='queued'",
                (claim, canonical_json(owner_identity) if owner_identity else None,
                 tx.now_us + 60_000_000, tx.now_us, operation_id)).rowcount
            if not changed:
                raise CoordinationError("STALE_VERSION", "Operation was claimed concurrently")
            sequence = tx.event("operation", "running", operation_id, row["actor_id"], {"kind": row["kind"]})
            tx.connection.execute("UPDATE operations SET sequence=? WHERE id=?", (sequence, operation_id))
            claimed = self.operation_record(tx.connection.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone())
        try:
            result = handler(self, claimed)
            with self.store.write() as tx:
                current = tx.connection.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
                if current["state"] == "running" and current["claim_token"] == claim:
                    return self.finish_operation(tx, operation_id, "succeeded", result=result, claim_token=claim)
                return self.operation_record(current)
        except Exception as error:  # noqa: BLE001 - ordinary worker faults need durable outcomes; process exits remain recoverable.
            diagnostic = (error.as_dict() if isinstance(error, CoordinationError) else
                          CoordinationError("OPERATION_FAILED", "Operation worker failed",
                                            details={"exception": type(error).__name__}).as_dict())
            with self.store.write() as tx:
                current = tx.connection.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
                if current["state"] == "running" and current["claim_token"] == claim:
                    return self.finish_operation(tx, operation_id,
                        "uncertain" if current["effect_started_us"] is not None else "failed",
                        error=diagnostic, claim_token=claim)
                return self.operation_record(current)
