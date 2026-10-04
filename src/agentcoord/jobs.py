"""Durable reminders and explicitly opted-in native conversation resumes.

SQLite owns admission and receipts. Process launch is an external effect: a
persisted launch intent without a result is uncertain and cannot auto-relaunch.
"""

from __future__ import annotations

import json
import os
import random
import selectors
import shutil
import signal
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path

from .core import Context, CoordinationError, Operation
from .identity import group_status, process_identity, process_status
from .store import private_directory

SCHEMA: tuple[str, ...] = (
    """CREATE TABLE jobs (
        id TEXT PRIMARY KEY, actor_id TEXT NOT NULL REFERENCES actors(id),
        task_generation TEXT NOT NULL REFERENCES assignments(generation),
        execution_generation TEXT REFERENCES executions(generation),
        kind TEXT NOT NULL CHECK(kind IN ('reminder','resume')),
        due_us INTEGER NOT NULL CHECK(due_us>=0), target_json TEXT NOT NULL CHECK(json_valid(target_json)),
        payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
        state TEXT NOT NULL CHECK(state IN
            ('pending','running','succeeded','failed','cancelled','uncertain')),
        operation_id TEXT REFERENCES operations(id),
        attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts>=0),
        cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0,1)),
        last_result_json TEXT CHECK(last_result_json IS NULL OR json_valid(last_result_json)), created_us INTEGER NOT NULL,
        updated_us INTEGER NOT NULL, version INTEGER NOT NULL DEFAULT 1 CHECK(version>0),
        delivery_deadline_us INTEGER NOT NULL CHECK(delivery_deadline_us>=due_us),
        CHECK(kind<>'resume' OR execution_generation IS NOT NULL)
    )""",
    "CREATE INDEX jobs_due ON jobs(state,due_us,id)",
    "CREATE INDEX jobs_owner ON jobs(actor_id,created_us,id)",
    "CREATE UNIQUE INDEX jobs_operation ON jobs(operation_id) WHERE operation_id IS NOT NULL",
    """CREATE UNIQUE INDEX jobs_resume_exclusive ON jobs(actor_id)
        WHERE kind='resume' AND state IN ('running','uncertain')""",
    """CREATE TABLE job_attempts (
        id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id),
        operation_id TEXT NOT NULL UNIQUE REFERENCES operations(id),
        claim_token TEXT NOT NULL, started_us INTEGER NOT NULL, ended_us INTEGER,
        outcome_json TEXT CHECK(outcome_json IS NULL OR json_valid(outcome_json)),
        execution_generation TEXT REFERENCES executions(generation),
        launch_intent_us INTEGER, process_identity_json TEXT
        CHECK(process_identity_json IS NULL OR json_valid(process_identity_json))
    )""",
    "CREATE INDEX job_attempts_job ON job_attempts(job_id,started_us,id)",
    """CREATE TABLE job_notifications (
        job_id TEXT NOT NULL REFERENCES jobs(id),
        message_id TEXT NOT NULL REFERENCES messages(id),
        PRIMARY KEY(job_id,message_id)
    )""",
)


def _json(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _error(code, message):
    raise CoordinationError(code, message)


def _args(arguments, required=(), optional=()):
    if not isinstance(arguments, dict) or set(arguments) - set(required) - set(optional):
        _error("INVALID_ARGUMENT", "Unexpected job arguments")
    if set(required) - set(arguments):
        _error("INVALID_ARGUMENT", "Required job arguments are missing")


def _integer(value, name, minimum=0, maximum=253402300799999999):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        _error("INVALID_ARGUMENT", f"{name} is outside its integer range")
    return value


def _text(value, name, limit=8192):
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        _error("INVALID_ARGUMENT", f"{name} requires bounded nonempty text")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        _error("INVALID_ARGUMENT", f"{name} requires valid UTF-8")
    if size > limit:
        _error("INVALID_ARGUMENT", f"{name} exceeds its UTF-8 byte limit")
    return value


def _id(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
    except ValueError:
        _error("INVALID_ARGUMENT", "An exact job ID is required")
    return value


def _record(row):
    result = dict(row)
    for key in ("target_json", "payload_json", "last_result_json"):
        value = result.pop(key)
        result[key[:-5]] = json.loads(value) if value is not None else None
    result["cancel_requested"] = bool(result["cancel_requested"])
    return result


def _owned(service, ctx, tx, job_id):
    service.require_actor(tx, ctx)
    row = tx.connection.execute("SELECT * FROM jobs WHERE id=?", (_id(job_id),)).fetchone()
    if row is None or (not ctx.operator and row["actor_id"] != ctx.actor_id):
        _error("NOT_FOUND", "Job was not found")
    return dict(row)


def validate_target(service, tx, job):
    """Validate immutable target and current consent, without doing process I/O."""
    actor = service.require_generation(
        tx, job["actor_id"], job["task_generation"], job["execution_generation"]
    )
    if actor["archived"]:
        _error("STALE_GENERATION", "Job target is archived")
    if job["kind"] == "resume":
        target = json.loads(job["target_json"])
        if (
            actor.get("child_id")
            or actor["harness"] != target["harness"]
            or actor["native_session_id"] != target["native_session_id"]
        ):
            _error("STALE_GENERATION", "Native resume target changed")
        if actor["reported_state"] not in {"idle", "blocked"}:
            _error("NOT_AUTHORIZED", "Native resume requires an idle or blocked actor")
        consent = {"enabled": actor["resume_enabled"], "inhibited": actor["resume_inhibited"]}
        if consent.get("enabled") is not True or consent.get("inhibited") is not False:
            _error("NOT_AUTHORIZED", "Native resume consent is absent or inhibited")
        execution = tx.connection.execute(
            "SELECT process_identity_json FROM executions WHERE generation=? AND actor_id=?",
            (job["execution_generation"], actor["id"]),
        ).fetchone()
        if execution is None or execution["process_identity_json"] is None:
            _error("NOT_AUTHORIZED", "Native process provenance is missing")
        if json.loads(execution["process_identity_json"]) != target["process_identity"]:
            _error("STALE_GENERATION", "Native process provenance changed")
    return actor


def schedule(service, ctx, args, tx):
    _args(args, ("kind", "due_us", "note"), ("timeout_seconds", "subject", "delivery_deadline_us"))
    if not isinstance(args["kind"], str) or args["kind"] not in {"reminder", "resume"}:
        _error("INVALID_ARGUMENT", "Job kind must be reminder or resume")
    actor = service.require_actor(tx, ctx)
    service.require_generation(tx, actor["id"], ctx.task_generation, ctx.execution_generation)
    due = _integer(args["due_us"], "due_us")
    note = _text(args["note"], "note")
    deadline = _integer(
        args.get("delivery_deadline_us", due + 86400_000000), "delivery_deadline_us", due
    )
    timeout = _integer(args.get("timeout_seconds", 3600), "timeout_seconds", 1, 86400)
    target = {"harness": actor["harness"], "native_session_id": actor["native_session_id"]}
    if args["kind"] == "resume":
        if ctx.identity_mode != "native_owner" or actor.get("child_id"):
            _error("NOT_AUTHORIZED", "Only a bound native conversation owner may schedule resume")
        if "subject" in args or "delivery_deadline_us" in args:
            _error("INVALID_ARGUMENT", "Resume does not accept reminder delivery fields")
        execution = tx.connection.execute(
            "SELECT process_identity_json FROM executions WHERE generation=? AND actor_id=?",
            (ctx.execution_generation, actor["id"]),
        ).fetchone()
        if execution is None or execution["process_identity_json"] is None:
            _error("NOT_AUTHORIZED", "Native process provenance is missing")
        target["process_identity"] = json.loads(execution["process_identity_json"])
        # Consent is checked at scheduling as well as immediately before launch.
        consent = {"enabled": actor["resume_enabled"], "inhibited": actor["resume_inhibited"]}
        if consent.get("enabled") is not True or consent.get("inhibited") is not False:
            _error("NOT_AUTHORIZED", "Native resume consent is absent or inhibited")
        _validate_native_target(target, note)
    elif "timeout_seconds" in args:
        _error("INVALID_ARGUMENT", "Reminder does not accept a process timeout")
    payload = {"note": note, "timeout_seconds": timeout}
    if args["kind"] == "reminder":
        payload["subject"] = _text(args.get("subject", "Scheduled reminder"), "subject", 512)
    job_id = str(uuid.uuid4())
    tx.connection.execute(
        """INSERT INTO jobs
        (id,actor_id,task_generation,execution_generation,kind,due_us,target_json,payload_json,state,
         created_us,updated_us,delivery_deadline_us) VALUES (?,?,?,?,?,?,?,?,'pending',?,?,?)""",
        (
            job_id,
            actor["id"],
            ctx.task_generation,
            ctx.execution_generation,
            args["kind"],
            due,
            _json(target),
            _json(payload),
            tx.now_us,
            tx.now_us,
            deadline,
        ),
    )
    tx.event("jobs", "scheduled", job_id, ctx.actor_id, {"kind": args["kind"], "due_us": due})
    return _record(tx.connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())


def get(service, ctx, args, tx):
    _args(args, ("id",))
    return _record(_owned(service, ctx, tx, args["id"]))


def list_jobs(service, ctx, args, tx):
    _args(args, (), ("state", "limit", "after"))
    service.require_actor(tx, ctx)
    limit = _integer(args.get("limit", 20), "limit", 1, 100)
    state = args.get("state")
    if state is not None and (
        not isinstance(state, str)
        or state not in {"pending", "running", "succeeded", "failed", "cancelled", "uncertain"}
    ):
        _error("INVALID_ARGUMENT", "Unknown job state")
    after = args.get("after", "")
    if "after" in args:
        _id(after)
    rows = tx.connection.execute(
        """SELECT * FROM jobs WHERE (? OR actor_id=?) AND
        (? IS NULL OR state=?) AND id>? ORDER BY id LIMIT ?""",
        (ctx.operator, ctx.actor_id, state, state, after, limit + 1),
    ).fetchall()
    return {
        "jobs": [_record(row) for row in rows[:limit]],
        "next": rows[limit - 1]["id"] if len(rows) > limit else None,
    }


def cancel(service, ctx, args, tx):
    _args(args, ("id",))
    job = _owned(service, ctx, tx, args["id"])
    service.require_generation(tx, ctx.actor_id, ctx.task_generation, ctx.execution_generation)
    if job["state"] in {"pending", "running", "uncertain"}:
        state = job["state"] if job["state"] in {"running", "uncertain"} else "cancelled"
        tx.connection.execute(
            """UPDATE jobs SET cancel_requested=1,state=?,version=version+1,updated_us=?
            WHERE id=? AND version=?""",
            (state, tx.now_us, job["id"], job["version"]),
        )
        if state == "cancelled" and job["operation_id"]:
            tx.connection.execute(
                "UPDATE operations SET state='cancelled',updated_us=? WHERE id=? AND state='queued'",
                (tx.now_us, job["operation_id"]),
            )
        tx.event("jobs", "cancel_requested", job["id"], ctx.actor_id, {})
    return _record(tx.connection.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone())


def reconcile(service, ctx, args, tx):
    _args(args, ("id", "version", "resolution", "note"), ("due_us",))
    job = _owned(service, ctx, tx, args["id"])
    _text(args["note"], "note")
    if _integer(args["version"], "version", 1) != job["version"]:
        _error("STALE_VERSION", "Job changed before reconciliation")
    if job["state"] not in {"uncertain", "failed"}:
        _error(
            "RECONCILIATION_REQUIRED", "Only a recorded failed or uncertain effect can be resolved"
        )
    if not isinstance(args["resolution"], str) or args["resolution"] not in {"retry", "complete"}:
        _error("INVALID_ARGUMENT", "Job resolution must be retry or complete")
    retry = args["resolution"] == "retry"
    service.require_generation(tx, ctx.actor_id, ctx.task_generation, ctx.execution_generation)
    if retry:
        validate_target(service, tx, job)
    if retry and job["cancel_requested"]:
        _error("NOT_AUTHORIZED", "Cancelled work cannot be resurrected")
    attempt = tx.connection.execute(
        "SELECT * FROM job_attempts WHERE operation_id=?", (job["operation_id"],)
    ).fetchone()
    previous = json.loads(job["last_result_json"]) if job["last_result_json"] else {}
    if (
        retry
        and attempt is not None
        and attempt["launch_intent_us"] is not None
        and previous.get("stopped") is not True
    ):
        _error(
            "RECONCILIATION_REQUIRED",
            "Launched work has no confirmed stopped process group; inspect its effect before closing this job",
        )
    due = _integer(args.get("due_us", tx.now_us), "due_us")
    state = "pending" if retry else ("cancelled" if job["cancel_requested"] else "succeeded")
    result = {"reconciled": True, "resolution": args["resolution"], "note": args["note"]}
    tx.connection.execute(
        """UPDATE jobs SET state=?,operation_id=NULL,due_us=?,
        delivery_deadline_us=MAX(delivery_deadline_us,?),last_result_json=?,version=version+1,updated_us=?
        WHERE id=? AND version=?""",
        (state, due, due, _json(result), tx.now_us, job["id"], job["version"]),
    )
    if job["operation_id"]:
        operation = tx.connection.execute(
            "SELECT state FROM operations WHERE id=?", (job["operation_id"],)
        ).fetchone()
        if operation is not None and operation["state"] == "uncertain":
            service.finish_operation(
                tx, job["operation_id"], "failed" if retry else state, result=result
            )
        # An already failed attempt remains an immutable historical failure.
        # Deliberate retry creates a new operation; the job owns its resolution.
    tx.event("jobs", "reconciled", job["id"], ctx.actor_id, {"resolution": args["resolution"]})
    return _record(tx.connection.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone())


def operations() -> tuple[Operation, ...]:
    return (
        Operation("job.schedule", schedule, True, True, True),
        Operation("job.list", list_jobs, False, False, True),
        Operation("job.get", get, False, False, True),
        Operation("job.cancel", cancel, True, True, True),
        Operation("job.resolve", reconcile, True, True, True),
    )


def _context(service, job):
    return Context(
        workspace_id=service.store.workspace_id,
        actor_id=job["actor_id"],
        connection_id="job-worker",
        identity_mode="native_owner",
        transport="worker",
        task_generation=job["task_generation"],
        execution_generation=job["execution_generation"],
    )


def claim_due(service, *, limit=16):
    """Enqueue a bounded due batch; actual worker claims are separate CAS writes."""
    limit = _integer(limit, "limit", 1, 100)
    receipts = []
    recover(service, limit=limit)
    with service.store.write() as tx:
        rows = tx.connection.execute(
            "SELECT * FROM jobs WHERE state='pending' AND cancel_requested=0 AND due_us<=? ORDER BY due_us,id LIMIT ?",
            (tx.now_us, limit),
        ).fetchall()
        for row in rows:
            job = dict(row)
            try:
                validate_target(service, tx, job)
            except CoordinationError as error:
                result = {"code": error.code, "phase": "target_validation"}
                tx.connection.execute(
                    "UPDATE jobs SET state='failed',last_result_json=?,version=version+1,updated_us=? WHERE id=? AND state='pending'",
                    (_json(result), tx.now_us, job["id"]),
                )
                if job["operation_id"]:
                    op = tx.connection.execute(
                        "SELECT state FROM operations WHERE id=?", (job["operation_id"],)
                    ).fetchone()
                    if op is not None and op["state"] == "queued":
                        service.finish_operation(tx, job["operation_id"], "failed", result=result)
                tx.event("jobs", "failed", job["id"], job["actor_id"], result)
                _notice(tx, service, job, "failed")
                continue
            if job["operation_id"]:
                receipts.append({"operation_id": job["operation_id"], "job_id": job["id"]})
                continue
            ctx = _context(service, job)
            receipt = service.enqueue(
                tx,
                ctx,
                "job.execute",
                {"job_id": job["id"]},
                key=f"job:{job['id']}:{job['version']}",
            )
            operation_id = receipt.get("operation_id", receipt.get("id"))
            if not operation_id:
                _error("OPERATION_FAILED", "Enqueue returned no operation identity")
            tx.connection.execute(
                "UPDATE jobs SET operation_id=?,updated_us=? WHERE id=? AND state='pending' AND cancel_requested=0",
                (operation_id, tx.now_us, job["id"]),
            )
            receipts.append({"operation_id": operation_id, "job_id": job["id"]})
    return receipts


def _validate_native_target(target, note):
    if target["harness"] not in {"codex", "claude", "grok"}:
        _error("INVALID_ARGUMENT", "Native resume is unsupported for this harness")
    _id(target["native_session_id"])
    if note.startswith("-"):
        _error("INVALID_ARGUMENT", "Resume note cannot be a CLI option")


def resume_argv(target, note, *, executables=None):
    """Fixed built-ins only; no shell, user argv, permission switches or env changes."""
    _validate_native_target(target, _text(note, "note"))
    harness = target["harness"]
    executable = (executables or {}).get(harness) or shutil.which(harness)
    if executable is None:
        raise FileNotFoundError("Native executable unavailable")
    if (
        not isinstance(executable, str)
        or not Path(executable).is_absolute()
        or "\x00" in executable
    ):
        _error("INVALID_ARGUMENT", "Configured native executable must be an absolute path")
    native = target["native_session_id"]
    return {
        "codex": [executable, "exec", "resume", "--json", native, note],
        "claude": [executable, "-p", "--resume", native, note],
        "grok": [executable, "-p", note, "--resume", native],
    }[harness]


def terminate_owned(process, identity):
    """Signal only an exact worker-launched leader in its own new process group."""
    if (
        identity is None
        or identity.get("pid") != process.pid
        or identity.get("pgid") != process.pid
        or process_status(identity) != "alive"
    ):
        return group_status(identity) == "gone"
    try:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            # Recheck the leader's kernel start identity before a second signal.
            if process_status(identity) != "alive":
                return group_status(identity) == "gone"
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return group_status(identity) == "gone"


def _safe_failure(error, phase):
    # No exception prose, argv or captured output reaches a receipt.
    return {"phase": phase, "error_type": type(error).__name__}


def _fault(fault, boundary):
    if fault is not None:
        fault(boundary)


def _current(tx, operation_id, token):
    row = tx.connection.execute(
        """SELECT jobs.* FROM jobs JOIN operations o ON o.id=jobs.operation_id
        WHERE o.id=? AND o.state='running' AND o.claim_token=? AND jobs.state='running' """,
        (operation_id, token),
    ).fetchone()
    return dict(row) if row is not None else None


def _settle(service, operation, state, result, *, retry=False, stopped=False):
    """One result CAS; cancellation wins over every completion/retry race."""
    with service.store.write() as tx:
        job = _current(tx, operation["id"], operation["claim_token"])
        if job is None:
            return {"state": "uncertain", "operation_id": operation["id"]}
        if job["cancel_requested"]:
            state, retry = ("cancelled" if stopped else "uncertain"), False
        elif retry:
            state = "pending"
        due = job["due_us"]
        if retry:
            # Scheduling jitter is not a security credential.
            delay = int(
                min(3600, 60 * 2 ** min(max(job["attempts"] - 1, 0), 6) * random.uniform(0.8, 1.2))
                * 1000_000
            )
            due = min(job["delivery_deadline_us"], tx.now_us + delay)
        tx.connection.execute(
            """UPDATE jobs SET state=?,last_result_json=?,due_us=?,
            operation_id=CASE WHEN ? THEN NULL ELSE operation_id END,
            updated_us=?,version=version+1 WHERE id=? AND operation_id=? AND state='running' """,
            (state, _json(result), due, retry, tx.now_us, job["id"], operation["id"]),
        )
        tx.connection.execute(
            """UPDATE job_attempts SET ended_us=?,outcome_json=?
            WHERE operation_id=? AND claim_token=? AND ended_us IS NULL""",
            (tx.now_us, _json(result), operation["id"], operation["claim_token"]),
        )
        opstate = "failed" if retry else state
        service.finish_operation(
            tx, operation["id"], opstate, result=result, claim_token=operation["claim_token"]
        )
        tx.event("jobs", state, job["id"], job["actor_id"], {"operation_id": operation["id"]})
        _notice(tx, service, job, state)
        return {
            "job_id": job["id"],
            "operation_id": operation["id"],
            "state": state,
            "result": result,
        }


def _notice(tx, service, job, state):
    if (
        state not in {"failed", "uncertain"}
        or tx.connection.execute(
            "SELECT 1 FROM job_notifications WHERE job_id=?", (job["id"],)
        ).fetchone()
    ):
        return
    actor = tx.connection.execute(
        "SELECT archived,current_task_generation FROM actors WHERE id=?", (job["actor_id"],)
    ).fetchone()
    if (
        actor is None
        or actor["archived"]
        or actor["current_task_generation"] != job["task_generation"]
    ):
        return  # The durable action remains inspectable; never forge a new sender generation.
    from .messages import append_message

    message = append_message(
        tx,
        _context(service, job),
        recipient_ids=[job["actor_id"]],
        kind="job",
        subject=f"Scheduled {job['kind']} requires review",
        body=f"Job {job['id']} is {state}. Inspect job.get and reconcile the recorded effect before retrying.",
        thread=f"job:{job['id']}",
    )
    tx.connection.execute("INSERT INTO job_notifications VALUES (?,?)", (job["id"], message["id"]))


def _pending_query(context, *, new_only=False, filters=None):
    from .messages import pending_filters

    filters = pending_filters(filters)
    values = {
        "actor": context.actor_id,
        "operator": context.operator,
        "filter_actor": filters.get("actor_id"),
        "task": filters.get("task"),
        "path": filters.get("path"),
    }
    query = """SELECT j.*,e.sequence FROM jobs j
        JOIN events e ON e.record_id=j.id AND e.domain='jobs'
        JOIN assignments a ON a.generation=j.task_generation
        WHERE (:operator OR j.actor_id=:actor) AND j.state IN ('failed','uncertain')
        AND (:filter_actor IS NULL OR j.actor_id=:filter_actor)
        AND (:task IS NULL OR a.task=:task)
        AND (:path IS NULL OR :path='.')
        AND e.sequence=(SELECT MAX(e2.sequence) FROM events e2
                         WHERE e2.domain='jobs' AND e2.record_id=j.id)"""
    if new_only and not context.operator:
        query += """ AND NOT EXISTS (SELECT 1 FROM action_presentations p
            WHERE p.actor_id=:actor AND p.kind='job' AND p.record_id=j.id AND p.version=j.version)"""
    return query, values


def select_actions(tx, context, *, after=None, limit=20, new_only=False, filters=None):
    """Current failure previews, independent of native notification handling."""
    query, values = _pending_query(context, new_only=new_only, filters=filters)
    values.update(
        {
            "after": 0 if after is None else _integer(after, "after"),
            "limit": _integer(limit, "limit", 1, 100),
        }
    )
    rows = tx.connection.execute(
        query + " AND e.sequence>:after ORDER BY e.sequence LIMIT :limit", values
    ).fetchall()
    actions = []
    for row in rows:
        notices = tx.connection.execute(
            "SELECT message_id FROM job_notifications WHERE job_id=? ORDER BY message_id LIMIT 17",
            (row["id"],),
        ).fetchall()
        actions.append(
            {
                "kind": "job",
                "id": row["id"],
                "version": row["version"],
                "actor_id": row["actor_id"],
                "state": row["state"],
                "sequence": row["sequence"],
                "summary": f"Scheduled {row['kind']} is {row['state']}",
                "message_ids": [n["message_id"] for n in notices[:16]],
                "message_ids_more": len(notices) > 16,
                "next_action": "job.get",
            }
        )
    return actions


def count_pending(tx, context, *, new_only=False, filters=None):
    query, values = _pending_query(context, new_only=new_only, filters=filters)
    return {
        "jobs": tx.connection.execute("SELECT COUNT(*) FROM (" + query + ")", values).fetchone()[0]
    }


def execute_operation(service, operation, *, fault=None, stop_event=None):
    """Execute one core-claimed job operation with all process I/O outside writes.

    Fault injection boundaries are for crash regressions. BaseException simulates
    abrupt death and deliberately leaves the durable intent for core recovery.
    """
    if (
        not isinstance(operation, dict)
        or operation.get("state") != "running"
        or not operation.get("claim_token")
    ):
        _error("INVALID_ARGUMENT", "A core-claimed running operation is required")
    token = operation["claim_token"]
    with service.store.write() as tx:
        op = tx.connection.execute(
            "SELECT * FROM operations WHERE id=? AND state='running' AND claim_token=?",
            (operation["id"], token),
        ).fetchone()
        if op is None:
            _error("STALE_VERSION", "Operation claim changed")
        row = tx.connection.execute(
            "SELECT * FROM jobs WHERE operation_id=? AND state IN ('pending','running')",
            (operation["id"],),
        ).fetchone()
        if row is None:
            _error("STALE_VERSION", "Job admission changed")
        job = dict(row)
        old_attempt = tx.connection.execute(
            "SELECT * FROM job_attempts WHERE operation_id=?", (operation["id"],)
        ).fetchone()
        if old_attempt is not None and old_attempt["launch_intent_us"] is not None:
            _error("RECONCILIATION_REQUIRED", "An existing launch intent cannot be retried")
        if job["cancel_requested"]:
            service.finish_operation(
                tx, operation["id"], "cancelled", result={"cancelled": True}, claim_token=token
            )
            tx.connection.execute(
                "UPDATE jobs SET state='cancelled',updated_us=?,version=version+1 WHERE id=?",
                (tx.now_us, job["id"]),
            )
            return {"job_id": job["id"], "state": "cancelled"}
        try:
            validate_target(service, tx, job)
        except CoordinationError as error:
            result = {"code": error.code, "phase": "target_validation"}
            tx.connection.execute(
                "UPDATE jobs SET state='failed',last_result_json=?,version=version+1,updated_us=? WHERE id=?",
                (_json(result), tx.now_us, job["id"]),
            )
            service.finish_operation(
                tx, operation["id"], "failed", result=result, claim_token=token
            )
            tx.event("jobs", "failed", job["id"], job["actor_id"], result)
            _notice(tx, service, job, "failed")
            return {"job_id": job["id"], "state": "failed", "result": result}
        if (
            job["kind"] == "resume"
            and tx.connection.execute(
                "SELECT 1 FROM jobs WHERE actor_id=? AND kind='resume' AND state IN ('running','uncertain') AND id<>?",
                (job["actor_id"], job["id"]),
            ).fetchone()
        ):
            result = {"phase": "exclusive_resume", "code": "RECONCILIATION_REQUIRED"}
            tx.connection.execute(
                "UPDATE jobs SET state='failed',last_result_json=?,version=version+1,updated_us=? WHERE id=?",
                (_json(result), tx.now_us, job["id"]),
            )
            service.finish_operation(
                tx, operation["id"], "failed", result=result, claim_token=token
            )
            tx.event("jobs", "failed", job["id"], job["actor_id"], result)
            _notice(tx, service, job, "failed")
            return {"job_id": job["id"], "state": "failed", "result": result}
        tx.connection.execute(
            "UPDATE jobs SET state='running',attempts=attempts+1,version=version+1,updated_us=? WHERE id=? AND state='pending' AND cancel_requested=0",
            (tx.now_us, job["id"]),
        )
        if old_attempt is None:
            tx.connection.execute(
                """INSERT INTO job_attempts
                (id,job_id,operation_id,claim_token,started_us) VALUES (?,?,?,?,?)""",
                (str(uuid.uuid4()), job["id"], operation["id"], token, tx.now_us),
            )
        else:
            tx.connection.execute(
                "UPDATE job_attempts SET claim_token=?,started_us=? WHERE operation_id=? AND launch_intent_us IS NULL",
                (token, tx.now_us, operation["id"]),
            )
        payload = json.loads(job["payload_json"])
        tx.connection.execute(
            "UPDATE operations SET lease_until_us=? WHERE id=? AND claim_token=?",
            (tx.now_us + (payload["timeout_seconds"] + 30) * 1000_000, operation["id"], token),
        )
    _fault(fault, "after_claim")
    if job["kind"] == "reminder":
        try:
            return _execute_reminder(service, operation, fault=fault)
        except (sqlite3.OperationalError, CoordinationError) as error:
            # Native messaging either committed both message and receipt, or
            # rolled both back. Only a transient admission/storage error retries.
            transient = getattr(error, "sqlite_errorcode", None) in {
                sqlite3.SQLITE_BUSY,
                sqlite3.SQLITE_LOCKED,
            } or (isinstance(error, CoordinationError) and error.retryable)
            with service.store.read() as tx:
                current = _current(tx, operation["id"], token)
                retry = (
                    transient
                    and current is not None
                    and tx.now_us < current["delivery_deadline_us"]
                )
            return _settle(
                service, operation, "failed", _safe_failure(error, "reminder_delivery"), retry=retry
            )
    return _execute_resume(service, operation, job, payload, fault=fault, stop_event=stop_event)


def _execute_reminder(service, operation, *, fault=None):
    from .messages import append_message

    # Reminder delivery and its receipt are one native database transaction.
    with service.store.write() as tx:
        job = _current(tx, operation["id"], operation["claim_token"])
        if job is None:
            _error("STALE_VERSION", "Reminder claim changed")
        if job["cancel_requested"]:
            state, result = "cancelled", {"cancelled": True}
        elif tx.now_us >= job["delivery_deadline_us"]:
            state, result = "failed", {"phase": "delivery_deadline", "code": "OPERATION_FAILED"}
        else:
            validate_target(service, tx, job)
            existing = tx.connection.execute(
                """SELECT n.message_id FROM job_notifications n
                JOIN messages m ON m.id=n.message_id WHERE n.job_id=? AND m.kind='reminder' """,
                (job["id"],),
            ).fetchone()
            if existing:
                message_id = existing["message_id"]
            else:
                payload = json.loads(job["payload_json"])
                message = append_message(
                    tx,
                    _context(service, job),
                    recipient_ids=[job["actor_id"]],
                    kind="reminder",
                    subject=payload["subject"],
                    body=payload["note"],
                    thread=f"job:{job['id']}",
                )
                message_id = message["id"]
                tx.connection.execute(
                    "INSERT INTO job_notifications VALUES (?,?)", (job["id"], message_id)
                )
            state, result = "succeeded", {"message_id": message_id}
        _fault(fault, "before_reminder_commit")
        tx.connection.execute(
            "UPDATE jobs SET state=?,last_result_json=?,updated_us=?,version=version+1 WHERE id=? AND operation_id=?",
            (state, _json(result), tx.now_us, job["id"], operation["id"]),
        )
        tx.connection.execute(
            "UPDATE job_attempts SET ended_us=?,outcome_json=? WHERE operation_id=? AND claim_token=?",
            (tx.now_us, _json(result), operation["id"], operation["claim_token"]),
        )
        service.finish_operation(
            tx, operation["id"], state, result=result, claim_token=operation["claim_token"]
        )
        tx.event("jobs", state, job["id"], job["actor_id"], result)
        return {
            "job_id": job["id"],
            "operation_id": operation["id"],
            "state": state,
            "result": result,
        }


def _execute_resume(service, operation, job, payload, *, fault=None, stop_event=None):
    target = json.loads(job["target_json"])
    # An observer must positively identify this exact native conversation as
    # offline; process absence alone cannot establish native session presence.
    observer = service.adapters.get("observe_native")
    try:
        if (
            observer is None
            or observer(dict(target)) != "offline"
            or process_status(target["process_identity"]) != "gone"
        ):
            return _settle(
                service, operation, "failed", {"phase": "presence", "code": "NOT_AUTHORIZED"}
            )
        argv = resume_argv(target, payload["note"], executables=service.config.native_executables)
    except Exception as error:  # noqa: BLE001 -- retain observer/argv failures as job receipts
        return _settle(
            service,
            operation,
            "failed",
            _safe_failure(error, "prelaunch"),
            retry=job["attempts"] < 2,
        )
    if stop_event is not None and stop_event.is_set():
        return _settle(
            service,
            operation,
            "failed",
            {"phase": "stopped_before_launch"},
            retry=True,
            stopped=True,
        )
    pre_failure = None
    with service.store.write() as tx:
        current = _current(tx, operation["id"], operation["claim_token"])
        if current is None:
            _error("STALE_VERSION", "Resume claim changed")
        if current["cancel_requested"]:
            # No process exists yet, so cancellation is positively confirmed.
            pre_cancelled = True
        else:
            pre_cancelled = False
            try:
                validate_target(service, tx, current)
                if tx.connection.execute(
                    "SELECT 1 FROM bindings WHERE actor_id=? AND revoked_us IS NULL LIMIT 1",
                    (current["actor_id"],),
                ).fetchone():
                    _error("NOT_AUTHORIZED", "Native resume target reconnected before launch")
                service.require_effect(tx, operation)
            except CoordinationError as error:
                pre_failure = {"phase": "prelaunch_validation", "code": error.code}
            else:
                tx.connection.execute(
                    "UPDATE job_attempts SET launch_intent_us=? WHERE operation_id=? AND claim_token=?",
                    (tx.now_us, operation["id"], operation["claim_token"]),
                )
                tx.connection.execute(
                    "UPDATE operations SET effect_started_us=? WHERE id=? AND state='running' AND claim_token=?",
                    (tx.now_us, operation["id"], operation["claim_token"]),
                )
    if pre_cancelled:
        return _settle(service, operation, "cancelled", {"cancelled": True}, stopped=True)
    if pre_failure is not None:
        return _settle(service, operation, "failed", pre_failure, stopped=True)
    _fault(fault, "before_spawn")
    output = None
    descriptor = None
    try:
        log_directory = service.store.path.parent / "jobs"
        private_directory(log_directory)
        descriptor = os.open(
            log_directory / f"{job['id']}.log",
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
            0o600,
        )
        os.fchmod(descriptor, 0o600)
        output = os.fdopen(descriptor, "ab")
        descriptor = None
        process = subprocess.Popen(
            argv,
            cwd=service.workspace.root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except (OSError, CoordinationError) as error:
        if descriptor is not None:
            os.close(descriptor)
        if output is not None:
            output.close()
        # Positive prelaunch evidence permits a bounded fresh attempt.
        result = {**_safe_failure(error, "prelaunch"), "launched": False, "stopped": True}
        retry = isinstance(error, OSError) and job["attempts"] < 2
        return _settle(service, operation, "failed", result, retry=retry, stopped=True)
    try:
        _fault(fault, "after_spawn")
        os.set_blocking(process.stdout.fileno(), False)
        return _monitor_resume(
            service, operation, payload, process, output, fault=fault, stop_event=stop_event
        )
    finally:
        output.close()
        process.stdout.close()


def _drain(process, output, remaining):
    """Drain bounded pipe chunks; retain a configured prefix without extra threads."""
    eof = False
    for _ in range(4):
        try:
            chunk = os.read(process.stdout.fileno(), 65536)
        except BlockingIOError:
            break
        if not chunk:
            eof = True
            break
        retained = chunk[:remaining]
        if retained:
            output.write(retained)
            remaining -= len(retained)
    output.flush()
    return remaining, eof


def _monitor_resume(service, operation, payload, process, output, *, fault=None, stop_event=None):
    identity = process_identity(process.pid)
    if identity is None or identity.get("pgid") != process.pid:
        return _settle(
            service,
            operation,
            "uncertain",
            {"phase": "process_identity", "code": "RECONCILIATION_REQUIRED"},
        )
    with service.store.write() as tx:
        if _current(tx, operation["id"], operation["claim_token"]) is None:
            # Lost ownership cannot authorize signalling; leave uncertainty.
            return {"operation_id": operation["id"], "state": "uncertain"}
        tx.connection.execute(
            "UPDATE job_attempts SET process_identity_json=? WHERE operation_id=? AND claim_token=?",
            (_json(identity), operation["id"], operation["claim_token"]),
        )
        tx.connection.execute(
            "UPDATE operations SET owner_identity_json=? WHERE id=? AND claim_token=?",
            (_json(identity), operation["id"], operation["claim_token"]),
        )
    _fault(fault, "after_identity")
    deadline = time.monotonic() + payload["timeout_seconds"]
    remaining = max(0, service.config.jobs_log_max_bytes - os.fstat(output.fileno()).st_size)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        return _poll_resume(
            service, operation, process, output, identity, deadline, remaining, selector, stop_event
        )
    finally:
        selector.close()


def _validate_running_resume(service, tx, job, operation, launched_identity):
    """Adopt one originating execution of this exact launched attempt, atomically."""
    actor = service.require_generation(tx, job["actor_id"], job["task_generation"])
    attempt = tx.connection.execute(
        "SELECT * FROM job_attempts WHERE job_id=? AND operation_id=? AND claim_token=?",
        (job["id"], operation["id"], operation["claim_token"]),
    ).fetchone()
    running = tx.connection.execute(
        "SELECT * FROM operations WHERE id=? AND state='running' AND claim_token=?",
        (operation["id"], operation["claim_token"]),
    ).fetchone()
    if (
        job["cancel_requested"] or attempt is None or running is None
        or attempt["ended_us"] is not None or attempt["launch_intent_us"] is None
        or attempt["process_identity_json"] is None
        or json.loads(attempt["process_identity_json"]) != launched_identity
        or running["actor_id"] != job["actor_id"]
        or running["task_generation"] != job["task_generation"]
        or running["owner_identity_json"] is None
        or json.loads(running["owner_identity_json"]) != launched_identity
        or running["effect_started_us"] is None
        or running["lease_until_us"] is None or running["lease_until_us"] <= tx.now_us
    ):
        _error("NOT_AUTHORIZED", "Native resume attempt no longer owns its launch")
    # A reconnect may race external process creation. Only connections proved to
    # belong to this launched process are compatible with continued ownership.
    for binding in tx.connection.execute(
        "SELECT native_evidence_json FROM bindings WHERE actor_id=? AND revoked_us IS NULL",
        (actor["id"],),
    ):
        if json.loads(binding[0]).get("process_identity") != launched_identity:
            _error("NOT_AUTHORIZED", "Another native connection invalidated resume ownership")
    adopted = attempt["execution_generation"]
    current = actor["current_execution_generation"]
    if adopted is None and current == job["execution_generation"]:
        validate_target(service, tx, job)
        if running["execution_generation"] != job["execution_generation"]:
            _error("STALE_GENERATION", "Resume operation execution changed")
    else:
        target = json.loads(job["target_json"])
        execution = tx.connection.execute(
            "SELECT process_identity_json FROM executions WHERE generation=? AND actor_id=?",
            (current, actor["id"]),
        ).fetchone()
        original = tx.connection.execute(
            "SELECT process_identity_json FROM executions WHERE generation=? AND actor_id=?",
            (job["execution_generation"], actor["id"]),
        ).fetchone()
        if (
            actor["archived"] or actor.get("child_id")
            or actor["harness"] != target["harness"]
            or actor["native_session_id"] != target["native_session_id"]
            or actor["process_identity"] != launched_identity
            or execution is None or execution[0] is None
            or json.loads(execution[0]) != launched_identity
            or original is None or original[0] is None
            or json.loads(original[0]) != target["process_identity"]
            or (adopted is not None and adopted != current)
        ):
            _error("STALE_GENERATION", "Native resume execution is not its owned continuation")
        if (
            actor["reported_state"] not in {"working", "idle", "waiting", "blocked"}
            or actor["resume_enabled"] is not True or actor["resume_inhibited"] is not False
        ):
            _error("NOT_AUTHORIZED", "Native resume consent is absent or inhibited")
        if adopted is None:
            advanced = tx.connection.execute(
                """UPDATE operations SET execution_generation=?
                WHERE id=? AND claim_token=? AND state='running' AND execution_generation=?
                  AND lease_until_us>? AND effect_started_us IS NOT NULL""",
                (current, operation["id"], operation["claim_token"], job["execution_generation"], tx.now_us),
            ).rowcount
            captured = tx.connection.execute(
                """UPDATE job_attempts SET execution_generation=?
                WHERE id=? AND claim_token=? AND execution_generation IS NULL AND ended_us IS NULL""",
                (current, attempt["id"], operation["claim_token"]),
            ).rowcount
            if advanced != 1 or captured != 1:
                _error("STALE_GENERATION", "Native resume execution adoption changed")
            tx.event("jobs", "execution_adopted", job["id"], actor["id"], {
                "operation_id": operation["id"], "execution_generation": current,
            })
        elif running["execution_generation"] != adopted:
            _error("STALE_GENERATION", "Resume operation lost its adopted execution")
    service.require_effect(tx, operation, continuing=True)


def _poll_resume(
    service, operation, process, output, identity, deadline, remaining, selector, stop_event
):
    next_context_check = 0
    revoked = False
    pipe_open = True
    while True:
        remaining, eof = _drain(process, output, remaining)
        if eof and pipe_open:
            selector.unregister(process.stdout)
            pipe_open = False
        if time.monotonic() >= next_context_check:
            try:
                with service.store.write(maintenance=True) as tx:
                    current = _current(tx, operation["id"], operation["claim_token"])
                    if current is not None:
                        _validate_running_resume(service, tx, current, operation, identity)
                    revoked = current is None or current["cancel_requested"]
            except CoordinationError:
                revoked = True
            next_context_check = time.monotonic() + 0.2
        timed_out = time.monotonic() >= deadline
        interrupted = stop_event is not None and stop_event.is_set()
        if revoked or timed_out or interrupted:
            stopped = terminate_owned(process, identity)
            return _settle(
                service,
                operation,
                "uncertain",
                {"phase": "timeout" if timed_out else "cancelled_or_revoked", "stopped": stopped},
                stopped=stopped,
            )
        returncode = process.poll()
        if returncode is None:
            selector.select(timeout=min(0.2, max(0.001, deadline - time.monotonic())))
            continue
        _drain(process, output, remaining)
        # Leader success is not proof that all tools it spawned have stopped.
        stopped = group_status(identity) == "gone"
        state = "succeeded" if returncode == 0 and stopped else "uncertain"
        return _settle(
            service,
            operation,
            state,
            {"returncode": returncode, "stopped": stopped},
            stopped=stopped,
        )


def recover(service, *, limit=100):
    """Align interrupted jobs with durable core receipts without relaunching."""
    limit = _integer(limit, "limit", 1, 100)
    with service.store.write() as tx:
        rows = tx.connection.execute(
            """SELECT j.*,o.state AS operation_state FROM jobs j
            JOIN operations o ON o.id=j.operation_id WHERE j.state IN ('pending','running')
            AND o.state IN ('failed','uncertain','cancelled') ORDER BY j.id LIMIT ?""",
            (limit,),
        ).fetchall()
        for row in rows:
            state = row["operation_state"]
            # A cancelled core claim does not prove spawned descendants stopped.
            attempted = tx.connection.execute(
                "SELECT launch_intent_us FROM job_attempts WHERE operation_id=?",
                (row["operation_id"],),
            ).fetchone()
            if attempted and attempted["launch_intent_us"] is not None:
                state = "uncertain"
            elif row["cancel_requested"]:
                state = "cancelled"
            tx.connection.execute(
                "UPDATE jobs SET state=?,version=version+1,updated_us=? WHERE id=? AND state IN ('pending','running')",
                (state, tx.now_us, row["id"]),
            )
            tx.event("jobs", state, row["id"], row["actor_id"], {"phase": "recovery"})
            _notice(tx, service, dict(row), state)
        return len(rows)
