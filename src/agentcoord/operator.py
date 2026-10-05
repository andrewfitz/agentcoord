"""Read-only bounded operator projections over the canonical domain records."""
from __future__ import annotations

import base64
import hashlib
import json

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
from .work import CURRENT_ACTIVITY_IDS

PAGE_BYTES = 100_000


def _fingerprint(context, query):
    return hashlib.sha256(canonical_json({"workspace": context.workspace_id, **query}).encode()).hexdigest()


def _encode(value):
    return base64.urlsafe_b64encode(canonical_json(value).encode()).decode().rstrip("=")


def _decode(value, fingerprint):
    try:
        if not isinstance(value, str) or len(value) > 8192:
            raise ValueError("cursor")
        result = json.loads(base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True))
        if not isinstance(result, dict) or result.get("query") != fingerprint:
            raise ValueError("query")
        return result
    except (ValueError, TypeError, UnicodeError) as error:
        raise CoordinationError("INVALID_ARGUMENT", "Cursor does not match this operator query") from error


def _operator(context):
    if not context.operator:
        raise CoordinationError("NOT_AUTHORIZED", "This view requires an operator connection")


def _filters(arguments):
    values = arguments.get("filters", {})
    validate_fields(values, {"actor_id", "task", "path"})
    result = {}
    if "actor_id" in values:
        result["actor_id"] = identifier(values["actor_id"], "actor_id")
    if values.get("task"):
        result["task"] = bounded_text(values["task"], "task", 4096)
    if values.get("path"):
        result["path"] = normalize_paths([values["path"]], allow_root=True)[0]
    return result


def _scope(alias, table, foreign_key, expression, path):
    if path == ".":
        return "1", []
    # Segment comparisons preserve src/a vs src/another; %/_ are ordinary path bytes.
    condition = f"({alias}.path=? OR substr({alias}.path,1,length(?)+1)=?||'/' OR substr(?,1,length({alias}.path)+1)={alias}.path||'/' OR {alias}.path='.')"
    return f"EXISTS(SELECT 1 FROM {table} {alias} WHERE {alias}.{foreign_key}={expression} AND {condition})", [path] * 4


def _actor_conditions(filters, expression):
    clauses, values = [], []
    if "actor_id" in filters:
        clauses.append(f"{expression}=?")
        values.append(filters["actor_id"])
    if "task" in filters:
        clauses.append(f"EXISTS(SELECT 1 FROM actors fa JOIN assignments ft ON ft.generation=fa.current_task_generation WHERE fa.id={expression} AND ft.task=?)")
        values.append(filters["task"])
    if "path" in filters:
        condition, selected = _scope("fp", "activity_paths", "activity_id", "fc.activity_id", filters["path"])
        clauses.append(f"EXISTS(SELECT 1 FROM current_activity fc WHERE fc.actor_id={expression} AND {condition})")
        values.extend(selected)
    return clauses, values


def history(service, context, arguments, tx):
    _operator(context)
    validate_fields(arguments, {"cursor", "limit", "domain", "kind", "actor_id", "query", "sequence", "direction", "filters"})
    limit = integer(arguments.get("limit", 20), "limit", 1, 100)
    direction = arguments.get("direction", "backward")
    if direction not in {"forward", "backward"}:
        raise CoordinationError("INVALID_ARGUMENT", "History direction must be forward or backward")
    filters = _filters(arguments)
    for key in ("domain", "kind", "actor_id", "query"):
        if key in arguments:
            filters[key] = identifier(arguments[key], key) if key == "actor_id" else bounded_text(arguments[key], key, 2048)
    fingerprint = _fingerprint(context, {"type": "history", "direction": direction, "filters": filters})
    highwater = tx.connection.execute("SELECT COALESCE(MAX(sequence),0) FROM events").fetchone()[0]
    position = 0 if direction == "forward" else highwater + 1
    if arguments.get("cursor"):
        cursor = _decode(arguments["cursor"], fingerprint)
        if set(cursor) != {"query", "position", "highwater"}:
            raise CoordinationError("INVALID_ARGUMENT", "Invalid history cursor")
        position = integer(cursor["position"], "cursor position", 0, 2**63 - 1)
        highwater = integer(cursor["highwater"], "cursor highwater", 0, 2**63 - 1)
        if position > highwater + 1:
            raise CoordinationError("INVALID_ARGUMENT", "History cursor is outside its snapshot")
    clauses, values = ["e.sequence<=?"], [highwater]
    for key in ("domain", "kind", "actor_id"):
        if key in filters:
            clauses.append(f"e.{key}=?")
            values.append(filters[key])
    if "query" in filters:
        # Search complete authored bodies and notes, not only event previews.
        clauses.append("""(instr(e.metadata_json,?)>0 OR EXISTS(SELECT 1 FROM messages sm WHERE sm.id=e.record_id AND e.domain='messages' AND (instr(sm.subject,?)>0 OR instr(CAST(sm.body_utf8 AS TEXT),?)>0)) OR EXISTS(SELECT 1 FROM activities sa WHERE sa.id=e.record_id AND e.domain='work' AND instr(sa.note,?)>0)
            OR (e.domain='work' AND e.kind='intent' AND (instr(json_extract(e.metadata_json,'$.purpose'),?)>0
                OR EXISTS(SELECT 1 FROM json_each(e.metadata_json,'$.invariants') si WHERE instr(si.value,?)>0))))""")
        values.extend([filters["query"]] * 6)
    if "task" in filters:
        clauses.append("""(EXISTS(SELECT 1 FROM activities ha WHERE e.domain='work' AND ha.id=e.record_id AND ha.task=?)
            OR EXISTS(SELECT 1 FROM messages hm JOIN assignments ht ON ht.generation=hm.sender_task_generation WHERE e.domain='messages' AND hm.id=e.record_id AND ht.task=?)
            OR EXISTS(SELECT 1 FROM decisions hd WHERE e.domain='decisions' AND hd.id=e.record_id AND hd.task=?)
            OR EXISTS(SELECT 1 FROM receipts hr JOIN assignments ht ON ht.generation=hr.producer_task_generation WHERE e.domain='readiness' AND hr.id=e.record_id AND ht.task=?)
            OR EXISTS(SELECT 1 FROM jobs hj JOIN assignments ht ON ht.generation=hj.task_generation WHERE e.domain='jobs' AND hj.id=e.record_id AND ht.task=?)
            OR EXISTS(SELECT 1 FROM intents hi JOIN assignments ht ON ht.generation=hi.task_generation WHERE e.domain='work' AND e.kind='intent' AND hi.id=e.record_id AND ht.task=?))""")
        values.extend([filters["task"]] * 6)
    if "path" in filters:
        possibilities = []
        for domain, table, key in (("work", "activity_paths", "activity_id"), ("work", "intents", "id"), ("messages", "message_paths", "message_id"), ("decisions", "decision_paths", "decision_id"), ("readiness", "receipt_paths", "receipt_id")):
            clause, scoped = _scope("hp", table, key, "e.record_id", filters["path"])
            possibilities.append(f"(e.domain=? AND {clause})")
            values.append(domain)
            values.extend(scoped)
        clauses.append("(" + " OR ".join(possibilities) + ")")
    predicate = " AND ".join(clauses)
    total = tx.connection.execute(f"SELECT COUNT(*) FROM events e WHERE {predicate}", values).fetchone()[0]
    if "sequence" in arguments:
        selected = integer(arguments["sequence"], "sequence", 1, 2**63 - 1)
        predicate += " AND e.sequence=?"
        values.append(selected)
    else:
        predicate += " AND e.sequence" + (">?" if direction == "forward" else "<?")
        values.append(position)
    order = "ASC" if direction == "forward" else "DESC"
    selected_rows = tx.connection.execute(f"SELECT e.* FROM events e WHERE {predicate} ORDER BY e.sequence {order} LIMIT ?", (*values, limit + 1)).fetchall()
    if "sequence" in arguments and not selected_rows:
        raise CoordinationError("NOT_FOUND", "History record is unavailable in this query")
    items, size = [], 0
    budget = min(service.config.frame_bytes - 1024, 230_000 if "sequence" in arguments else PAGE_BYTES)
    for row in selected_rows[:limit]:
        item = dict(row)
        item["metadata"] = json.loads(item.pop("metadata_json"))
        if "sequence" in arguments:
            if item["domain"] == "work":
                if item["kind"] == "intent":
                    item["record"] = {"id": item["record_id"], "actor_id": item["actor_id"],
                                      **item["metadata"]}
                record = tx.connection.execute("SELECT id,actor_id,task,state,note,evidence_json,sequence,created_us FROM activities WHERE id=?", (item["record_id"],)).fetchone()
                if record is not None:
                    item["record"] = dict(record)
                    item["record"]["evidence"] = json.loads(item["record"].pop("evidence_json"))
                    item["record"]["paths"] = [entry[0] for entry in tx.connection.execute("SELECT path FROM activity_paths WHERE activity_id=? ORDER BY path", (record["id"],))]
            elif item["domain"] == "decisions":
                record = tx.connection.execute("SELECT id,subject,body,state,response,version,deadline_us FROM decisions WHERE id=?", (item["record_id"],)).fetchone()
                if record is not None:
                    item["record"] = dict(record)
        encoded = len(canonical_json(item).encode())
        if size + encoded > budget:
            if not items:
                raise CoordinationError("INVALID_ARGUMENT", "Event exceeds bounded detail budget; inspect its domain record")
            break
        items.append(item)
        size += encoded
    more = len(selected_rows) > len(items) and "sequence" not in arguments
    cursor = _encode({"query": fingerprint, "position": items[-1]["sequence"], "highwater": highwater}) if more and items else None
    return {"items": items, "cursor": cursor, "total": total, "highwater": highwater, "direction": direction}


def _page(tx, *, table, select, joins="", clauses=(), values=(), key="id", position=None, limit=20, fingerprint, section):
    clauses, values = list(clauses), list(values)
    predicate = " AND ".join(clauses) if clauses else "1"
    total = tx.connection.execute(f"SELECT COUNT(*) FROM {table} {joins} WHERE {predicate}", values).fetchone()[0]
    if position == "":
        return [], {"total": total, "position": ""}
    if position is not None:
        clauses.append(f"{key}>?")
        values.append(position)
    predicate = " AND ".join(clauses) if clauses else "1"
    selected = tx.connection.execute(f"SELECT {select} FROM {table} {joins} WHERE {predicate} ORDER BY {key} LIMIT ?", (*values, limit + 1)).fetchall()
    items = [dict(row) for row in selected[:limit]]
    # Cursor stores the selected source key separately from returned display fields.
    next_position = selected[limit - 1]["page_key"] if len(selected) > limit else ""
    return items, {"total": total, "position": next_position}


def snapshot(service, context, arguments, tx):
    _operator(context)
    validate_fields(arguments, {"limit", "cursor", "view", "filters", "section"})
    limit = integer(arguments.get("limit", 20), "limit", 1, 100)
    view, section = arguments.get("view", "current"), arguments.get("section", "all")
    if view not in {"current", "all"} or section not in {"all", "actors", "actions", "work"}:
        raise CoordinationError("INVALID_ARGUMENT", "Invalid operator view or section")
    filters = _filters(arguments)
    fingerprint = _fingerprint(context, {"type": "snapshot", "view": view, "section": section, "filters": filters})
    positions = {}
    if arguments.get("cursor"):
        cursor = _decode(arguments["cursor"], fingerprint)
        if set(cursor) != {"query", "positions"} or not isinstance(cursor["positions"], dict):
            raise CoordinationError("INVALID_ARGUMENT", "Invalid snapshot cursor")
        positions = cursor["positions"]
        if set(positions) - {"actors", "activities", "readiness", "jobs", "failures", "actions"}:
            raise CoordinationError("INVALID_ARGUMENT", "Unknown snapshot cursor sections")
        if any(value is not None and (not isinstance(value, str) or len(value) > 8192) for value in positions.values()):
            raise CoordinationError("INVALID_ARGUMENT", "Invalid snapshot cursor positions")
    output = {"actors": [], "activities": [], "readiness": [], "jobs": [], "failures": [], "actions": {"items": [], "total": 0, "cursor": None}, "counts": {}, "pages": {}}
    pages = {}
    if section in {"all", "actors"}:
        clauses, values = _actor_conditions(filters, "a.id")
        if view == "current":
            clauses.append("a.archived=0")
        actors, page = _page(tx, table="actors a", select="a.id AS page_key,a.id,a.label,a.harness,a.reported_state,a.archived,t.task,p.observed_state,p.observed_us,ac.created_us AS activity_us",
                            joins="LEFT JOIN assignments t ON t.generation=a.current_task_generation LEFT JOIN presence p ON p.actor_id=a.id LEFT JOIN current_activity ca ON ca.actor_id=a.id LEFT JOIN activities ac ON ac.id=ca.activity_id",
                            clauses=clauses, values=values, key="a.id", position=positions.get("actors"), limit=limit, fingerprint=fingerprint, section="actors")
        for actor in actors:
            actor.pop("page_key")
            actor["observed_state"] = actor["observed_state"] or "unknown"
        output["actors"], pages["actors"] = actors, page
        output["counts"]["actors"] = page["total"]
        output["counts"]["current_actors"] = tx.connection.execute("SELECT COUNT(*) FROM actors WHERE archived=0").fetchone()[0]
        output["counts"]["historical_actors"] = tx.connection.execute("SELECT COUNT(*) FROM actors WHERE archived=1").fetchone()[0]
    if section in {"all", "actions"}:
        # The application's one canonical pending composer owns coalescing and semantics.
        action_position = positions.get("actions")
        actions = service.pending(tx, context, limit=limit, cursor=action_position or None, filters=filters, byte_budget=PAGE_BYTES - 2048)
        if not isinstance(actions, dict) or not isinstance(actions.get("items"), list):
            raise CoordinationError("INVALID_ARGUMENT", "Canonical operator action projection is not configured")
        output["actions"] = actions
        if action_position == "":
            actions = {**actions, "items": [], "cursor": None}
            output["actions"] = actions
        pages["actions"] = {"total": actions.get("total", 0), "position": actions.get("cursor") or ""}
    if section in {"all", "work"}:
        specs = (
            ("activities", "activities w", "w.id,w.actor_id,w.task,w.state,substr(w.note,1,512) AS note,length(w.note)>512 AS truncated,w.sequence,w.created_us", "w.actor_id", "w.id", "activity_paths", "activity_id", ""),
            ("readiness", "receipts w", "w.id,w.producer_id,w.artifact,w.status,w.version,w.sequence", "w.producer_id", "w.id", "receipt_paths", "receipt_id", ""),
            ("jobs", "jobs w", "w.id,w.actor_id,w.kind,w.due_us,w.state,w.attempts", "w.actor_id", "w.id", None, None, "w.state NOT IN ('succeeded','cancelled')"),
            ("failures", "operations w", "w.id,w.actor_id,w.kind,w.state,w.updated_us", "w.actor_id", "w.id", None, None, "w.state IN ('failed','uncertain')"),
        )
        for name, table, select, actor_expr, key, scope_table, scope_key, extra in specs:
            clauses, values = _actor_conditions({k: v for k, v in filters.items() if k != "task" and (k != "path" or scope_table is None)}, actor_expr)
            if "task" in filters:
                if name == "activities":
                    clauses.append("w.task=?")
                else:
                    generation = "w.producer_task_generation" if name == "readiness" else "w.task_generation"
                    clauses.append(f"EXISTS(SELECT 1 FROM assignments st WHERE st.generation={generation} AND st.task=?)")
                values.append(filters["task"])
            if "path" in filters and scope_table:
                clause, scoped = _scope("sp", scope_table, scope_key, "w.id", filters["path"])
                clauses.append(clause)
                values.extend(scoped)
            if extra:
                clauses.append(extra)
            if name == "activities" and view == "current":
                clauses.append(f"w.id IN ({CURRENT_ACTIVITY_IDS})")
            items, page = _page(tx, table=table, select=f"{key} AS page_key,{select}", clauses=clauses, values=values, key=key, position=positions.get(name), limit=limit, fingerprint=fingerprint, section=name)
            for item in items:
                item.pop("page_key")
            output[name], pages[name] = items, page
            output["counts"][name] = page["total"]
    next_positions = {name: page["position"] for name, page in pages.items()}
    output["pages"] = {name: {"total": page["total"], "more": bool(page["position"])} for name, page in pages.items()}
    output["cursor"] = _encode({"query": fingerprint, "positions": next_positions}) if any(bool(value) for value in next_positions.values()) else None
    if len(canonical_json(output).encode()) > PAGE_BYTES:
        raise CoordinationError("INVALID_ARGUMENT", "Snapshot exceeds display budget; request a smaller page or a narrower section")
    return output


def operations():
    return (Operation("operator.snapshot", snapshot, actor_required=False), Operation("operator.history", history, actor_required=False))
