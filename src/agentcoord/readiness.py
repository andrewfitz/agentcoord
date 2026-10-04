"""Hash-bound artifact publication and explicit dependency acceptance."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
from dataclasses import replace
from pathlib import Path

from . import messages
from .core import (
    Context,
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
    """CREATE TABLE receipts (
        id TEXT PRIMARY KEY, producer_id TEXT NOT NULL REFERENCES actors(id),
        producer_task_generation TEXT NOT NULL REFERENCES assignments(generation), artifact TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('ready','verified','withdrawn')), hashes_json TEXT NOT NULL,
        evidence_json TEXT NOT NULL, version INTEGER NOT NULL CHECK(version>0),
        sequence INTEGER NOT NULL UNIQUE REFERENCES events(sequence), created_us INTEGER NOT NULL,
        UNIQUE(producer_id,artifact,version))""",
    "CREATE INDEX receipts_latest ON receipts(producer_id,artifact,version DESC)",
    """CREATE TABLE receipt_paths (
        receipt_id TEXT NOT NULL REFERENCES receipts(id), path TEXT NOT NULL,
        PRIMARY KEY(receipt_id,path))""",
    "CREATE INDEX receipt_paths_scope ON receipt_paths(path,receipt_id)",
    """CREATE TABLE subscriptions (
        id TEXT PRIMARY KEY, consumer_id TEXT NOT NULL REFERENCES actors(id),
        consumer_task_generation TEXT NOT NULL REFERENCES assignments(generation),
        producer_id TEXT NOT NULL REFERENCES actors(id), producer_task_generation TEXT NOT NULL REFERENCES assignments(generation),
        artifact TEXT NOT NULL, paths_json TEXT NOT NULL, next_action TEXT NOT NULL, task TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('waiting','notified','cancelled')), version INTEGER NOT NULL CHECK(version>0))""",
    "CREATE INDEX subscriptions_producer ON subscriptions(producer_id,artifact,state)",
    "CREATE INDEX subscriptions_consumer ON subscriptions(consumer_id,state)",
    """CREATE TABLE dependency_updates (
        id TEXT PRIMARY KEY, subscription_id TEXT NOT NULL REFERENCES subscriptions(id),
        receipt_id TEXT NOT NULL REFERENCES receipts(id), presented_us INTEGER, accepted_us INTEGER,
        sequence INTEGER NOT NULL UNIQUE REFERENCES events(sequence), UNIQUE(subscription_id,receipt_id))""",
    "CREATE INDEX updates_subscription ON dependency_updates(subscription_id,sequence DESC)",
    """CREATE TABLE readiness_notifications (
        receipt_id TEXT NOT NULL REFERENCES receipts(id), message_id TEXT NOT NULL REFERENCES messages(id),
        PRIMARY KEY(receipt_id,message_id))""",
    "CREATE INDEX readiness_notice_message ON readiness_notifications(message_id)",
    """CREATE TRIGGER receipts_immutable BEFORE UPDATE ON receipts BEGIN
        SELECT RAISE(ABORT,'immutable readiness receipt'); END""",
)


def _fail(message, code="INVALID_ARGUMENT"):
    raise CoordinationError(code, message)


def _latest(tx, producer, artifact):
    row = tx.connection.execute("SELECT * FROM receipts WHERE producer_id=? AND artifact=? ORDER BY version DESC LIMIT 1", (producer, artifact)).fetchone()
    return dict(row) if row else None


def _receipt(tx, receipt_id):
    identifier(receipt_id, "receipt")
    row = tx.connection.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
    if row is None:
        _fail("Receipt does not exist", "NOT_FOUND")
    result = dict(row)
    result["hashes"] = json.loads(result.pop("hashes_json"))
    result["evidence"] = json.loads(result.pop("evidence_json"))
    return result


def public_receipt(receipt, *, path_after=None, limit=20):
    """Expose immutable scope in bounded pages without losing the full stored contract."""
    limit = integer(limit, "limit", 0, 100)
    if path_after is not None:
        path_after = normalize_paths([path_after])[0]
    hashes = {}
    remaining = False
    for path in sorted(receipt["hashes"]):
        if path_after is not None and path <= path_after:
            continue
        if len(hashes) >= limit or len(canonical_json({**hashes, path: receipt["hashes"][path]}).encode()) > 65536:
            remaining = True
            break
        hashes[path] = receipt["hashes"][path]
    return {**receipt, "hashes": hashes, "path_count": len(receipt["hashes"]),
            "paths_after": next(reversed(hashes)) if remaining and hashes else None,
            "paths_more": remaining}


def _update(tx, update_id, consumer_id):
    identifier(update_id, "update")
    row = tx.connection.execute("""SELECT u.*,s.consumer_id,s.consumer_task_generation,s.producer_id,
        s.producer_task_generation,s.artifact,s.paths_json,s.next_action,s.task,s.state AS subscription_state,
        s.version AS subscription_version FROM dependency_updates u JOIN subscriptions s ON s.id=u.subscription_id
        WHERE u.id=? AND s.consumer_id=?""", (update_id, consumer_id)).fetchone()
    if row is None:
        _fail("Dependency update is unavailable to this actor", "NOT_FOUND")
    return dict(row)


def _append_update(tx, subscription, receipt):
    previous = tx.connection.execute("SELECT id FROM dependency_updates WHERE subscription_id=? AND receipt_id=?", (subscription["id"], receipt["id"])).fetchone()
    if previous:
        return previous[0]
    update_id = str(uuid.uuid4())
    seq = tx.event("readiness", "dependency_update", update_id, receipt["producer_id"], {"subscription_id": subscription["id"]})
    tx.connection.execute("INSERT INTO dependency_updates VALUES (?,?,?,NULL,NULL,?)", (update_id, subscription["id"], receipt["id"], seq))
    tx.connection.execute("UPDATE subscriptions SET state='notified',version=version+1 WHERE id=?", (subscription["id"],))
    return update_id


def subscribe(service, context, args, tx):
    validate_fields(args, {"producer", "artifact", "paths", "next_action", "task"}, {"producer", "artifact", "paths", "next_action"})
    consumer = service.require_actor(tx, context)
    service.require_generation(tx, consumer["id"], context.task_generation)
    producer = messages.actor(tx, args["producer"])
    if producer["archived"]:
        _fail("Cannot subscribe to an archived producer", "NOT_AUTHORIZED")
    artifact = bounded_text(args["artifact"], "artifact", 256)
    scoped = normalize_paths(args["paths"])
    if not scoped:
        _fail("A subscription requires complete exact paths")
    action = bounded_text(args["next_action"], "next_action", 2000)
    assigned = tx.connection.execute("SELECT task FROM assignments WHERE generation=?", (consumer["current_task_generation"],)).fetchone()
    task = bounded_text(args.get("task", assigned[0]), "task", 256)
    previous = tx.connection.execute("SELECT * FROM subscriptions WHERE consumer_id=? AND producer_id=? AND artifact=? AND state!='cancelled'", (consumer["id"], producer["id"], artifact)).fetchone()
    if previous:
        if (previous["consumer_task_generation"], previous["producer_task_generation"], previous["paths_json"], previous["next_action"], previous["task"]) != (
            consumer["current_task_generation"], producer["current_task_generation"], canonical_json(scoped), action, task):
            _fail("Cancel the existing subscription before replacing its contract", "STALE_VERSION")
        return dict(previous)
    subscription_id = str(uuid.uuid4())
    tx.connection.execute("INSERT INTO subscriptions VALUES (?,?,?,?,?,?,?,?,?,'waiting',1)", (
        subscription_id, consumer["id"], consumer["current_task_generation"], producer["id"], producer["current_task_generation"], artifact, canonical_json(scoped), action, task))
    subscription = dict(tx.connection.execute("SELECT * FROM subscriptions WHERE id=?", (subscription_id,)).fetchone())
    latest = _latest(tx, producer["id"], artifact)
    if latest and latest["producer_task_generation"] == producer["current_task_generation"]:
        _append_update(tx, subscription, latest)
    return dict(tx.connection.execute("SELECT * FROM subscriptions WHERE id=?", (subscription_id,)).fetchone())


def cancel(service, context, args, tx):
    validate_fields(args, {"id"}, {"id"})
    service.require_actor(tx, context)
    identifier(args["id"], "subscription")
    row = tx.connection.execute("SELECT * FROM subscriptions WHERE id=? AND consumer_id=?", (args["id"], context.actor_id)).fetchone()
    if row is None:
        _fail("Only the subscription consumer can cancel it", "NOT_AUTHORIZED")
    tx.connection.execute("UPDATE subscriptions SET state='cancelled',version=version+1 WHERE id=? AND state!='cancelled'", (args["id"],))
    return {"id": args["id"], "state": "cancelled"}


def prepare_hashes(root, paths, *, allow_missing=False):
    """Read exact regular files outside a database transaction, without symlink traversal."""
    scoped = normalize_paths(paths)
    result = {}
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    root_fd = os.open(Path(root), directory_flags)
    try:
        for path in scoped:
            parent = os.dup(root_fd)
            try:
                pieces = path.split("/")
                for piece in pieces[:-1]:
                    child = os.open(piece, directory_flags, dir_fd=parent)
                    os.close(parent)
                    parent = child
                try:
                    descriptor = os.open(pieces[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                except FileNotFoundError:
                    if allow_missing:
                        result[path] = None
                        continue
                    _fail(f"Required readiness input is missing: {path}", "NOT_FOUND")
                with os.fdopen(descriptor, "rb") as stream:
                    before = os.fstat(stream.fileno())
                    if not stat.S_ISREG(before.st_mode):
                        _fail("Readiness paths must be regular files")
                    digest = hashlib.sha256()
                    for block in iter(lambda: stream.read(262144), b""):
                        digest.update(block)
                    after = os.fstat(stream.fileno())
                    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                        _fail("Readiness input changed while hashing", "STALE_VERSION")
                    result[path] = digest.hexdigest()
            except FileNotFoundError:
                if allow_missing:
                    result[path] = None
                else:
                    _fail(f"Required readiness input is missing: {path}", "NOT_FOUND")
            except OSError as error:
                _fail(f"Cannot read readiness input: {path} (errno {error.errno})", "OPERATION_FAILED")
            finally:
                os.close(parent)
    finally:
        os.close(root_fd)
    return result


def _enqueue(service, context, kind, args, tx, snapshot):
    return service.enqueue(tx, context, kind, {"request": args, "snapshot": snapshot}, key=str(uuid.uuid4()))


def publish(service, context, args, tx):
    validate_fields(args, {"artifact", "paths", "evidence", "status"}, {"artifact", "paths", "evidence"})
    producer = service.require_actor(tx, context, active=True)
    service.require_generation(tx, producer["id"], context.task_generation)
    artifact = bounded_text(args["artifact"], "artifact", 256)
    scoped = normalize_paths(args["paths"])
    if not scoped:
        _fail("Readiness needs complete exact inputs")
    status = args.get("status", "ready")
    if status not in {"ready", "verified"}:
        _fail("Choose ready or verified")
    evidence = canonical_json(args["evidence"])
    if len(evidence.encode()) > 8192 or status == "verified" and args["evidence"] in (None, "", {}, []):
        _fail("Verified readiness requires actual evidence references")
    latest = _latest(tx, producer["id"], artifact)
    return _enqueue(service, context, "readiness.publish", {**args, "paths": scoped}, tx,
                    {"latest_id": latest["id"] if latest else None, "latest_version": latest["version"] if latest else 0})


def handoff(service, context, args, tx):
    validate_fields(args, {"artifact", "paths", "evidence", "status", "recipients", "subject", "body", "thread"},
                    {"artifact", "paths", "evidence", "recipients", "subject", "body"})
    recipients = args["recipients"]
    if not isinstance(recipients, list) or not 1 <= len(recipients) <= 16:
        _fail("Handoff requires 1..16 explicit recipients")
    recipient_generations = {}
    for recipient in recipients:
        row = messages.actor(tx, recipient)
        if row["archived"]:
            _fail("Handoff recipient is archived", "NOT_AUTHORIZED")
        recipient_generations[recipient] = row["current_task_generation"]
    if len(set(recipients)) != len(recipients):
        _fail("Handoff recipient IDs must be unique")
    bounded_text(args.get("thread", ""), "thread", 256, allow_empty=True)
    bounded_text(args["subject"], "subject", 1024)
    bounded_text(args["body"], "body", 65536)
    # Share publication validation without enqueuing a second command.
    publication = {key: value for key, value in args.items() if key in {"artifact", "paths", "evidence", "status"}}
    producer = service.require_actor(tx, context, active=True)
    service.require_generation(tx, producer["id"], context.task_generation)
    artifact = bounded_text(publication["artifact"], "artifact", 256)
    scoped = normalize_paths(publication["paths"])
    status = publication.get("status", "ready")
    if not scoped or status not in {"ready", "verified"}:
        _fail("Handoff requires complete inputs and valid readiness status")
    evidence = canonical_json(publication["evidence"])
    if len(evidence.encode()) > 8192 or status == "verified" and publication["evidence"] in (None, "", {}, []):
        _fail("Verified readiness requires actual evidence references")
    latest = _latest(tx, producer["id"], artifact)
    return _enqueue(service, context, "readiness.handoff", {**args, "paths": scoped}, tx,
                    {"latest_id": latest["id"] if latest else None, "latest_version": latest["version"] if latest else 0,
                     "recipient_generations": recipient_generations})


def publish_prepared(service, context, request, snapshot, hashes, tx):
    producer = service.require_generation(tx, context.actor_id, context.task_generation)
    if producer["archived"] or producer["reported_state"] in {"paused", "completed"}:
        _fail("Producer is no longer active", "STALE_GENERATION")
    latest = _latest(tx, producer["id"], request["artifact"])
    if (latest["id"] if latest else None, latest["version"] if latest else 0) != (snapshot["latest_id"], snapshot["latest_version"]):
        _fail("Readiness changed during hash preparation", "STALE_VERSION")
    expected_paths = normalize_paths(request["paths"])
    if set(hashes) != set(expected_paths) or any(value is None for value in hashes.values()):
        _fail("Prepared hashes do not cover the publication contract")
    status, evidence = request.get("status", "ready"), canonical_json(request["evidence"])
    encoded_hashes = canonical_json(hashes)
    if latest and (latest["producer_task_generation"], latest["status"], latest["hashes_json"], latest["evidence_json"]) == (context.task_generation, status, encoded_hashes, evidence):
        return _receipt(tx, latest["id"])
    receipt_id = str(uuid.uuid4())
    seq = tx.event("readiness", "published", receipt_id, producer["id"], {"artifact": request["artifact"]})
    tx.connection.execute("INSERT INTO receipts VALUES (?,?,?,?,?,?,?,?,?,?)", (
        receipt_id, producer["id"], context.task_generation, request["artifact"], status, encoded_hashes,
        evidence, snapshot["latest_version"] + 1, seq, tx.now_us))
    tx.connection.executemany("INSERT INTO receipt_paths VALUES (?,?)", [(receipt_id, path) for path in expected_paths])
    receipt = _receipt(tx, receipt_id)
    for row in tx.connection.execute("SELECT * FROM subscriptions WHERE producer_id=? AND producer_task_generation=? AND artifact=? AND state!='cancelled'",
                                    (producer["id"], context.task_generation, request["artifact"])).fetchall():
        _append_update(tx, dict(row), receipt)
    return receipt


def withdraw(service, context, args, tx):
    validate_fields(args, {"artifact", "reason"}, {"artifact", "reason"})
    producer = service.require_actor(tx, context)
    service.require_generation(tx, producer["id"], context.task_generation)
    artifact = bounded_text(args["artifact"], "artifact", 256)
    reason = bounded_text(args["reason"], "reason", 2000)
    latest = _latest(tx, producer["id"], artifact)
    if not latest or latest["producer_task_generation"] != context.task_generation:
        _fail("No current-generation receipt exists to withdraw", "NOT_FOUND")
    if latest["status"] == "withdrawn" and json.loads(latest["evidence_json"]) == reason:
        return public_receipt(_receipt(tx, latest["id"]))
    receipt_id = str(uuid.uuid4())
    seq = tx.event("readiness", "withdrawn", receipt_id, producer["id"], {"artifact": artifact})
    tx.connection.execute("INSERT INTO receipts VALUES (?,?,?,?, 'withdrawn',?,?,?,?,?)", (
        receipt_id, producer["id"], context.task_generation, artifact, latest["hashes_json"], canonical_json(reason), latest["version"] + 1, seq, tx.now_us))
    tx.connection.execute("INSERT INTO receipt_paths SELECT ?,path FROM receipt_paths WHERE receipt_id=?", (receipt_id, latest["id"]))
    receipt = _receipt(tx, receipt_id)
    for row in tx.connection.execute("SELECT * FROM subscriptions WHERE producer_id=? AND producer_task_generation=? AND artifact=? AND state!='cancelled'", (producer["id"], context.task_generation, artifact)).fetchall():
        _append_update(tx, dict(row), receipt)
    # Known handoff recipients receive one withdrawal notice, without a broadcast.
    targets = [r[0] for r in tx.connection.execute("""SELECT DISTINCT r.actor_id FROM readiness_notifications n
        JOIN recipients r ON r.message_id=n.message_id JOIN receipts p ON p.id=n.receipt_id
        JOIN actors a ON a.id=r.actor_id WHERE p.producer_id=? AND p.artifact=?
        AND p.producer_task_generation=? AND a.archived=0
        AND r.recipient_task_generation=a.current_task_generation""", (producer["id"], artifact, context.task_generation))]
    if targets:
        for start in range(0, len(targets), 16):
            message = messages.append_message(tx, context, recipient_ids=targets[start:start+16], kind="readiness_withdrawn",
                subject=artifact, body=reason, thread="", paths=list(receipt["hashes"]), declared_context={"receipt_id": receipt_id})
            tx.connection.execute("INSERT INTO readiness_notifications VALUES (?,?)", (receipt_id, message["id"]))
    return public_receipt(receipt)


def accept(service, context, args, tx):
    validate_fields(args, {"id"}, {"id"})
    service.require_actor(tx, context)
    update = _update(tx, args["id"], context.actor_id)
    receipt = _receipt(tx, update["receipt_id"])
    _validate_update(service, tx, context, update, receipt)
    return _enqueue(service, context, "readiness.accept", args, tx,
        {"receipt_id": receipt["id"], "receipt_version": receipt["version"], "subscription_version": update["subscription_version"], "paths": json.loads(update["paths_json"])})


def _validate_update(service, tx, context, update, receipt):
    if update["subscription_state"] == "cancelled":
        _fail("Subscription was cancelled", "STALE_VERSION")
    service.require_generation(tx, update["consumer_id"], update["consumer_task_generation"])
    if context.task_generation != update["consumer_task_generation"]:
        _fail("Consumer task changed", "STALE_GENERATION")
    service.require_generation(tx, update["producer_id"], update["producer_task_generation"])
    latest = _latest(tx, update["producer_id"], update["artifact"])
    if latest["id"] != receipt["id"] or receipt["producer_task_generation"] != update["producer_task_generation"]:
        _fail("Receipt was superseded or its generation changed", "STALE_VERSION")
    if receipt["status"] == "withdrawn":
        _fail("Readiness was withdrawn", "STALE_VERSION")
    if set(json.loads(update["paths_json"])) - set(receipt["hashes"]):
        _fail("Latest receipt no longer covers complete subscription scope", "STALE_VERSION")


def accept_prepared(service, context, update_id, snapshot, hashes, tx):
    update = _update(tx, update_id, context.actor_id)
    receipt = _receipt(tx, update["receipt_id"])
    _validate_update(service, tx, context, update, receipt)
    if (receipt["id"], receipt["version"], update["subscription_version"]) != (snapshot["receipt_id"], snapshot["receipt_version"], snapshot["subscription_version"]):
        _fail("Dependency changed during hash preparation", "STALE_VERSION")
    required = set(json.loads(update["paths_json"]))
    if set(hashes) != required or any(hashes[path] != receipt["hashes"][path] for path in required):
        _fail("Dependency input hashes changed", "STALE_VERSION")
    tx.connection.execute("UPDATE dependency_updates SET accepted_us=COALESCE(accepted_us,?) WHERE id=?", (tx.now_us, update_id))
    return {"id": update_id, "receipt_id": receipt["id"], "accepted": True, "receipt_version": receipt["version"]}


def inspect(service, context, args, tx):
    validate_fields(args, {"producer", "artifact", "paths", "limit", "after"})
    if "producer" in args:
        identifier(args["producer"], "producer")
    if "artifact" in args:
        bounded_text(args["artifact"], "artifact", 256)
    scoped = normalize_paths(args.get("paths", ()))
    limit = integer(args.get("limit", 20), "limit", 1, 100)
    sql = """SELECT r.id,r.version,r.sequence FROM receipts r WHERE NOT EXISTS (SELECT 1 FROM receipts newer
        WHERE newer.producer_id=r.producer_id AND newer.artifact=r.artifact AND newer.version>r.version)"""
    params = []
    for field, column in (("producer", "producer_id"), ("artifact", "artifact")):
        if field in args:
            sql += f" AND r.{column}=?"
            params.append(args[field])
    if "after" in args:
        sql += " AND r.sequence<?"
        params.append(integer(args["after"], "after", 1, 2**63-1))
    if scoped:
        sql += """ AND EXISTS (SELECT 1 FROM receipt_paths p JOIN json_each(?) q ON p.path=q.value
            OR instr(p.path,q.value||'/')=1 OR instr(q.value,p.path||'/')=1 WHERE p.receipt_id=r.id)"""
        params.append(canonical_json(scoped))
    rows = tx.connection.execute(sql + " ORDER BY r.sequence DESC LIMIT ?", (*params, limit + 1)).fetchall()
    return _enqueue(service, context, "readiness.inspect", args, tx,
                    {"receipts": [dict(row) for row in rows[:limit]], "more": len(rows) > limit})


def updates(service, context, args, tx):
    validate_fields(args, {"after", "limit", "history"})
    if type(args.get("history", False)) is not bool:
        _fail("history must be a boolean")
    limit = integer(args.get("limit", 20), "limit", 1, 100)
    after = integer(args.get("after", 0), "after", 0, 2**63-1)
    sql = """SELECT u.* FROM dependency_updates u JOIN subscriptions s ON s.id=u.subscription_id
        WHERE s.consumer_id=? AND u.sequence>?"""
    if not args.get("history", False):
        sql += " AND s.state!='cancelled' AND u.accepted_us IS NULL AND NOT EXISTS (SELECT 1 FROM dependency_updates n WHERE n.subscription_id=u.subscription_id AND n.sequence>u.sequence)"
    rows = tx.connection.execute(sql + " ORDER BY u.sequence LIMIT ?", (context.actor_id, after, limit + 1)).fetchall()
    selected = []
    for row in rows[:limit]:
        update = _update(tx, row["id"], context.actor_id)
        receipt = _receipt(tx, update["receipt_id"])
        status = receipt["status"]
        if update["subscription_state"] == "cancelled":
            status = "cancelled"
        elif set(json.loads(update["paths_json"])) - set(receipt["hashes"]):
            status = "incomplete_scope" if status != "withdrawn" else status
        candidate = {**dict(row), "status": status, "artifact": update["artifact"], "receipt": public_receipt(receipt, limit=0)}
        if selected and len(canonical_json([*selected, candidate]).encode()) > 100_000:
            break
        selected.append(candidate)
    return {"updates": selected, "after": selected[-1]["sequence"] if len(rows) > len(selected) else None}


def _pending_query(context, *, new_only=False, filters=None):
    filters = messages.pending_filters(filters)
    values = {"actor": context.actor_id}
    owner = "" if context.operator else " AND s.consumer_id=:actor"
    notice_owner = "" if context.operator else " AND p.actor_id=:actor"
    common = ""
    notice_filters = ""
    if "actor_id" in filters:
        owner += " AND s.consumer_id=:filter_actor"
        notice_owner += " AND p.actor_id=:filter_actor"
        values["filter_actor"] = filters["actor_id"]
    if "task" in filters:
        common += " AND s.task=:task"
        notice_filters += " AND EXISTS (SELECT 1 FROM assignments a WHERE a.generation=r.producer_task_generation AND a.task=:task)"
        values["task"] = filters["task"]
    if "path" in filters:
        common += """ AND EXISTS (SELECT 1 FROM json_each(s.paths_json) q WHERE q.value=:path OR
            :path='.' OR instr(q.value,:path||'/')=1 OR instr(:path,q.value||'/')=1)"""
        notice_filters += """ AND EXISTS (SELECT 1 FROM receipt_paths q WHERE q.receipt_id=r.id AND
            (q.path=:path OR :path='.' OR instr(q.path,:path||'/')=1 OR instr(:path,q.path||'/')=1))"""
        values["path"] = filters["path"]
    pending = """s.state!='cancelled' AND u.accepted_us IS NULL AND NOT EXISTS
        (SELECT 1 FROM dependency_updates newer WHERE newer.subscription_id=u.subscription_id AND newer.sequence>u.sequence)"""
    updates_sql = f"""SELECT 'readiness' AS action_kind,u.id,u.receipt_id,u.sequence,r.version,
        s.artifact,r.status,s.paths_json,r.hashes_json,s.consumer_id AS actor_id,r.producer_id,
        s.consumer_task_generation,r.producer_task_generation,
        a.current_task_generation AS current_producer_generation,
        ca.current_task_generation AS current_consumer_generation,
        r.version=(SELECT MAX(latest.version) FROM receipts latest WHERE latest.producer_id=r.producer_id
                   AND latest.artifact=r.artifact) AS is_latest FROM dependency_updates u
        JOIN subscriptions s ON s.id=u.subscription_id JOIN receipts r ON r.id=u.receipt_id
        JOIN actors a ON a.id=r.producer_id JOIN actors ca ON ca.id=s.consumer_id WHERE {pending}{owner}{common}"""
    notices_sql = f"""SELECT 'readiness_notice' AS action_kind,r.id,r.id AS receipt_id,
        MAX(m.sequence) AS sequence,MAX(m.sequence) AS version,r.artifact,r.status,
        '[]' AS paths_json,r.hashes_json,MIN(p.actor_id) AS actor_id,r.producer_id,
        NULL AS consumer_task_generation,r.producer_task_generation,
        a.current_task_generation AS current_producer_generation,NULL AS current_consumer_generation,
        r.version=(SELECT MAX(latest.version) FROM receipts latest WHERE latest.producer_id=r.producer_id
                   AND latest.artifact=r.artifact) AS is_latest FROM readiness_notifications n JOIN recipients p ON p.message_id=n.message_id
        JOIN messages m ON m.id=n.message_id JOIN receipts r ON r.id=n.receipt_id
        JOIN actors a ON a.id=r.producer_id WHERE p.handled_us IS NULL{notice_owner}{notice_filters} AND NOT EXISTS
        (SELECT 1 FROM dependency_updates u JOIN subscriptions s ON s.id=u.subscription_id
         WHERE u.receipt_id=r.id AND s.consumer_id=p.actor_id AND {pending}) GROUP BY r.id"""
    sql = updates_sql + " UNION ALL " + notices_sql
    if new_only and not context.operator:
        sql = "SELECT * FROM (" + sql + """) candidate WHERE NOT EXISTS
            (SELECT 1 FROM action_presentations p WHERE p.actor_id=:actor AND p.kind=candidate.action_kind
             AND p.record_id=candidate.id AND p.version=candidate.version)"""
    return sql, values


def _notice_ids(tx, context, row):
    if row["action_kind"] == "readiness":
        owner = replace(context, operator=False, actor_id=row["actor_id"])
        return messages.notice_ids(tx, owner, "readiness_notifications", "receipt_id", row["receipt_id"])
    sql = """SELECT DISTINCT n.message_id,m.sequence FROM readiness_notifications n JOIN recipients p
        ON p.message_id=n.message_id JOIN messages m ON m.id=n.message_id WHERE n.receipt_id=?
        AND p.handled_us IS NULL AND NOT EXISTS (SELECT 1 FROM dependency_updates u JOIN subscriptions s
          ON s.id=u.subscription_id WHERE u.receipt_id=n.receipt_id AND s.consumer_id=p.actor_id
          AND s.state!='cancelled' AND u.accepted_us IS NULL AND NOT EXISTS
          (SELECT 1 FROM dependency_updates newer WHERE newer.subscription_id=u.subscription_id AND newer.sequence>u.sequence))"""
    args = [row["receipt_id"]]
    if not context.operator:
        sql += " AND p.actor_id=?"
        args.append(context.actor_id)
    count = tx.connection.execute("SELECT COUNT(*) FROM (" + sql + ")", args).fetchone()[0]
    ids = [r[0] for r in tx.connection.execute(sql + " ORDER BY m.sequence LIMIT 20", args)]
    return {"message_ids": ids, "notice_count": count, "message_ids_more": count > len(ids)}


def select_actions(tx, context, *, after=None, limit=20, new_only=False, filters=None):
    sql, values = _pending_query(context, new_only=new_only, filters=filters)
    values.update(after=0 if after is None else integer(after, "after", 0, 2**63-1),
                  limit=integer(limit, "limit", 1, 100))
    rows = tx.connection.execute("SELECT * FROM (" + sql + ") WHERE sequence>:after ORDER BY sequence LIMIT :limit", values).fetchall()
    result = []
    for row in rows:
        status = row["status"]
        if row["current_producer_generation"] != row["producer_task_generation"] or (
                row["consumer_task_generation"] is not None and
                row["current_consumer_generation"] != row["consumer_task_generation"]):
            status = "obsolete_generation"
        elif not row["is_latest"]:
            status = "superseded"
        elif set(json.loads(row["paths_json"])) - set(json.loads(row["hashes_json"])) and status != "withdrawn":
            status = "incomplete_scope"
        result.append({"kind": row["action_kind"], "id": row["id"], "version": row["version"],
            "sequence": row["sequence"], "summary": row["artifact"], "status": status,
            "actor_id": row["actor_id"], "consumer_id": row["actor_id"], "producer_id": row["producer_id"],
            "receipt_id": row["receipt_id"], **_notice_ids(tx, context, row),
            "next_action": "readiness.accept" if row["action_kind"] == "readiness" else "readiness.inspect"})
    return result


def count_pending(tx, context, *, new_only=False, filters=None):
    sql, values = _pending_query(context, new_only=new_only, filters=filters)
    rows = tx.connection.execute("SELECT action_kind,COUNT(*) FROM (" + sql + ") GROUP BY action_kind", values)
    counts = {row[0]: row[1] for row in rows}
    return {"readiness": counts.get("readiness", 0), "readiness_notices": counts.get("readiness_notice", 0)}


def mark_presented(tx, context, actions):
    if context.operator:
        _fail("Operator reads cannot present another actor's updates", "NOT_AUTHORIZED")
    for action in actions:
        if action["kind"] == "readiness":
            tx.connection.execute("UPDATE dependency_updates SET presented_us=COALESCE(presented_us,?) WHERE id=? AND subscription_id IN (SELECT id FROM subscriptions WHERE consumer_id=?)", (tx.now_us, action["id"], context.actor_id))


def run_operation(service, operation):
    """Prepare hashes without a writer lock, then commit result and domain effect together."""
    args = json.loads(operation["arguments_json"]) if "arguments_json" in operation else operation["arguments"]
    request, snapshot = args["request"], args["snapshot"]
    with service.store.read() as tx:
        actor = messages.actor(tx, operation["actor_id"])
        context = Context(service.workspace.id, actor["id"], "readiness-worker", "worker", operation["task_generation"],
                          operation.get("execution_generation"), identity_mode="native_child" if actor["child_id"] else "native_owner")
        service.require_generation(tx, actor["id"], operation["task_generation"])
        captured = []
        if operation["kind"] == "readiness.inspect":
            for item in snapshot["receipts"]:
                captured.append(_receipt(tx, item["id"]))
    if operation["kind"] == "readiness.inspect":
        prepared = [(receipt, prepare_hashes(service.workspace.root, list(receipt["hashes"]), allow_missing=True)) for receipt in captured if receipt["status"] != "withdrawn"]
    else:
        prepared = prepare_hashes(service.workspace.root, snapshot["paths"] if operation["kind"] == "readiness.accept" else request["paths"], allow_missing=operation["kind"] == "readiness.accept")
    with service.store.write() as tx:
        service.require_effect(tx, operation)
        if operation["kind"] in {"readiness.publish", "readiness.handoff"}:
            for recipient, generation in snapshot.get("recipient_generations", {}).items():
                service.require_generation(tx, recipient, generation)
            result = public_receipt(publish_prepared(service, context, request, snapshot, prepared, tx))
            if operation["kind"] == "readiness.handoff":
                notice = messages.append_message(tx, context, recipient_ids=request["recipients"], kind="handoff", subject=request["subject"], body=request["body"], thread=request.get("thread", ""), paths=request["paths"], declared_context={"receipt_id": result["id"]})
                tx.connection.execute("INSERT INTO readiness_notifications VALUES (?,?)", (result["id"], notice["id"]))
                result = {"receipt": result, "message": notice}
        elif operation["kind"] == "readiness.accept":
            result = accept_prepared(service, context, request["id"], snapshot, prepared, tx)
        elif operation["kind"] == "readiness.inspect":
            hashes_by_id = {r["id"]: hashes for r, hashes in prepared}
            rows = []
            for receipt in captured:
                latest = _latest(tx, receipt["producer_id"], receipt["artifact"])
                if latest["id"] != receipt["id"] or latest["version"] != receipt["version"]:
                    _fail("Readiness changed during inspection", "STALE_VERSION")
                producer = messages.actor(tx, receipt["producer_id"])
                status = receipt["status"]
                changed = []
                if producer["current_task_generation"] != receipt["producer_task_generation"]:
                    status = "obsolete_generation"
                elif status != "withdrawn":
                    changed = [p for p, digest in receipt["hashes"].items() if hashes_by_id[receipt["id"]][p] != digest]
                    if changed:
                        status = "changed"
                changed_preview = []
                for path in changed[:20]:
                    if len(canonical_json([*changed_preview, path]).encode()) > 32768:
                        break
                    changed_preview.append(path)
                candidate = {**public_receipt(receipt, limit=0), "current_status": status,
                             "changed_paths": changed_preview, "changed_path_count": len(changed),
                             "changed_paths_more": len(changed)>len(changed_preview)}
                if rows and len(canonical_json([*rows, candidate]).encode()) > 100_000:
                    break
                rows.append(candidate)
            more = snapshot["more"] or len(rows)<len(captured)
            result = {"receipts": rows, "more": more,
                      "after": rows[-1]["sequence"] if more and rows else None}
        else:
            _fail("Unknown readiness worker operation")
        service.finish_operation(tx, operation["id"], "succeeded", result=result, claim_token=operation["claim_token"])
        return result


def slow_handlers():
    return {name: run_operation for name in ("readiness.publish", "readiness.handoff", "readiness.accept", "readiness.inspect")}


def operations():
    return tuple(Operation(name, handler, True, True, True) for name, handler in (
        ("readiness.subscribe", subscribe), ("readiness.cancel", cancel), ("readiness.publish", publish),
        ("readiness.withdraw", withdraw), ("readiness.accept", accept), ("readiness.handoff", handoff),
        ("readiness.inspect", inspect))) + (Operation("readiness.updates", updates, False, False, True),)
