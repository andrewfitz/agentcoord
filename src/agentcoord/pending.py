"""One bounded pending-action query for agent and operator adapters."""

from __future__ import annotations

import base64
import hashlib
import json

from . import commits, core, decisions, jobs, messages, readiness
from .core import CoordinationError, Operation, canonical_json, integer, validate_fields

SCHEMA = (
    """CREATE TABLE action_presentations (
        actor_id TEXT NOT NULL REFERENCES actors(id),kind TEXT NOT NULL,
        record_id TEXT NOT NULL,version INTEGER NOT NULL,presented_us INTEGER NOT NULL,
        PRIMARY KEY(actor_id,kind,record_id,version))""",
)


def _cursor(query, after, highwater):
    return (
        base64.urlsafe_b64encode(
            canonical_json({"query": query, "after": after, "highwater": highwater}).encode()
        )
        .decode()
        .rstrip("=")
    )


def _position(cursor, query):
    try:
        if not isinstance(cursor, str) or len(cursor) > 1024:
            raise ValueError("cursor")
        value = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if set(value) != {"query", "after", "highwater"} or value["query"] != query:
            raise ValueError("query")
        for key in ("after", "highwater"):
            integer(value[key], key, 0, 2**63 - 1)
        if value["after"] > value["highwater"]:
            raise ValueError("order")
        return value["after"], value["highwater"]
    except (ValueError, TypeError, UnicodeError, CoordinationError) as error:
        raise CoordinationError(
            "INVALID_ARGUMENT", "Pending cursor does not match this view"
        ) from error


def _preview(action):
    fields = {
        "kind",
        "id",
        "version",
        "sequence",
        "state",
        "routing_state",
        "deadline_us",
        "next_action",
        "message_ids",
        "message_ids_more",
        "notice_count",
        "actor_id",
        "sender_id",
        "recipient_id",
        "producer_id",
        "consumer_id",
        "artifact",
        "status",
    }
    result = {key: value for key, value in action.items() if key in fields}
    summary = str(action.get("summary", ""))
    encoded = summary.encode("utf-8")
    result["summary"] = encoded[:720].decode("utf-8", errors="ignore")
    if len(encoded) > 720:
        result["summary_excerpt"] = True
    return result


def select(
    tx,
    context,
    *,
    cursor=None,
    limit=3,
    new_only=False,
    byte_budget=8192,
    filters=None,
    present=False,
):
    integer(limit, "limit", 1, 100 if context.operator else 20)
    integer(byte_budget, "byte_budget", 1024, 260_000)
    if type(new_only) is not bool or type(present) is not bool:
        raise CoordinationError("INVALID_ARGUMENT", "Presentation flags must be booleans")
    if present and context.operator:
        raise CoordinationError(
            "NOT_AUTHORIZED", "Operator inspection cannot present another actor's work"
        )
    filters = dict(filters or {})
    validate_fields(filters, {"actor_id", "task", "path"})
    query = hashlib.sha256(
        canonical_json(
            {
                "workspace": context.workspace_id,
                "actor": context.actor_id,
                "generation": context.task_generation,
                "operator": context.operator,
                "new_only": new_only,
                "filters": filters,
            }
        ).encode()
    ).hexdigest()
    highwater = tx.connection.execute("SELECT COALESCE(MAX(sequence),0) FROM events").fetchone()[0]
    after = 0
    if cursor is not None:
        after, highwater = _position(cursor, query)
    fetch_limit = min(100, limit + 1)
    options = {"after": after, "limit": fetch_limit, "new_only": new_only, "filters": filters}
    domains = (decisions, readiness, commits, jobs, core)
    batches = [domain.select_actions(tx, context, **options) for domain in domains]
    logical = [action for batch in batches for action in batch]
    plain = messages.select_unhandled(tx, context, exclude_domain_notifications=True, **options)
    actions = sorted(
        (action for action in (*logical, *plain) if action["sequence"] <= highwater),
        key=lambda action: (action["sequence"], action["kind"], action["id"]),
    )
    counts = messages.count_pending(
        tx, context, exclude_domain_notifications=True, new_only=new_only, filters=filters
    )
    for domain in domains:
        counts.update(domain.count_pending(tx, context, new_only=new_only, filters=filters))
    result = {
        "items": [],
        "counts": counts,
        "total": sum(counts.values()),
        "cursor": None,
        "highwater": highwater,
        "more": False,
    }
    selected = []
    for action in actions[:limit]:
        candidate = _preview(action)
        result["items"].append(candidate)
        result["cursor"] = _cursor(query, action["sequence"], highwater)
        result["more"] = True
        # Leave room for the complete success envelope and actual request ID.
        if len(canonical_json(result).encode()) + 512 > byte_budget:
            result["items"].pop()
            if not selected:
                raise CoordinationError(
                    "INVALID_ARGUMENT",
                    "Action cannot fit the requested budget; retrieve its domain record",
                )
            break
        selected.append(action)
    # A saturated domain page may conceal another action. A final empty page is
    # preferable to losing records at the deliberate operator page maximum.
    result["more"] = len(actions) > len(selected) or any(
        len(batch) == fetch_limit for batch in (*batches, plain)
    )
    result["cursor"] = (
        _cursor(query, selected[-1]["sequence"], highwater) if selected and result["more"] else None
    )
    if present:
        for action in selected:
            if action["kind"] != "message":
                tx.connection.execute(
                    """INSERT OR IGNORE INTO action_presentations
                    (actor_id,kind,record_id,version,presented_us) VALUES (?,?,?,?,?)""",
                    (context.actor_id, action["kind"], action["id"], action["version"], tx.now_us),
                )
        messages.mark_presented(
            tx,
            context,
            [message_id for action in selected for message_id in action.get("message_ids", ())],
        )
        decisions.mark_presented(
            tx, context, [action for action in selected if action["kind"].startswith("decision")]
        )
        readiness.mark_presented(
            tx, context, [action for action in selected if action["kind"].startswith("readiness")]
        )
    return result


def _sync(service, context, arguments, tx):
    validate_fields(arguments, {"cursor", "limit", "new_only"})
    service.require_actor(tx, context)
    return select(tx, context, **arguments, byte_budget=service.config.action_bytes, present=True)


def operations():
    return (Operation("message.sync", _sync, True, True, True),)
