"""Meaningful work assertions, scope discovery and retained evidence."""
from __future__ import annotations

import json
import uuid

from . import identity
from .core import (
    CoordinationError,
    Operation,
    bounded_text,
    canonical_json,
    identifier,
    integer,
    normalize_paths,
    validate_fields,
)

SCHEMA = (
    """CREATE TABLE activities (
        id TEXT PRIMARY KEY, actor_id TEXT NOT NULL REFERENCES actors(id),
        task_generation TEXT NOT NULL REFERENCES assignments(generation), task TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('working','idle','blocked','paused','completed')),
        note TEXT NOT NULL, evidence_json TEXT NOT NULL, sequence INTEGER NOT NULL UNIQUE REFERENCES events(sequence),
        created_us INTEGER NOT NULL, mode TEXT NOT NULL DEFAULT 'canonical' CHECK(mode IN ('canonical','shared_report')))""",
    "CREATE INDEX activities_actor_sequence ON activities(actor_id,sequence)",
    """CREATE TABLE activity_paths (
        activity_id TEXT NOT NULL REFERENCES activities(id), path TEXT NOT NULL,
        PRIMARY KEY(activity_id,path))""",
    "CREATE INDEX activity_paths_scope ON activity_paths(path,activity_id)",
    """CREATE TABLE current_activity (
        actor_id TEXT PRIMARY KEY REFERENCES actors(id), activity_id TEXT NOT NULL UNIQUE REFERENCES activities(id))""",
    """CREATE TABLE shared_activity_current (
        actor_id TEXT NOT NULL REFERENCES actors(id), task TEXT NOT NULL, paths_json TEXT NOT NULL,
        activity_id TEXT NOT NULL UNIQUE REFERENCES activities(id), PRIMARY KEY(actor_id,task,paths_json))""",
    """CREATE TABLE intents (
        id TEXT PRIMARY KEY, actor_id TEXT NOT NULL REFERENCES actors(id), task_generation TEXT NOT NULL REFERENCES assignments(generation),
        path TEXT NOT NULL, purpose TEXT NOT NULL, invariants_json TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('active','completed','withdrawn')), updated_us INTEGER NOT NULL,
        UNIQUE(actor_id,task_generation,path))""",
    "CREATE INDEX intents_path ON intents(path,state)",
    """CREATE TABLE retained_changes (
        id TEXT PRIMARY KEY, actor_id TEXT NOT NULL REFERENCES actors(id), source_json TEXT NOT NULL,
        sequence INTEGER NOT NULL UNIQUE REFERENCES events(sequence), created_us INTEGER NOT NULL)""",
)

STATES = {"working", "idle", "blocked", "paused", "completed"}

# Shared reports are separate scopes on one connection. Canonical reports must
# still belong to the actor's current assignment; historical scopes are not work.
CURRENT_ACTIVITY_IDS = """SELECT c.activity_id FROM current_activity c
    JOIN activities ca ON ca.id=c.activity_id JOIN actors owner ON owner.id=c.actor_id
    WHERE owner.archived=0 AND ca.task_generation=owner.current_task_generation
    UNION SELECT s.activity_id FROM shared_activity_current s
    JOIN actors owner ON owner.id=s.actor_id WHERE owner.archived=0"""


def _fail(message, code="INVALID_ARGUMENT"):
    raise CoordinationError(code, message)


def _record(tx, row, *, preview=False, paths_after=None, limit=20):
    result = dict(row)
    encoded = result.pop("evidence_json")
    if preview and len(encoded.encode()) > 1024:
        result.update(evidence_omitted=True, evidence_bytes=len(encoded.encode()))
    else:
        result["evidence"] = json.loads(encoded)
    if preview and len(result["note"]) > 360:
        result.update(note_bytes=len(result["note"].encode()), note_excerpt=True)
        result["note"] = result["note"][:360]
    if paths_after is not None:
        paths_after = normalize_paths([paths_after], allow_root=True)[0]
    count = tx.connection.execute("SELECT COUNT(*) FROM activity_paths WHERE activity_id=?", (result["id"],)).fetchone()[0]
    rows = tx.connection.execute("SELECT path FROM activity_paths WHERE activity_id=? AND path>? ORDER BY path LIMIT ?",
                                 (result["id"], paths_after or "", (8 if preview else limit) + 1)).fetchall()
    selected, size = [], 0
    for path, in rows[:8 if preview else limit]:
        path_size = len(canonical_json(path).encode()) + 1
        if size + path_size > (8192 if preview else 32000):
            break
        selected.append(path)
        size += path_size
    result.update(paths=selected, path_count=count,
                  paths_after=selected[-1] if len(rows) > len(selected) and selected else None,
                  paths_more=len(rows) > len(selected))
    return result


def activity(service, context, args, tx):
    validate_fields(args, {"task", "paths", "note", "state", "evidence"})
    actor = service.require_actor(tx, context)
    service.require_generation(tx, actor["id"], context.task_generation)
    state = args.get("state", "working")
    if state not in STATES:
        _fail("Invalid reported work state")
    shared = context.identity_mode == "shared_group"
    prior = tx.connection.execute("SELECT a.* FROM current_activity c JOIN activities a ON a.id=c.activity_id WHERE c.actor_id=?", (actor["id"],)).fetchone()
    task = args.get("task")
    if task is None:
        assigned = tx.connection.execute("SELECT task FROM assignments WHERE generation=?", (actor["current_task_generation"],)).fetchone()
        task = assigned[0]
    task = bounded_text(task, "task", 256)
    inherit = state == "completed" and not shared and prior and prior["task"] == task and prior["task_generation"] == actor["current_task_generation"]
    if "note" not in args and not inherit:
        _fail("Activity requires a concrete note")
    note = bounded_text(args.get("note", prior["note"] if inherit else None), "note", 2000)
    inherited_paths = [r[0] for r in tx.connection.execute("SELECT path FROM activity_paths WHERE activity_id=? ORDER BY path", (prior["id"],))] if inherit else []
    scoped = list(normalize_paths(args.get("paths", inherited_paths), allow_root=True))
    if inherit and "paths" in args and scoped != inherited_paths:
        _fail("Completion scope must equal this task's current activity scope")
    evidence = args.get("evidence", json.loads(prior["evidence_json"]) if inherit else "")
    encoded = canonical_json(evidence)
    if len(encoded.encode()) > 8192:
        _fail("Evidence references exceed 8192 bytes")
    if shared:
        prior = tx.connection.execute("SELECT a.* FROM shared_activity_current s JOIN activities a ON a.id=s.activity_id WHERE s.actor_id=? AND s.task=? AND s.paths_json=?", (actor["id"], task, canonical_json(scoped))).fetchone()
    if prior and (shared or prior["task_generation"] == actor["current_task_generation"]) and (prior["task"], prior["state"], prior["note"], prior["evidence_json"]) == (task, state, note, encoded):
        previous_paths = [r[0] for r in tx.connection.execute("SELECT path FROM activity_paths WHERE activity_id=? ORDER BY path", (prior["id"],))]
        if previous_paths == scoped:
            return {"recorded": False, "activity": _record(tx, prior, preview=True)}
    if not shared:
        actor = identity.assign_task(tx, context, task)
    elif state not in {"paused", "completed"}:
        unfinished = tx.connection.execute("SELECT COUNT(*) FROM shared_activity_current s JOIN activities a ON a.id=s.activity_id WHERE s.actor_id=? AND a.state NOT IN ('paused','completed')", (actor["id"],)).fetchone()[0]
        if not prior and unfinished >= 256:
            _fail("Shared report capacity reached; complete existing reported scopes", "SERVICE_BUSY")
    activity_id = str(uuid.uuid4())
    seq = tx.event("work", "activity", activity_id, actor["id"], {"mode": "shared_report" if shared else "canonical"})
    tx.connection.execute("INSERT INTO activities VALUES (?,?,?,?,?,?,?,?,?,?)", (
        activity_id, actor["id"], actor["current_task_generation"], task, state, note, encoded,
        seq, tx.now_us, "shared_report" if shared else "canonical"))
    tx.connection.executemany("INSERT INTO activity_paths VALUES (?,?)", [(activity_id, p) for p in scoped])
    if shared:
        tx.connection.execute("INSERT INTO shared_activity_current VALUES (?,?,?,?) ON CONFLICT(actor_id,task,paths_json) DO UPDATE SET activity_id=excluded.activity_id",
                              (actor["id"], task, canonical_json(scoped), activity_id))
    else:
        tx.connection.execute("INSERT INTO current_activity VALUES (?,?) ON CONFLICT(actor_id) DO UPDATE SET activity_id=excluded.activity_id", (actor["id"], activity_id))
        # Reported work state never releases an execution lease or a Git grant.
        tx.connection.execute("UPDATE actors SET reported_state=? WHERE id=?", (state, actor["id"]))
    row = tx.connection.execute("SELECT * FROM activities WHERE id=?", (activity_id,)).fetchone()
    result = {"recorded": True, "activity": _record(tx, row, preview=True), "identity_mode": "shared_group" if shared else "native_actor"}
    previous_paths = [r[0] for r in tx.connection.execute("SELECT path FROM activity_paths WHERE activity_id=? ORDER BY path", (prior["id"],))] if prior else []
    if scoped and state not in {"completed", "paused"} and (not prior or prior["task"] != task or previous_paths != scoped):
        result["scope_context"] = _scope_context(tx, actor["id"], scoped)
    return result


def _scope_context(tx, actor_id, paths):
    rows = tx.connection.execute(f"""SELECT a.id,a.actor_id,a.task,a.state,a.mode,a.sequence,
        substr(a.note,1,360) AS note_preview,presence.observed_state,presence.observed_us
        FROM activities a JOIN ({CURRENT_ACTIVITY_IDS}) c ON c.activity_id=a.id
        LEFT JOIN presence ON presence.actor_id=a.actor_id
        WHERE a.actor_id!=? AND a.state NOT IN ('completed','paused')
        AND EXISTS (SELECT 1 FROM activity_paths p JOIN json_each(?) q
            ON p.path=q.value OR p.path='.' OR q.value='.' OR instr(p.path,q.value||'/')=1
              OR instr(q.value,p.path||'/')=1 WHERE p.activity_id=a.id)
        ORDER BY a.sequence DESC LIMIT 4""", (actor_id, canonical_json(paths))).fetchall()
    return {"items": [dict(row) for row in rows[:3]], "more": len(rows) > 3,
            "basis": "Advisory scope overlap, not ownership or live presence. Folder overlap alone needs no message or wait."}


def activity_select(tx, context, *, paths=(), after=0, limit=20, current=True):
    scoped = normalize_paths(paths, allow_root=True)
    after, limit = integer(after, "after", 0, 2**63-1), integer(limit, "limit", 1, 100)
    sql = "SELECT a.* FROM activities a"
    params = [after]
    if current:
        # Drive discovery from the small current set. Correlated EXISTS checks
        # otherwise scan that set for each historical activity before LIMIT.
        sql += f" JOIN ({CURRENT_ACTIVITY_IDS}) c ON c.activity_id=a.id"
    sql += " WHERE a.sequence>?"
    if scoped:
        sql += """ AND EXISTS (SELECT 1 FROM activity_paths p JOIN json_each(?) q
            ON p.path=q.value OR p.path='.' OR q.value='.' OR instr(p.path,q.value||'/')=1
              OR instr(q.value,p.path||'/')=1 WHERE p.activity_id=a.id)"""
        params.append(canonical_json(scoped))
    rows = tx.connection.execute(sql + " ORDER BY a.sequence LIMIT ?", (*params, limit + 1)).fetchall()
    selected, size = [], 0
    for row in rows[:limit]:
        item = _record(tx, row, preview=True)
        item_size = len(canonical_json(item).encode()) + 1
        if size + item_size > 40_000:
            break
        selected.append(item)
        size += item_size
    if rows and not selected:
        _fail("Activity cannot fit its discovery page; retrieve its detail", "INVALID_ARGUMENT")
    return {"activities": selected, "after": selected[-1]["sequence"] if len(rows) > len(selected) else None,
            "basis": "Reported intent and evidence references; not verified liveness, ownership or runtime proof."}


def activities(service, context, args, tx):
    validate_fields(args, {"paths", "after", "limit", "current"})
    if type(args.get("current", True)) is not bool:
        _fail("current must be a boolean")
    return activity_select(tx, context, paths=args.get("paths", ()), after=args.get("after", 0), limit=args.get("limit", 20), current=args.get("current", True))


def intent(service, context, args, tx):
    validate_fields(args, {"paths", "purpose", "invariants", "state"}, {"paths", "purpose", "invariants"})
    actor = service.require_actor(tx, context)
    service.require_generation(tx, actor["id"], context.task_generation)
    scoped = normalize_paths(args["paths"], allow_root=True)
    if not scoped:
        _fail("Intent requires scope")
    purpose = bounded_text(args["purpose"], "purpose", 2000)
    invariants = args["invariants"]
    if not isinstance(invariants, list) or len(invariants) > 32:
        _fail("Invariants must be a list of at most 32 descriptions")
    for invariant in invariants:
        bounded_text(invariant, "invariant", 2000)
    state = args.get("state", "active")
    if state not in {"active", "completed", "withdrawn"}:
        _fail("Invalid intent state")
    result, recorded = [], False
    for path in scoped:
        old = tx.connection.execute("SELECT id,purpose,invariants_json,state FROM intents WHERE actor_id=? AND task_generation=? AND path=?", (actor["id"], actor["current_task_generation"], path)).fetchone()
        intent_id = old[0] if old else str(uuid.uuid4())
        result.append(intent_id)
        if old and (old["purpose"], old["invariants_json"], old["state"]) == (purpose, canonical_json(invariants), state):
            continue
        recorded = True
        tx.connection.execute("INSERT INTO intents VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(actor_id,task_generation,path) DO UPDATE SET purpose=excluded.purpose,invariants_json=excluded.invariants_json,state=excluded.state,updated_us=excluded.updated_us",
                              (intent_id, actor["id"], actor["current_task_generation"], path, purpose, canonical_json(invariants), state, tx.now_us))
        tx.event("work", "intent", intent_id, actor["id"], {
            "path": path, "purpose": purpose, "invariants": invariants,
            "state": state, "task_generation": actor["current_task_generation"], "task": actor["task"],
        })
    return {"ids": result, "state": state, "recorded": recorded,
            "instruction": "Intent describes purpose; it does not admit or lock file writes."}


def _outcomes(tx, paths, limit, before):
    if before is not None:
        integer(before, "outcomes_before", 1, 2**63 - 1)
    rows = tx.connection.execute("""SELECT a.id,a.actor_id,a.task,a.state,a.sequence,a.created_us,
        substr(a.note,1,360) AS note_preview,length(CAST(a.note AS BLOB)) AS note_bytes,
        length(CAST(a.evidence_json AS BLOB)) AS evidence_bytes
        FROM activities a WHERE a.state='completed' AND (? IS NULL OR a.sequence<?)
        AND EXISTS (SELECT 1 FROM activity_paths p JOIN json_each(?) q
            ON p.path=q.value OR p.path='.' OR q.value='.' OR instr(p.path,q.value||'/')=1
              OR instr(q.value,p.path||'/')=1 WHERE p.activity_id=a.id)
        ORDER BY a.sequence DESC LIMIT ?""", (before, before, canonical_json(paths), limit + 1)).fetchall()
    selected = [dict(row) for row in rows[:limit]]
    return {"outcomes": selected,
            "outcomes_before": selected[-1]["sequence"] if len(rows) > limit else None}


def evidence_select(tx, context, *, paths, limit=20, intents_after=None, outcomes_before=None):
    scoped = normalize_paths(paths, allow_root=True)
    if not scoped:
        _fail("Evidence lookup requires scope")
    limit = integer(limit, "limit", 1, 20)
    activities = activity_select(tx, context, paths=scoped, limit=limit)
    if intents_after is not None:
        identifier(intents_after, "intents_after")
    intent_rows = tx.connection.execute("""SELECT i.id,i.actor_id,i.task_generation,t.task,i.path,
        substr(i.purpose,1,360) AS purpose_preview,i.state,i.updated_us,
        json_array_length(i.invariants_json) AS invariant_count
        FROM intents i JOIN assignments t ON t.generation=i.task_generation WHERE (? IS NULL OR i.id>?)
          AND EXISTS (SELECT 1 FROM json_each(?) q WHERE i.path=q.value OR i.path='.' OR q.value='.'
            OR instr(i.path,q.value||'/')=1 OR instr(q.value,i.path||'/')=1)
        ORDER BY i.id LIMIT ?""", (intents_after, intents_after, canonical_json(scoped), limit + 1)).fetchall()
    intents = [dict(row) for row in intent_rows[:limit]]
    decisions = [dict(row) for row in tx.connection.execute("""SELECT d.id,d.state,d.subject,
        substr(d.response,1,360) AS response_preview,length(CAST(d.response AS BLOB)) AS response_bytes,
        d.version,d.sequence FROM decisions d
        WHERE (d.sender_id=? OR d.recipient_id=? OR (d.parent_id=? AND d.routing_state='escalated'))
          AND EXISTS (SELECT 1 FROM decision_paths p JOIN json_each(?) q ON p.path=q.value OR p.path='.' OR q.value='.'
            OR instr(p.path,q.value||'/')=1 OR instr(q.value,p.path||'/')=1 WHERE p.decision_id=d.id)
        ORDER BY d.sequence DESC LIMIT ?""", (context.actor_id, context.actor_id, context.actor_id, canonical_json(scoped), integer(limit, "limit", 1, 100)))]
    receipts = [dict(row) for row in tx.connection.execute("""SELECT r.id,r.producer_id,r.artifact,r.status,r.evidence_json,r.version,r.sequence
        FROM receipts r WHERE EXISTS (SELECT 1 FROM receipt_paths p JOIN json_each(?) q ON p.path=q.value
            OR instr(p.path,q.value||'/')=1 OR instr(q.value,p.path||'/')=1 WHERE p.receipt_id=r.id)
        ORDER BY r.sequence DESC LIMIT ?""", (canonical_json(scoped), limit))]
    for receipt in receipts:
        encoded = receipt.pop("evidence_json")
        if len(encoded.encode()) > 1024:
            receipt.update(evidence_bytes=len(encoded.encode()), evidence_omitted=True)
        else:
            receipt["evidence"] = json.loads(encoded)
    return {**activities, **_outcomes(tx, scoped, limit, outcomes_before), "intents": intents,
            "intents_after": intents[-1]["id"] if len(intent_rows) > limit else None,
            "decisions": decisions, "readiness": receipts,
            "basis": "Stored author assertions and references. Inspect current receipt hashes and actual checks before relying on them."}


def evidence(service, context, args, tx):
    validate_fields(args, {"paths", "limit", "intents_after", "outcomes_before"}, {"paths"})
    return evidence_select(tx, context, paths=args["paths"], limit=args.get("limit", 20),
                           intents_after=args.get("intents_after"), outcomes_before=args.get("outcomes_before"))


def evidence_detail(service, context, args, tx):
    validate_fields(args, {"kind", "id", "paths_after", "limit"}, {"kind", "id"})
    identifier(args["id"], "record")
    if args["kind"] == "activity":
        row = tx.connection.execute("SELECT * FROM activities WHERE id=?", (args["id"],)).fetchone()
        if row:
            return _record(tx, row, paths_after=args.get("paths_after"), limit=integer(args.get("limit", 20), "limit", 1, 100))
    elif args["kind"] == "intent":
        row = tx.connection.execute("SELECT i.*,t.task FROM intents i JOIN assignments t ON t.generation=i.task_generation WHERE i.id=?", (args["id"],)).fetchone()
        if row:
            result = dict(row)
            result["invariants"] = json.loads(result.pop("invariants_json"))
            return result
    elif args["kind"] == "change":
        row = tx.connection.execute("SELECT * FROM retained_changes WHERE id=?", (args["id"],)).fetchone()
        if row:
            result = dict(row)
            result["source"] = json.loads(result.pop("source_json"))
            return result
    elif args["kind"] == "decision":
        from .decisions import get
        return get(service, context, {"id": args["id"]}, tx)
    elif args["kind"] == "readiness":
        from .readiness import _receipt, public_receipt
        limit = integer(args.get("limit", 20), "limit", 1, 100)
        return public_receipt(_receipt(tx, args["id"]), path_after=args.get("paths_after"), limit=limit)
    else:
        _fail("Unknown evidence kind")
    _fail("Evidence record does not exist", "NOT_FOUND")


def operations():
    return (
        Operation("work.activity", activity, True, True, True),
        Operation("work.activities", activities, False, False, False),
        Operation("work.intent", intent, True, True, True),
        Operation("work.evidence", evidence, False, False, True),
        Operation("work.evidence_detail", evidence_detail, False, False, False),
    )
