"""Immutable directed conversations and independent recipient receipts."""
from __future__ import annotations

import base64
import hashlib
import json
import uuid

from .core import (
    CoordinationError,
    Operation,
    bounded_text,
    canonical_json,
    identifier,
    normalize_paths,
    validate_fields,
)
from .core import integer as _integer

SCHEMA = (
    """CREATE TABLE messages (
        id TEXT PRIMARY KEY, sender_id TEXT NOT NULL REFERENCES actors(id),
        sender_task_generation TEXT NOT NULL REFERENCES assignments(generation),
        thread TEXT NOT NULL, kind TEXT NOT NULL, subject TEXT NOT NULL,
        body_utf8 BLOB NOT NULL, body_sha256 TEXT NOT NULL,
        body_bytes INTEGER NOT NULL CHECK(body_bytes>=0), context_json TEXT NOT NULL,
        sequence INTEGER NOT NULL UNIQUE REFERENCES events(sequence), created_us INTEGER NOT NULL,
        CHECK(length(body_utf8)=body_bytes))""",
    """CREATE TABLE recipients (
        message_id TEXT NOT NULL REFERENCES messages(id), actor_id TEXT NOT NULL REFERENCES actors(id),
        recipient_task_generation TEXT NOT NULL REFERENCES assignments(generation),
        role TEXT NOT NULL CHECK(role IN ('to','cc','bcc')), presented_us INTEGER,
        handled_us INTEGER, requested_ack_us INTEGER, acknowledged_us INTEGER,
        PRIMARY KEY(message_id,actor_id))""",
    "CREATE INDEX recipients_pending ON recipients(actor_id,handled_us,message_id)",
    "CREATE INDEX messages_thread_sequence ON messages(thread,sequence)",
    "CREATE INDEX messages_sender_sequence ON messages(sender_id,sequence)",
    """CREATE TABLE message_paths (
        message_id TEXT NOT NULL REFERENCES messages(id), path TEXT NOT NULL,
        PRIMARY KEY(message_id,path))""",
    "CREATE INDEX message_paths_scope ON message_paths(path,message_id)",
    """CREATE TABLE attachments (
        id TEXT PRIMARY KEY, message_id TEXT NOT NULL REFERENCES messages(id),
        reference_json TEXT NOT NULL, content_sha256 TEXT NOT NULL,
        bytes INTEGER NOT NULL CHECK(bytes>=0))""",
    "CREATE INDEX attachments_message_page ON attachments(message_id,id)",
    """CREATE TRIGGER messages_immutable BEFORE UPDATE ON messages BEGIN
        SELECT RAISE(ABORT,'immutable message'); END""",
    """CREATE TRIGGER recipients_identity_immutable BEFORE UPDATE OF
        message_id,actor_id,recipient_task_generation,role,requested_ack_us ON recipients BEGIN
        SELECT RAISE(ABORT,'immutable recipient identity'); END""",
)


def _error(message, code="INVALID_ARGUMENT"):
    raise CoordinationError(code, message)


fields = validate_fields
canonical = canonical_json


def text(value, label, maximum=65536, *, empty=False):
    return bounded_text(value, label, maximum, allow_empty=empty)


def integer(value, label, minimum=0, maximum=2**63-1):
    return _integer(value, label, minimum, maximum)


def paths(values, *, root=False):
    return normalize_paths(values, allow_root=root)


def actor(tx, actor_id, *, active=False):
    identifier(actor_id, "actor")
    # Routing needs authority fields, never retained import/checkpoint payloads.
    row = tx.connection.execute("""SELECT id,child_id,current_task_generation,
        archived,reported_state FROM actors WHERE id=?""", (actor_id,)).fetchone()
    if row is None:
        _error("Actor does not exist", "NOT_FOUND")
    if active and (row["archived"] or row["reported_state"] in {"paused", "completed"}):
        _error("Actor is inactive", "NOT_AUTHORIZED")
    return dict(row)


def append_message(tx, context, *, recipient_ids, kind, subject, body, thread,
                   paths=(), declared_context=None, requested_ack=False):
    sender = actor(tx, context.actor_id)
    if context.task_generation != sender["current_task_generation"]:
        _error("Sender task changed", "STALE_GENERATION")
    if not isinstance(recipient_ids, (list, tuple)) or not 1 <= len(recipient_ids) <= 16:
        _error("A message requires 1..16 explicit recipients")
    recipients = [actor(tx, item) for item in recipient_ids]
    if len({item['id'] for item in recipients}) != len(recipients):
        _error("Recipient IDs must be unique")
    if any(item["archived"] for item in recipients):
        _error("An archived actor cannot receive a new message", "NOT_AUTHORIZED")
    if type(requested_ack) is not bool:
        _error("requested_ack must be a boolean")
    kind, subject, body = text(kind, "kind", 40), text(subject, "subject", 1024), text(body, "body")
    thread = text(thread, "thread", 256, empty=True)
    scoped = normalize_paths(paths, allow_root=True)
    metadata = {} if declared_context is None else declared_context
    if not isinstance(metadata, dict):
        _error("Message context must be a JSON object")
    encoded_context = canonical(metadata)
    if len(encoded_context.encode()) > 8192:
        _error("Message context exceeds 8192 bytes")
    raw = body.encode("utf-8")
    message_id = str(uuid.uuid4())
    sequence = tx.event("messages", kind, message_id, sender["id"], {})
    tx.connection.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
        message_id, sender["id"], sender["current_task_generation"], thread, kind, subject,
        raw, hashlib.sha256(raw).hexdigest(), len(raw), encoded_context, sequence, tx.now_us,
    ))
    tx.connection.executemany("INSERT INTO recipients VALUES (?,?,?,'to',NULL,NULL,?,NULL)", [
        (message_id, item["id"], item["current_task_generation"], tx.now_us if requested_ack else None)
        for item in recipients
    ])
    tx.connection.executemany("INSERT INTO message_paths VALUES (?,?)", [(message_id, item) for item in scoped])
    return {"id": message_id, "sequence": sequence, "body_bytes": len(raw), "recipient_count": len(recipients)}


def _visible(tx, context, message_id):
    identifier(message_id, "message")
    columns = "m.id,m.sender_id,m.subject,m.kind,m.thread,m.sequence,m.body_bytes,m.context_json"
    if context.operator:
        row = tx.connection.execute(f"SELECT {columns} FROM messages m WHERE id=?", (message_id,)).fetchone()
    else:
        row = tx.connection.execute(f"""SELECT {columns} FROM messages m WHERE m.id=? AND
            (m.sender_id=? OR EXISTS (SELECT 1 FROM recipients r WHERE r.message_id=m.id AND r.actor_id=?))""",
            (message_id, context.actor_id, context.actor_id)).fetchone()
    if row is None:
        _error("Message is unavailable to this actor", "NOT_FOUND")
    return row


def _decode_chunk(raw, offset, limit, total_bytes):
    if raw and raw[0] & 0xC0 == 0x80:
        _error("Offset must identify a UTF-8 character boundary")
    end = min(len(raw), limit)
    while end > 0 and end < len(raw) and raw[end] & 0xC0 == 0x80:
        end -= 1
    if end == 0 and raw:
        _error("Chunk limit is smaller than the next UTF-8 character")
    return raw[:end].decode("utf-8"), offset + end if offset + end < total_bytes else None


def attachment_index(tx, context, message_id, *, after=None, limit=20):
    _visible(tx, context, message_id)
    return _attachment_page(tx, message_id, after=after, limit=limit)


def _attachment_page(tx, message_id, *, after=None, limit=20):
    """Metadata only, after the caller has established message visibility."""
    limit = integer(limit, "limit", 1, 100)
    if after is not None:
        identifier(after, "attachment continuation")
    rows = tx.connection.execute("""SELECT id,content_sha256,bytes FROM attachments
        WHERE message_id=? AND (? IS NULL OR id>?) ORDER BY id LIMIT ?""",
        (message_id, after, after, limit + 1)).fetchall()
    selected = rows[:limit]
    return {"message_id": message_id, "items": [dict(row) for row in selected],
            "next_after": selected[-1]["id"] if len(rows) > limit else None}


def read_attachment(tx, context, attachment_id, *, offset=0, limit=32768):
    identifier(attachment_id, "attachment")
    offset, limit = integer(offset, "offset"), integer(limit, "limit", 1, 32768)
    row = tx.connection.execute("""SELECT id,message_id,content_sha256,bytes,
        length(CAST(reference_json AS BLOB)) AS reference_bytes
        FROM attachments WHERE id=?""", (attachment_id,)).fetchone()
    if row is None:
        _error("Attachment is unavailable to this actor", "NOT_FOUND")
    _visible(tx, context, row["message_id"])
    if offset > row["reference_bytes"]:
        _error("Offset exceeds the reference size")
    raw = bytes(tx.connection.execute("""SELECT substr(CAST(reference_json AS BLOB),?,?)
        FROM attachments WHERE id=?""", (offset + 1, limit + 4, attachment_id)).fetchone()[0])
    reference, next_offset = _decode_chunk(raw, offset, limit, row["reference_bytes"])
    return {**dict(row), "reference": reference, "offset": offset, "next_offset": next_offset}


def read_message(tx, context, message_id, *, offset=0, limit=32768):
    offset, limit = integer(offset, "offset"), integer(limit, "limit", 1, 65536)
    row = _visible(tx, context, message_id)
    if offset > row["body_bytes"]:
        _error("Offset exceeds the body size")
    raw = bytes(tx.connection.execute("SELECT substr(body_utf8,?,?) FROM messages WHERE id=?",
        (offset+1, limit+4, message_id)).fetchone()[0])
    body, next_offset = _decode_chunk(raw, offset, limit, row["body_bytes"])
    attachments = _attachment_page(tx, message_id)
    return {"id": row["id"], "sender_id": row["sender_id"], "subject": row["subject"],
            "kind": row["kind"], "thread": row["thread"], "sequence": row["sequence"],
            "body": body, "body_bytes": row["body_bytes"], "offset": offset,
            "next_offset": next_offset, "context": json.loads(row["context_json"]),
            "attachments": attachments["items"], "attachments_next_after": attachments["next_after"]}


def text_chunk(value, *, offset=0, limit=8192):
    raw = (value or "").encode("utf-8")
    offset, limit = integer(offset, "offset"), integer(limit, "limit", 1, 32768)
    if offset > len(raw) or (offset < len(raw) and raw[offset] & 0xC0 == 0x80):
        _error("Offset must identify a UTF-8 character boundary")
    end = min(len(raw), offset + limit)
    while end > offset and end < len(raw) and raw[end] & 0xC0 == 0x80:
        end -= 1
    if end == offset and offset < len(raw):
        _error("Chunk limit is smaller than the next UTF-8 character")
    return {"text": raw[offset:end].decode("utf-8"), "bytes": len(raw),
            "offset": offset, "next_offset": end if end < len(raw) else None}


def read_batch(tx, context, ids, *, index=0, body_limit=8192, byte_budget=32768):
    """Read selected messages once, without presentation or handling effects."""
    if not isinstance(ids, list) or not 1 <= len(ids) <= 16:
        _error("Batch requires 1..16 distinct message IDs")
    for message_id in ids:
        identifier(message_id, "message")
    if len(set(ids)) != len(ids):
        _error("Batch requires 1..16 distinct message IDs")
    index = integer(index, "index", 0, len(ids))
    body_limit = integer(body_limit, "body_limit", 4, 32768)
    byte_budget = integer(byte_budget, "byte_budget", 16384, 65536)
    result = {"items": [], "next_index": None}
    item_bytes = 0
    for position in range(index, len(ids)):
        limit = body_limit
        try:
            item = {"ok": True, **read_message(tx, context, ids[position], limit=limit)}
        except CoordinationError as error:
            item = {"ok": False, "id": ids[position], "error": {"code": error.code, "message": error.message}}
        next_index = position + 1 if position + 1 < len(ids) else None
        # Serialize each item, not every earlier body on every candidate.
        overhead = len(canonical({"items": [], "next_index": next_index}).encode())
        while True:
            encoded_bytes = len(canonical(item).encode())
            # Each existing item needs one comma before this one.
            if overhead + item_bytes + encoded_bytes + len(result["items"]) <= byte_budget:
                result["items"].append(item)
                result["next_index"] = next_index
                item_bytes += encoded_bytes
                break
            if result["items"]:
                result["next_index"] = position
                return result
            if limit == 4 or not item["ok"]:
                _error("Message metadata exceeds batch budget; use message or a larger byte_budget")
            limit = max(4, limit // 2)
            # Immutable content was fetched once. Shorten only the returned
            # prefix, retaining an exact UTF-8 continuation for the remainder.
            body, offset = _decode_chunk(item["body"].encode("utf-8"), 0, limit, item["body_bytes"])
            item = {**item, "body": body, "next_offset": offset}
    return result


def pending_filters(filters=None):
    filters = dict(filters or {})
    validate_fields(filters, {"actor_id", "task", "path"})
    if "actor_id" in filters:
        identifier(filters["actor_id"], "actor_id")
    if "task" in filters:
        text(filters["task"], "task", 256)
    if "path" in filters:
        filters["path"] = paths([filters["path"]], root=True)[0]
    return filters


def notice_ids(tx, context, table, column, record_id):
    # Table and column are domain-owned constants, never caller input.
    sql = f"""SELECT DISTINCT n.message_id,m.sequence FROM {table} n JOIN recipients p
        ON p.message_id=n.message_id JOIN messages m ON m.id=n.message_id
        WHERE n.{column}=? AND p.handled_us IS NULL"""
    args = [record_id]
    if not context.operator:
        sql += " AND p.actor_id=?"
        args.append(context.actor_id)
    count = tx.connection.execute(f"SELECT COUNT(*) FROM ({sql})", args).fetchone()[0]
    rows = tx.connection.execute(sql + " ORDER BY m.sequence LIMIT 20", args).fetchall()
    return {"message_ids": [row[0] for row in rows], "notice_count": count,
            "message_ids_more": count > len(rows)}


def _pending_query(tx, context, *, new_only=False, filters=None, exclude_domain_notifications=False,
                   count=False):
    filters = pending_filters(filters)
    columns = "COUNT(DISTINCT m.id)" if count else """m.id,m.kind,m.subject,m.sender_id,m.thread,m.body_bytes,m.sequence,m.created_us,
        MIN(r.actor_id) AS actor_id,COUNT(*) AS recipient_count,
        CASE WHEN SUM(r.presented_us IS NULL)>0 THEN NULL ELSE MIN(r.presented_us) END AS presented_us,
        substr(CAST(m.body_utf8 AS TEXT),1,360) AS summary"""
    sql = f"SELECT {columns} FROM recipients r JOIN messages m ON m.id=r.message_id WHERE r.handled_us IS NULL"
    args = []
    if not context.operator:
        sql += " AND r.actor_id=?"
        args.append(context.actor_id)
    if "actor_id" in filters:
        sql += " AND r.actor_id=?"
        args.append(filters["actor_id"])
    if new_only:
        sql += " AND r.presented_us IS NULL"
    if "task" in filters:
        sql += """ AND (m.thread=? OR EXISTS (SELECT 1 FROM assignments a WHERE
            a.generation IN (m.sender_task_generation,r.recipient_task_generation) AND a.task=?))"""
        args.extend([filters["task"], filters["task"]])
    if "path" in filters:
        sql += """ AND EXISTS (SELECT 1 FROM message_paths p WHERE p.message_id=m.id AND
            (p.path=? OR p.path='.' OR ?='.' OR instr(p.path,?||'/')=1 OR instr(?,p.path||'/')=1))"""
        args.extend([filters["path"]] * 4)
    if exclude_domain_notifications:
        sql += """ AND NOT EXISTS (SELECT 1 FROM decision_notifications n WHERE n.message_id=m.id)
            AND NOT EXISTS (SELECT 1 FROM readiness_notifications n WHERE n.message_id=m.id)"""
        if tx.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='job_notifications'").fetchone():
            sql += """ AND NOT EXISTS (SELECT 1 FROM job_notifications n JOIN jobs j ON j.id=n.job_id
                WHERE n.message_id=m.id AND j.state IN ('failed','uncertain'))"""
    return sql, args


def select_unhandled(tx, context, *, after=None, limit=20, exclude_ids=(), new_only=False,
                     exclude_domain_notifications=False, filters=None):
    limit = integer(limit, "limit", 1, 100)
    after = 0 if after is None else integer(after, "after")
    sql, args = _pending_query(tx, context, new_only=new_only, filters=filters,
                              exclude_domain_notifications=exclude_domain_notifications)
    sql += " AND m.sequence>?"
    args.append(after)
    if exclude_ids:
        sql += " AND m.id NOT IN (SELECT value FROM json_each(?))"
        args.append(canonical(list(exclude_ids)))
    sql += " GROUP BY m.id ORDER BY m.sequence LIMIT ?"
    rows = tx.connection.execute(sql, (*args, limit)).fetchall()
    return [{**dict(row), "kind": "message", "message_kind": row["kind"], "version": 1,
             "summary_excerpt": len(row["summary"].encode("utf-8")) < row["body_bytes"],
             "message_ids": [row["id"]], "next_action": "message.get"} for row in rows]


def count_pending(tx, context, *, exclude_domain_notifications=False, new_only=False, filters=None):
    sql, args = _pending_query(tx, context, new_only=new_only, filters=filters,
                              exclude_domain_notifications=exclude_domain_notifications, count=True)
    count = tx.connection.execute(sql, args).fetchone()[0]
    return {"messages": count}


def mark_presented(tx, context, message_ids):
    if context.operator:
        _error("Operator reads cannot present another actor's messages", "NOT_AUTHORIZED")
    for message_id in set(message_ids):
        identifier(message_id, "message")
        tx.connection.execute("UPDATE recipients SET presented_us=COALESCE(presented_us,?) WHERE message_id=? AND actor_id=?",
                              (tx.now_us, message_id, context.actor_id))


def consume_message(tx, context, message_id, *, acknowledge=False):
    if context.operator:
        _error("Operator reads cannot handle another actor's messages", "NOT_AUTHORIZED")
    identifier(message_id, "message")
    row = tx.connection.execute("SELECT * FROM recipients WHERE message_id=? AND actor_id=?", (message_id, context.actor_id)).fetchone()
    if row is None:
        _error("Only an explicit recipient can handle this message", "NOT_AUTHORIZED")
    if acknowledge and row["requested_ack_us"] is None:
        _error("Receipt acknowledgment was not requested", "INVALID_ARGUMENT")
    column = "acknowledged_us" if acknowledge else "handled_us"
    tx.connection.execute(f"UPDATE recipients SET {column}=COALESCE({column},?) WHERE message_id=? AND actor_id=?",
                          (tx.now_us, message_id, context.actor_id))
    return {"id": message_id, "acknowledged" if acknowledge else "handled": True}


def _send(service, context, args, tx):
    fields(args, {"recipients", "kind", "subject", "body", "thread", "paths", "context", "requested_ack", "wake"},
           {"recipients", "kind", "subject", "body"})
    service.require_actor(tx, context)
    if type(args.get("wake", True)) is not bool:
        _error("wake must be boolean")
    result = append_message(tx, context, recipient_ids=args["recipients"], kind=args["kind"], subject=args["subject"],
                          body=args["body"], thread=args.get("thread", ""), paths=args.get("paths", ()),
                          declared_context=args.get("context"), requested_ack=args.get("requested_ack", False))
    if args.get("wake", True):
        from .wake import enqueue
        result["wake"] = enqueue(service, context, tx, result, args["recipients"])
    return result


def _get(service, context, args, tx):
    fields(args, {"id", "offset", "limit"}, {"id"})
    return read_message(tx, context, args["id"], offset=args.get("offset", 0), limit=args.get("limit", 32768))


def _get_batch(service, context, args, tx):
    fields(args, {"ids", "index", "body_limit", "byte_budget"}, {"ids"})
    return read_batch(tx, context, args["ids"], index=args.get("index", 0),
                      body_limit=args.get("body_limit", 8192), byte_budget=args.get("byte_budget", 32768))


def _attachments(service, context, args, tx):
    fields(args, {"id", "after", "limit"}, {"id"})
    return attachment_index(tx, context, args["id"], after=args.get("after"), limit=args.get("limit", 20))


def _attachment(service, context, args, tx):
    fields(args, {"id", "offset", "limit"}, {"id"})
    return read_attachment(tx, context, args["id"], offset=args.get("offset", 0), limit=args.get("limit", 32768))


def _consume(service, context, args, tx):
    fields(args, {"id"}, {"id"})
    service.require_actor(tx, context)
    return consume_message(tx, context, args["id"])


def _ack(service, context, args, tx):
    fields(args, {"id"}, {"id"})
    service.require_actor(tx, context)
    return consume_message(tx, context, args["id"], acknowledge=True)


def _consume_batch(service, context, args, tx):
    fields(args, {"ids"}, {"ids"})
    service.require_actor(tx, context)
    values = args["ids"]
    if not isinstance(values, list) or not 1 <= len(values) <= 100:
        _error("Batch requires 1..100 message IDs")
    results = []
    for value in values:
        try:
            results.append({"ok": True, **consume_message(tx, context, value)})
        except CoordinationError as error:
            results.append({"ok": False, "id": value, "error": {"code": error.code, "message": error.message}})
    return {"results": results}


def history(tx, context, args):
    fields(args, {"cursor", "limit", "actor", "task", "thread", "paths", "since_us", "until_us", "text"})
    limit = integer(args.get("limit", 20), "limit", 1, 100)
    filters = {key: value for key, value in args.items() if key not in {"cursor", "limit"}}
    high = tx.connection.execute("SELECT COALESCE(MAX(sequence),0) FROM messages").fetchone()[0]
    after = 0
    signature = hashlib.sha256(canonical({"workspace": context.workspace_id, "actor": context.actor_id,
        "operator": context.operator, "filters": filters}).encode()).hexdigest()
    if args.get("cursor"):
        try:
            cursor = json.loads(base64.urlsafe_b64decode(text(args["cursor"], "cursor", 2048)))
            if set(cursor) != {"after", "high", "filter"} or cursor["filter"] != signature:
                _error("Cursor belongs to different history filters")
            after, high = integer(cursor["after"], "cursor after"), integer(cursor["high"], "cursor high")
            if after > high:
                _error("Cursor position exceeds its high-water mark")
        except (ValueError, TypeError, KeyError):
            _error("Invalid history cursor")
    sql = "SELECT m.id,m.kind,m.subject,m.thread,m.sender_id,m.sequence,m.created_us,m.body_bytes FROM messages m WHERE m.sequence>? AND m.sequence<=?"
    params = [after, high]
    if not context.operator:
        sql += " AND (m.sender_id=? OR EXISTS (SELECT 1 FROM recipients r WHERE r.message_id=m.id AND r.actor_id=?))"
        params.extend([context.actor_id, context.actor_id])
    if "actor" in filters:
        wanted = identifier(filters["actor"], "actor filter")
        sql += " AND (m.sender_id=? OR EXISTS (SELECT 1 FROM recipients r WHERE r.message_id=m.id AND r.actor_id=?))"
        params.extend([wanted, wanted])
    if "task" in filters:
        sql += " AND EXISTS (SELECT 1 FROM assignments a WHERE a.generation=m.sender_task_generation AND a.task=?)"
        params.append(text(filters["task"], "task", 256))
    if "thread" in filters:
        sql += " AND m.thread=?"
        params.append(text(filters["thread"], "thread", 256, empty=True))
    if "paths" in filters:
        scoped = paths(filters["paths"], root=True)
        sql += " AND EXISTS (SELECT 1 FROM message_paths p JOIN json_each(?) q ON p.path=q.value OR p.path='.' OR q.value='.' OR instr(p.path,q.value||'/')=1 OR instr(q.value,p.path||'/')=1 WHERE p.message_id=m.id)"
        params.append(canonical(scoped))
    for key, comparison in (("since_us", ">="), ("until_us", "<=")):
        if key in filters:
            sql += f" AND m.created_us{comparison}?"
            params.append(integer(filters[key], key))
    if "text" in filters:
        search = text(filters["text"], "search", 512).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        sql += " AND (m.subject LIKE ? ESCAPE '\\' OR CAST(m.body_utf8 AS TEXT) LIKE ? ESCAPE '\\')"
        params.extend([f"%{search}%", f"%{search}%"])
    rows = tx.connection.execute(sql + " ORDER BY m.sequence LIMIT ?", (*params, limit + 1)).fetchall()
    selected = [dict(row) for row in rows[:limit]]
    continuation = None
    if len(rows) > limit:
        continuation = base64.urlsafe_b64encode(canonical({"after": selected[-1]["sequence"], "high": high, "filter": signature}).encode()).decode()
    return {"messages": selected, "cursor": continuation, "high_water": high}


def _history(service, context, args, tx):
    return history(tx, context, args)


def _inbox(service, context, args, tx):
    fields(args, {"after", "limit", "new_only"})
    if type(args.get("new_only", False)) is not bool:
        _error("new_only must be a boolean")
    return {"messages": select_unhandled(tx, context, after=args.get("after"), limit=args.get("limit", 20), new_only=args.get("new_only", False)),
            "pending_counts": count_pending(tx, context, new_only=args.get("new_only", False))}


def operations():
    return (
        Operation("message.send", _send, True, True, True),
        Operation("message.get", _get, False, False, False),
        Operation("message.get_batch", _get_batch, False, False, False),
        Operation("message.attachments", _attachments, False, False, False),
        Operation("message.attachment", _attachment, False, False, False),
        Operation("message.consume", _consume, True, True, True),
        Operation("message.consume_batch", _consume_batch, True, True, True),
        Operation("message.ack", _ack, True, True, True),
        Operation("message.inbox", _inbox, False, False, True),
        Operation("message.history", _history, False, False, False),
        Operation("message.search", _history, False, False, False),
    )
