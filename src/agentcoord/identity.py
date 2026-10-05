"""Native actors, immutable connection binding and conservative lifecycle proof."""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import secrets
import sys
import uuid
from pathlib import Path

from .core import (
    UNSET,
    Context,
    CoordinationError,
    Operation,
    bounded_text,
    canonical_json,
    identifier,
    integer,
    validate_fields,
)

HARNESSES = {"claude", "codex", "cursor", "grok"}
SCHEMA = (
    """CREATE TABLE actors(id TEXT PRIMARY KEY,harness TEXT NOT NULL,native_session_id TEXT NOT NULL,
        child_id TEXT NOT NULL DEFAULT '',parent_id TEXT REFERENCES actors(id),label TEXT NOT NULL,
        current_task_generation TEXT NOT NULL,current_execution_generation TEXT,
        reported_state TEXT NOT NULL DEFAULT 'idle' CHECK(reported_state IN ('working','idle','waiting','blocked','paused','completed')),
        archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1)),created_us INTEGER NOT NULL,
        resume_enabled INTEGER NOT NULL DEFAULT 0 CHECK(resume_enabled IN (0,1)),
        resume_inhibited INTEGER NOT NULL DEFAULT 0 CHECK(resume_inhibited IN (0,1)),
        process_identity_json TEXT CHECK(process_identity_json IS NULL OR json_valid(process_identity_json)),
        checkpoint_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(checkpoint_json)),
        metadata_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(metadata_json)),version INTEGER NOT NULL DEFAULT 1,
        UNIQUE(harness,native_session_id,child_id))""",
    """CREATE TABLE assignments(generation TEXT PRIMARY KEY,actor_id TEXT NOT NULL REFERENCES actors(id),
        task TEXT NOT NULL,created_us INTEGER NOT NULL)""",
    "CREATE INDEX assignments_actor ON assignments(actor_id,created_us)",
    """CREATE TABLE native_aliases(harness TEXT NOT NULL,native_id TEXT NOT NULL,child_id TEXT NOT NULL DEFAULT '',
        actor_id TEXT NOT NULL REFERENCES actors(id),provenance_json TEXT NOT NULL CHECK(json_valid(provenance_json)),
        PRIMARY KEY(harness,native_id,child_id))""",
    """CREATE TABLE executions(generation TEXT PRIMARY KEY,actor_id TEXT NOT NULL REFERENCES actors(id),
        native_run_id TEXT NOT NULL,process_identity_json TEXT CHECK(process_identity_json IS NULL OR json_valid(process_identity_json)),
        state TEXT NOT NULL CHECK(state IN ('running','ended','unknown')),created_us INTEGER NOT NULL,ended_us INTEGER,
        UNIQUE(actor_id,native_run_id))""",
    "CREATE INDEX executions_process ON executions(actor_id,generation)",
    """CREATE TABLE bindings(id TEXT PRIMARY KEY,actor_id TEXT REFERENCES actors(id),token_hash TEXT NOT NULL UNIQUE,
        connection_generation TEXT NOT NULL,transport TEXT NOT NULL CHECK(transport IN ('cli','mcp','hook','operator')),
        identity_mode TEXT NOT NULL CHECK(identity_mode IN ('native_owner','native_child','shared_group','operator')),
        native_evidence_json TEXT CHECK(native_evidence_json IS NULL OR json_valid(native_evidence_json)),
        revoked_us INTEGER,created_us INTEGER NOT NULL)""",
    "CREATE INDEX bindings_actor ON bindings(actor_id,revoked_us)",
    """CREATE TABLE parent_routes(child_id TEXT PRIMARY KEY REFERENCES actors(id),parent_id TEXT NOT NULL REFERENCES actors(id),
        child_task_generation TEXT NOT NULL,parent_task_generation TEXT NOT NULL,source TEXT NOT NULL,
        created_us INTEGER NOT NULL,CHECK(child_id<>parent_id))""",
    """CREATE TABLE presence(actor_id TEXT PRIMARY KEY REFERENCES actors(id),observed_state TEXT NOT NULL
        CHECK(observed_state IN ('running','idle','offline','unknown')),observed_us INTEGER NOT NULL,
        process_identity_json TEXT CHECK(process_identity_json IS NULL OR json_valid(process_identity_json)),
        confidence TEXT NOT NULL CHECK(confidence IN ('verified','unknown')))""",
)


def _actor(tx, actor_id: str) -> dict:
    row = tx.connection.execute("SELECT a.*,s.task FROM actors a LEFT JOIN assignments s ON s.generation=a.current_task_generation WHERE a.id=?",
                                (actor_id,)).fetchone()
    if row is None:
        raise CoordinationError("UNBOUND_ACTOR", "Actor is not registered")
    result = dict(row)
    for field, name in (("metadata_json", "metadata"), ("checkpoint_json", "checkpoint"),
                        ("process_identity_json", "process_identity")):
        result[name] = json.loads(result[field]) if result[field] is not None else None
    for field in ("archived", "resume_enabled", "resume_inhibited"):
        result[field] = bool(result[field])
    return result


def _actor_view(actor: dict) -> dict:
    """Expose native authority and state without private storage/history fields."""
    fields = (
        "id", "harness", "native_session_id", "child_id", "parent_id", "label",
        "current_task_generation", "current_execution_generation", "reported_state",
        "archived", "created_us", "resume_enabled", "resume_inhibited",
        "process_identity", "version", "task",
    )
    return {field: actor[field] for field in fields}


def actor_for_context(tx, context: Context) -> dict:
    if context.operator or context.actor_id is None:
        raise CoordinationError("UNBOUND_ACTOR", "Operation requires a native actor")
    if context.transport != "worker":
        binding = tx.connection.execute("SELECT actor_id,identity_mode,revoked_us FROM bindings WHERE id=?",
                                        (context.connection_id,)).fetchone()
        if (binding is None or binding["revoked_us"] is not None or
                binding["actor_id"] != context.actor_id or binding["identity_mode"] != context.identity_mode):
            raise CoordinationError("UNBOUND_ACTOR", "Connection is not bound to this native actor")
    return _actor(tx, context.actor_id)


def require_generation(tx, actor_id, expected_task, expected_execution=UNSET) -> dict:
    actor = _actor(tx, actor_id)
    if actor["current_task_generation"] != expected_task or (
            expected_execution is not UNSET and actor["current_execution_generation"] != expected_execution):
        raise CoordinationError("STALE_GENERATION", "Actor assignment or native execution changed")
    return actor


def assign_task(tx, context: Context, task: str) -> dict:
    task = bounded_text(task, "task", 1024)
    actor = actor_for_context(tx, context)
    if context.identity_mode == "shared_group":
        raise CoordinationError("NOT_AUTHORIZED", "Shared reports cannot reassign native group work")
    if actor["task"] == task:
        return actor
    if actor["reported_state"] in {"paused", "completed"}:
        raise CoordinationError("NOT_AUTHORIZED", "Explicitly resume paused/completed work before reassignment")
    if tx.connection.execute("SELECT 1 FROM operations WHERE actor_id=? AND state IN ('running','uncertain') LIMIT 1",
                             (actor["id"],)).fetchone():
        raise CoordinationError("RECONCILIATION_REQUIRED", "Reconcile own external operations before reassignment")
    generation = str(uuid.uuid4())
    tx.connection.execute("INSERT INTO assignments(generation,actor_id,task,created_us) VALUES (?,?,?,?)",
                          (generation, actor["id"], task, tx.now_us))
    tx.connection.execute("UPDATE actors SET current_task_generation=?,version=version+1 WHERE id=?",
                          (generation, actor["id"]))
    tx.event("identity", "assigned", actor["id"], actor["id"], {"task": task, "generation": generation})
    return _actor(tx, actor["id"])


def register_native(tx, native: dict) -> dict:
    allowed = {"harness", "native_session_id", "child_id", "parent_id", "label", "task", "native_run_id",
               "process_identity", "source", "execution_generation"}
    validate_fields(native, allowed, {"harness", "native_session_id"})
    harness = native["harness"]
    if harness not in HARNESSES:
        raise CoordinationError("INVALID_ARGUMENT", "Unsupported native harness")
    session = bounded_text(native["native_session_id"], "native_session_id", 256)
    child = bounded_text(native.get("child_id", ""), "child_id", 256, allow_empty=True)
    row = tx.connection.execute("SELECT id FROM actors WHERE harness=? AND native_session_id=? AND child_id=?",
                                (harness, session, child)).fetchone()
    alias = tx.connection.execute("SELECT actor_id FROM native_aliases WHERE harness=? AND native_id=? AND child_id=?",
                                  (harness, session, child)).fetchone()
    if row and alias and row[0] != alias[0]:
        raise CoordinationError("UNBOUND_ACTOR", "Native identity has conflicting mappings")
    actor_id = row[0] if row else alias[0] if alias else str(uuid.uuid4())
    if not row and not alias:
        generation = str(uuid.uuid4())
        task = bounded_text(native.get("task", ""), "task", 1024, allow_empty=True)
        label = bounded_text(native.get("label") or f"{harness}-{actor_id[:8]}", "label", 256)
        tx.connection.execute("INSERT INTO actors(id,harness,native_session_id,child_id,label,current_task_generation,created_us) VALUES (?,?,?,?,?,?,?)",
            (actor_id, harness, session, child, label, generation, tx.now_us))
        tx.connection.execute("INSERT INTO assignments VALUES (?,?,?,?)", (generation, actor_id, task, tx.now_us))
        tx.event("identity", "registered", actor_id, actor_id, {"harness": harness})
    if native.get("process_identity") is not None:
        proof = _validate_process_identity(native["process_identity"])
        tx.connection.execute("UPDATE actors SET process_identity_json=COALESCE(process_identity_json,?),archived=0 WHERE id=?",
                              (canonical_json(proof), actor_id))
    # Binding identifies an actor. A reconnect's inherited run token cannot
    # advance execution authority, reactivate an ended run, or rewind a new run.
    if native.get("native_run_id") is not None:
        bounded_text(native["native_run_id"], "native_run_id", 1024)
    if native.get("parent_id") is not None:
        parent_id = identifier(native["parent_id"], "parent_id")
        parent = _actor(tx, parent_id)
        actor = _actor(tx, actor_id)
        if (not child or parent["child_id"] or parent_id == actor_id or
                parent["harness"] != harness or parent["native_session_id"] != actor["native_session_id"] or
                native.get("source") != "native_child_start"):
            raise CoordinationError("NOT_AUTHORIZED", "Native parent routing requires verified child-start evidence")
        route = tx.connection.execute("SELECT parent_id FROM parent_routes WHERE child_id=?", (actor_id,)).fetchone()
        if route is not None and route[0] != parent_id:
            raise CoordinationError("NOT_AUTHORIZED", "An existing child cannot be reparented")
        tx.connection.execute("UPDATE actors SET parent_id=? WHERE id=?", (parent_id, actor_id))
        tx.connection.execute("INSERT INTO parent_routes VALUES (?,?,?,?,?,?) ON CONFLICT(child_id) DO UPDATE SET child_task_generation=excluded.child_task_generation,parent_task_generation=excluded.parent_task_generation,source=excluded.source",
            (actor_id, parent_id, actor["current_task_generation"], parent["current_task_generation"], "native_child_start", tx.now_us))
    return _actor(tx, actor_id)


def start_execution(tx, context: Context, *, native_run_id: str, process_proof=None) -> dict:
    """Record an explicit originating native start, never a connection refresh.

    The adapter supplies the native run token from the actual start event. The
    existing token is immutable: a delayed start for an older/ended run records
    no lifecycle transition and cannot reopen it.
    """
    actor = actor_for_context(tx, context)
    if context.identity_mode == "shared_group":
        raise CoordinationError("NOT_AUTHORIZED", "Shared MCP bindings cannot advance native execution")
    run = bounded_text(native_run_id, "native_run_id", 1024)
    previous = tx.connection.execute("SELECT generation,state FROM executions WHERE actor_id=? AND native_run_id=?",
                                     (actor["id"], run)).fetchone()
    if previous is not None:
        return {"actor": actor, "execution_generation": previous["generation"],
                "applied": False, "reason": "already_registered" if previous["state"] != "ended" else "ended_run"}
    state = json.loads(tx.connection.execute("SELECT value_json FROM meta WHERE key='service_state'").fetchone()[0])
    if state != "active":
        raise CoordinationError("AUTHORITY_FENCED", "Service cannot register another native execution")
    proof = _validate_process_identity(process_proof) if process_proof is not None else actor["process_identity"]
    generation = str(uuid.uuid4())
    tx.connection.execute("INSERT INTO executions(generation,actor_id,native_run_id,process_identity_json,state,created_us) VALUES (?,?,?,?,'running',?)",
        (generation, actor["id"], run, canonical_json(proof) if proof else None, tx.now_us))
    if actor["current_execution_generation"] is not None:
        tx.connection.execute("UPDATE executions SET state='unknown' WHERE generation=? AND state='running'",
                              (actor["current_execution_generation"],))
    # A new native execution does not override explicit pause/completion consent.
    tx.connection.execute("UPDATE actors SET current_execution_generation=?,archived=0,version=version+1 WHERE id=?",
                          (generation, actor["id"]))
    if proof is not None:
        tx.connection.execute("UPDATE actors SET process_identity_json=? WHERE id=?", (canonical_json(proof), actor["id"]))
    tx.event("identity", "execution_started", generation, actor["id"], {"native_run_id": run})
    return {"actor": _actor(tx, actor["id"]), "execution_generation": generation, "applied": True}


def insert_imported_actor(tx, record: dict) -> dict:
    """Insert an importer-supplied stable identity without guessing mailbox owners."""
    actor_id = identifier(record["id"], "actor ID")
    if tx.connection.execute("SELECT 1 FROM actors WHERE id=?", (actor_id,)).fetchone():
        return _actor(tx, actor_id)
    generation = identifier(record["current_task_generation"], "task generation")
    tx.connection.execute("""INSERT INTO actors(id,harness,native_session_id,child_id,parent_id,label,
        current_task_generation,current_execution_generation,reported_state,archived,created_us,
        resume_enabled,resume_inhibited,process_identity_json,checkpoint_json,metadata_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (actor_id, record["harness"], record["native_session_id"], record.get("child_id", ""),
         record.get("parent_id"), record["label"], generation, record.get("current_execution_generation"),
         record.get("reported_state", "idle"), int(record.get("archived", True)), record.get("created_us", tx.now_us),
         int(record.get("resume_enabled", False)), int(record.get("resume_inhibited", False)),
         canonical_json(record["process_identity"]) if record.get("process_identity") else None,
         canonical_json(record.get("checkpoint", {})), canonical_json(record.get("metadata", {}))))
    tx.connection.execute("INSERT INTO assignments VALUES (?,?,?,?)",
                          (generation, actor_id, record.get("task", ""), record.get("created_us", tx.now_us)))
    return _actor(tx, actor_id)


def bind_native(store, native: dict, *, transport="cli", connection_id=None) -> dict:
    if transport not in {"cli", "mcp", "hook"}:
        raise CoordinationError("INVALID_ARGUMENT", "Invalid native transport")
    if not native.get("native_session_id"):
        proof = native.get("process_identity")
        if proof is None:
            raise CoordinationError("UNBOUND_ACTOR", "Native startup evidence is missing; register the supported lifecycle hook")
        proof = _validate_process_identity(proof)
        with store.read() as tx:
            matches = tx.connection.execute("SELECT id,harness,native_session_id,child_id FROM actors WHERE process_identity_json=? AND harness=?",
                                             (canonical_json(proof), native.get("harness"))).fetchall()
        if len(matches) != 1:
            raise CoordinationError("UNBOUND_ACTOR", "Native process has no unique registered session; run native startup registration")
        native = {**native, "native_session_id": matches[0]["native_session_id"], "child_id": matches[0]["child_id"]}
    token, connection = secrets.token_urlsafe(32), connection_id or str(uuid.uuid4())
    with store.write() as tx:
        state = json.loads(tx.connection.execute("SELECT value_json FROM meta WHERE key='service_state'").fetchone()[0])
        if state == "draining":
            rows = tx.connection.execute("""SELECT id FROM actors WHERE harness=? AND native_session_id=? AND child_id=?
                UNION SELECT actor_id FROM native_aliases WHERE harness=? AND native_id=? AND child_id=?""",
                (native.get("harness"), native["native_session_id"], native.get("child_id", ""),
                 native.get("harness"), native["native_session_id"], native.get("child_id", ""))).fetchall()
            if len(rows) != 1:
                raise CoordinationError("AUTHORITY_FENCED", "Draining service only reconnects existing native identities")
            actor = _actor(tx, rows[0][0])
        else:
            actor = register_native(tx, native)
            tx.connection.execute("UPDATE actors SET archived=0 WHERE id=?", (actor["id"],))
        mode = "native_child" if actor["child_id"] else "shared_group" if transport == "mcp" else "native_owner"
        tx.connection.execute("INSERT INTO bindings(id,actor_id,token_hash,connection_generation,transport,identity_mode,native_evidence_json,created_us) VALUES (?,?,?,?,?,?,?,?)",
            (connection, actor["id"], hashlib.sha256(token.encode()).hexdigest(), str(uuid.uuid4()), transport, mode, canonical_json(native), tx.now_us))
        context = Context(store.workspace_id, actor["id"], connection, transport,
                          actor["current_task_generation"], actor["current_execution_generation"], identity_mode=mode)
    return {"context": context, "token": token}


def context_from_token(store, token: str, *, transport: str | None = None) -> Context:
    bounded_text(token, "connection credential", 256)
    with store.read() as tx:
        row = tx.connection.execute("SELECT * FROM bindings WHERE token_hash=? AND revoked_us IS NULL",
                                    (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        if row is None or (transport is not None and row["transport"] != transport):
            raise CoordinationError("UNBOUND_ACTOR", "Connection credential is unavailable")
        if row["identity_mode"] == "operator":
            return Context(store.workspace_id, None, row["id"], "operator", operator=True, identity_mode="operator")
        actor = _actor(tx, row["actor_id"])
        return Context(store.workspace_id, actor["id"], row["id"], row["transport"],
                       actor["current_task_generation"], actor["current_execution_generation"],
                       identity_mode=row["identity_mode"])


def authorized_parent(tx, recipient_id, captured_generation) -> dict | None:
    require_generation(tx, recipient_id, captured_generation)
    route = tx.connection.execute("SELECT * FROM parent_routes WHERE child_id=?", (recipient_id,)).fetchone()
    if route is None or route["child_task_generation"] != captured_generation:
        return None
    parent = _actor(tx, route["parent_id"])
    if parent["current_task_generation"] != route["parent_task_generation"] or parent["archived"]:
        return None
    return parent


def apply_lifecycle_event(tx, context, *, state, execution_generation, event, note="") -> dict:
    actor = actor_for_context(tx, context)
    if context.identity_mode == "shared_group":
        raise CoordinationError("NOT_AUTHORIZED", "Shared MCP bindings cannot change native lifecycle")
    if state not in {"working", "idle", "waiting", "blocked", "paused", "completed"}:
        raise CoordinationError("INVALID_ARGUMENT", "Invalid lifecycle state")
    if execution_generation is None or execution_generation != actor["current_execution_generation"]:
        tx.event("identity", "ambiguous_event" if execution_generation is None else "stale_event", actor["id"], actor["id"], {"event": event})
        return {"applied": False, "reason": "ambiguous_generation" if execution_generation is None else "stale_generation"}
    if actor["reported_state"] in {"paused", "completed"} and state not in {"paused", "completed"}:
        return {"applied": False, "reason": "explicit_resume_required"}
    if state == "completed" and tx.connection.execute("SELECT 1 FROM operations WHERE actor_id=? AND state IN ('running','uncertain') LIMIT 1", (actor["id"],)).fetchone():
        return {"applied": False, "reason": "reconciliation_required"}
    tx.connection.execute("UPDATE actors SET reported_state=?,resume_inhibited=CASE WHEN ? THEN 1 ELSE resume_inhibited END,checkpoint_json=?,version=version+1 WHERE id=?",
                          (state, int(state == "paused"), canonical_json({"note": note, "event": event}), actor["id"]))
    if event in {"stop", "end", "child_stop", "failure"}:
        tx.connection.execute("UPDATE executions SET state='ended',ended_us=COALESCE(ended_us,?) WHERE generation=?",
                              (tx.now_us, execution_generation))
    tx.event("identity", "lifecycle", actor["id"], actor["id"], {"state": state, "execution_generation": execution_generation})
    return {"applied": True, "state": state, "execution_generation": execution_generation}


class _BSDInfo(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint32) for name in (
        "flags", "status", "xstatus", "pid", "ppid", "uid", "gid", "ruid", "rgid", "svuid", "svgid", "reserved")] + [
        ("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32)] + [
        (name, ctypes.c_uint32) for name in ("nfiles", "pgid", "jobc", "tdev", "tpgid")] + [
        ("nice", ctypes.c_int32), ("start_sec", ctypes.c_uint64), ("start_usec", ctypes.c_uint64)]


def _process_info(pid: int) -> dict | None:
    if type(pid) is not int or pid <= 0:
        return None
    try:
        if sys.platform == "darwin":
            lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            info = _BSDInfo()
            lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
            lib.proc_pidinfo.restype = ctypes.c_int
            size = lib.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
            if size != ctypes.sizeof(info) or info.pid != pid or not info.start_sec:
                return None
            image = ctypes.create_string_buffer(4096)
            lib.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
            lib.proc_pidpath.restype = ctypes.c_int
            path = image.value.decode(errors="replace") if lib.proc_pidpath(pid, image, len(image)) > 0 else None
            return {"pid": pid, "pgid": info.pgid, "started": f"{info.start_sec}.{info.start_usec:06d}",
                    "source": "darwin-libproc", "ppid": info.ppid,
                    "name": (info.name or info.comm).decode(errors="replace"), "executable": path}
        if sys.platform.startswith("linux"):
            raw = Path(f"/proc/{pid}/stat").read_text()
            fields = raw[raw.rfind(")") + 2:].split()
            boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            return {"pid": pid, "pgid": int(fields[2]), "started": boot + ":" + fields[19],
                    "source": "linux-proc", "ppid": int(fields[1]),
                    "name": raw[raw.find("(") + 1:raw.rfind(")")], "executable": os.readlink(f"/proc/{pid}/exe")}
    except (OSError, ValueError, IndexError, AttributeError):
        pass
    return None


def process_identity(pid: int) -> dict | None:
    info = _process_info(pid)
    return {key: info[key] for key in ("pid", "pgid", "started", "source")} if info else None


def _validate_process_identity(value) -> dict:
    if not isinstance(value, dict):
        raise CoordinationError("INVALID_ARGUMENT", "Process identity must be an object")
    validate_fields(value, {"pid", "pgid", "started", "source"}, {"pid", "pgid", "started", "source"})
    integer(value["pid"], "pid", 1, 2**31 - 1)
    integer(value["pgid"], "pgid", 1, 2**31 - 1)
    patterns = {"darwin-libproc": r"[1-9][0-9]*\.[0-9]{6}",
                "linux-proc": r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}:[0-9]+"}
    if value["source"] not in patterns or not isinstance(value["started"], str) or not re.fullmatch(patterns[value["source"]], value["started"]):
        raise CoordinationError("INVALID_ARGUMENT", "Process start identity is invalid")
    return dict(value)


def process_status(identity) -> str:
    try:
        identity = _validate_process_identity(identity)
    except CoordinationError:
        return "unknown"
    current = process_identity(identity["pid"])
    if current is not None:
        if current["source"] != identity["source"]:
            return "unknown"
        return "alive" if current["started"] == identity["started"] else "gone"
    try:
        os.kill(identity["pid"], 0)
    except ProcessLookupError:
        return "gone"
    except OSError:
        pass
    return "unknown"


def group_status(identity) -> str:
    if process_status(identity) == "alive":
        return "alive"
    try:
        identity = _validate_process_identity(identity)
        # Signal zero asks the kernel about the group without launching a
        # privileged process-list helper. A reused group remains conservatively
        # alive; this observation never establishes permission to signal it.
        os.killpg(identity["pgid"], 0)
    except ProcessLookupError:
        return "gone"
    except (CoordinationError, OSError):
        return "unknown"
    return "alive"


def native_context(harness: str | None, environ=None, payload=None, *, store=None, executable_paths=None) -> dict:
    from .config import native_environment
    env = native_environment(environ)
    payload = dict(payload or {})
    session_names = {"claude": "CLAUDE_SESSION_ID", "codex": "CODEX_THREAD_ID", "grok": "GROK_SESSION_ID", "cursor": "CURSOR_SESSION_ID"}
    if harness is None:
        if env.get("AGENTCOORD_HARNESS"):
            harness = env["AGENTCOORD_HARNESS"]
        else:
            candidates = [name for name, key in session_names.items() if env.get(key)]
            if len(candidates) == 1:
                harness = candidates[0]
            elif len(candidates) > 1:
                raise CoordinationError("UNBOUND_ACTOR", "Multiple native session contexts require an explicit harness")
    if harness is not None and harness not in HARNESSES:
        raise CoordinationError("INVALID_ARGUMENT", "Unsupported native harness")
    native = payload.get("native_session_id") or payload.get("session_id") or payload.get("sessionId") or payload.get("conversation_id")
    native = native or env.get(session_names.get(harness, ""))
    child = payload.get("child_id") or payload.get("agent_id") or payload.get("agentId") or env.get("AGENTCOORD_CHILD_ID", "")
    run = payload.get("native_run_id") or env.get("AGENTCOORD_NATIVE_RUN_ID")
    native_names = {"claude": {"claude", "claude-code"}, "codex": {"codex", "codex-aarch64-apple-darwin", "codex-x86_64-apple-darwin"},
                    "grok": {"grok", "grok-build"}, "cursor": {"cursor-agent"}}
    names = native_names[harness] if harness else set().union(*native_names.values())
    configured = {}
    for key, value in dict(executable_paths or {}).items():
        if key not in HARNESSES:
            raise CoordinationError("INVALID_ARGUMENT", "Unsupported native executable configuration")
        configured[key] = Path(bounded_text(value, "native executable", 4096)).expanduser().resolve()
    pid, seen, proof = os.getppid(), set(), None
    for _ in range(12):
        if pid <= 1 or pid in seen:
            break
        seen.add(pid)
        info = _process_info(pid)
        if info is None:
            break
        image = Path(info["executable"]).resolve() if info.get("executable") else None
        matching = [name for name, executable in configured.items() if image == executable and (harness is None or name == harness)]
        if image and harness in {None, "grok"} and re.fullmatch(r"grok-[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9._]+)?-(?:macos|linux)-(?:aarch64|x86_64)", image.name):
            matching.append("grok")
        # Cursor's versioned native distribution names its image 'agent'. That
        # basename alone cannot identify a harness; require the kernel image's
        # versioned cursor-agent path or explicit executable configuration.
        if image and image.name == "agent" and "cursor-agent" in image.parts:
            position = image.parts.index("cursor-agent")
            suffix = image.parts[position + 1:]
            if len(suffix) == 3 and suffix[0] == "versions" and re.fullmatch(r"[0-9][A-Za-z0-9._-]{0,127}", suffix[1]) and harness in {None, "cursor"}:
                matching.append("cursor")
        if matching or (Path(info["name"]).name in names and info["name"] != "agent"):
            if harness is None:
                if len(set(matching)) > 1:
                    raise CoordinationError("UNBOUND_ACTOR", "Native executable maps to multiple harnesses")
                harness = matching[0] if matching else next(name for name, candidates in native_names.items() if Path(info["name"]).name in candidates)
            proof = {key: info[key] for key in ("pid", "pgid", "started", "source")}
            break
        pid = info["ppid"]
    if harness is None:
        raise CoordinationError("UNBOUND_ACTOR", "No supported native harness evidence is available")
    result = {"harness": harness, "child_id": child, "source": "native_environment" if native else "verified_process"}
    if native:
        result["native_session_id"] = native
    if run:
        result["native_run_id"] = run
    if proof:
        result["process_identity"] = proof
    if not native and proof is None:
        raise CoordinationError("UNBOUND_ACTOR", "Native startup context is missing; register lifecycle bootstrap or verified startup configuration")
    if not native and store is not None:
        with store.read() as tx:
            rows = tx.connection.execute("SELECT native_session_id,child_id FROM actors WHERE harness=? AND process_identity_json=?",
                (harness, canonical_json(proof))).fetchall()
        if len(rows) != 1:
            raise CoordinationError("UNBOUND_ACTOR", "Native process has no exact registered session")
        result.update(native_session_id=rows[0]["native_session_id"], child_id=rows[0]["child_id"])
    return result


def _get(service, context, arguments, tx):
    validate_fields(arguments, set())
    actor = actor_for_context(tx, context)
    return {"workspace_id": context.workspace_id, "actor": _actor_view(actor), "identity_mode": context.identity_mode,
            "separately_addressable_children": context.identity_mode == "native_child"}


def _status(service, context, arguments, tx):
    validate_fields(arguments, {"all", "limit", "after"})
    show_all = arguments.get("all", False)
    if type(show_all) is not bool:
        raise CoordinationError("INVALID_ARGUMENT", "all must be boolean")
    limit = integer(arguments.get("limit", 20), "limit", 1, 100)
    after = identifier(arguments["after"], "after") if arguments.get("after") is not None else ""
    rows = tx.connection.execute("""SELECT a.id,a.label,a.harness,a.native_session_id,a.child_id,
        a.current_task_generation,a.current_execution_generation,a.reported_state,a.archived,
        s.task,p.observed_state,p.observed_us,p.confidence
        FROM actors a JOIN assignments s ON s.generation=a.current_task_generation
        LEFT JOIN presence p ON p.actor_id=a.id
        WHERE ((? AND a.archived=0) OR (NOT ? AND a.id=?)) AND a.id>?
        ORDER BY a.id LIMIT ?""", (int(show_all), int(show_all), context.actor_id, after, limit + 1)).fetchall()
    actors = []
    for row in rows[:limit]:
        actor = dict(row)
        actor["archived"] = bool(actor["archived"])
        actor["observed_state"] = actor["observed_state"] or "unknown"
        actor["confidence"] = actor["confidence"] or "unknown"
        actor["observed_age_us"] = None if actor["observed_us"] is None else max(0, tx.now_us - actor["observed_us"])
        actors.append(actor)
    return {"actors": actors, "more": len(rows) > limit,
            "after": actors[-1]["id"] if len(rows) > limit else None,
            "identity_mode": context.identity_mode}


def _checkpoint(service, context, arguments, tx):
    validate_fields(arguments, {"state", "note", "resume_enabled"}, {"state", "note"})
    actor = actor_for_context(tx, context)
    state = arguments["state"]
    if state not in {"working", "idle", "waiting", "blocked", "paused", "completed"}:
        raise CoordinationError("INVALID_ARGUMENT", "Invalid checkpoint state")
    note = bounded_text(arguments["note"], "note", 65536)
    consent = arguments.get("resume_enabled", actor["resume_enabled"])
    if type(consent) is not bool:
        raise CoordinationError("INVALID_ARGUMENT", "resume_enabled must be boolean")
    if state == "completed" and tx.connection.execute("SELECT 1 FROM operations WHERE actor_id=? AND state IN ('running','uncertain') LIMIT 1", (actor["id"],)).fetchone():
        raise CoordinationError("RECONCILIATION_REQUIRED", "Cannot complete unresolved own external operations")
    tx.connection.execute("UPDATE actors SET reported_state=?,resume_enabled=?,resume_inhibited=CASE WHEN ? THEN 0 ELSE resume_inhibited END,checkpoint_json=?,version=version+1 WHERE id=?",
        (state, int(consent), int(consent), canonical_json({"note": note}), actor["id"]))
    tx.event("identity", "checkpoint", actor["id"], actor["id"], {"state": state})
    return {"actor": _actor_view(_actor(tx, actor["id"])), "changed": True}


def _complete(service, context, arguments, tx):
    validate_fields(arguments, {"note"}, {"note"})
    return _checkpoint(service, context, {"state": "completed", "note": arguments["note"]}, tx)


def _event(service, context, arguments, tx):
    validate_fields(arguments, {"state", "event", "execution_generation", "native_run_id", "note"}, {"state", "event"})
    event = bounded_text(arguments["event"], "event", 128)
    generation = arguments.get("execution_generation")
    run = arguments.get("native_run_id")
    proof = None
    if event in {"start", "child_start"} and context.transport == "hook":
        binding = tx.connection.execute("SELECT native_evidence_json FROM bindings WHERE id=?", (context.connection_id,)).fetchone()
        evidence = json.loads(binding[0]) if binding and binding[0] else {}
        proof = evidence.get("process_identity")
        if run is None and proof is not None:
            proof = _validate_process_identity(proof)
            run = "native-process:" + hashlib.sha256(canonical_json(proof).encode()).hexdigest()
    if run is not None:
        run = bounded_text(run, "native_run_id", 1024)
        if event in {"start", "child_start"}:
            if context.transport != "hook":
                raise CoordinationError("NOT_AUTHORIZED", "Native execution starts require originating lifecycle hook evidence")
            started = start_execution(tx, context, native_run_id=run, process_proof=proof)
            if started.get("reason") == "ended_run":
                return {"applied": False, "reason": "ended_run", "execution_generation": started["execution_generation"]}
            generation = started["execution_generation"]
        else:
            row = tx.connection.execute("SELECT generation FROM executions WHERE actor_id=? AND native_run_id=?", (context.actor_id, run)).fetchone()
            originating = row[0] if row else None
            if generation is not None and generation != originating:
                raise CoordinationError("INVALID_ARGUMENT", "Originating run and execution generation disagree")
            generation = originating
    return apply_lifecycle_event(tx, context, state=arguments["state"], event=bounded_text(arguments["event"], "event", 128),
        execution_generation=generation, note=bounded_text(arguments.get("note", ""), "note", 65536, allow_empty=True))


def _delegate(service, context, arguments, tx):
    validate_fields(arguments, {"child_id"}, {"child_id"})
    parent, child = actor_for_context(tx, context), _actor(tx, identifier(arguments["child_id"], "child_id"))
    if parent["child_id"] or not child["child_id"] or child["harness"] != parent["harness"] or child["native_session_id"] != parent["native_session_id"]:
        raise CoordinationError("NOT_AUTHORIZED", "Only this native parent may authorize its registered child route")
    prior = tx.connection.execute("SELECT parent_id FROM parent_routes WHERE child_id=?", (child["id"],)).fetchone()
    if prior and prior[0] != parent["id"]:
        raise CoordinationError("NOT_AUTHORIZED", "An existing child cannot be reparented")
    tx.connection.execute("INSERT INTO parent_routes VALUES (?,?,?,?,?,?) ON CONFLICT(child_id) DO UPDATE SET child_task_generation=excluded.child_task_generation,parent_task_generation=excluded.parent_task_generation,source=excluded.source",
        (child["id"], parent["id"], child["current_task_generation"], parent["current_task_generation"], "parent_declaration", tx.now_us))
    return {"child_id": child["id"], "parent_id": parent["id"], "routing_authorized": True}


def operations() -> tuple[Operation, ...]:
    return (Operation("identity.get", _get), Operation("identity.status", _status), Operation("identity.checkpoint", _checkpoint, True, True),
            Operation("identity.complete", _complete, True, True), Operation("identity.event", _event, True, True),
            Operation("identity.delegate", _delegate, True, True))
