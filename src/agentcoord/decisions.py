"""Explicit decisions, scoped followers and once-only parent routing."""
from __future__ import annotations

import uuid
from dataclasses import replace

from . import identity, messages
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
    """CREATE TABLE decisions (
        id TEXT PRIMARY KEY, sender_id TEXT NOT NULL REFERENCES actors(id),
        recipient_id TEXT NOT NULL REFERENCES actors(id),
        recipient_task_generation TEXT NOT NULL REFERENCES assignments(generation),
        parent_id TEXT REFERENCES actors(id), parent_task_generation TEXT REFERENCES assignments(generation),
        task TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL, deadline_us INTEGER NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('open','answered','declined','cancelled')),
        routing_state TEXT NOT NULL CHECK(routing_state IN ('waiting','escalated','diagnostic')),
        response TEXT, resolved_by TEXT REFERENCES actors(id), resolved_us INTEGER,
        version INTEGER NOT NULL CHECK(version>0), sequence INTEGER NOT NULL REFERENCES events(sequence),
        CHECK((parent_id IS NULL)=(parent_task_generation IS NULL)))""",
    "CREATE INDEX decisions_incoming ON decisions(recipient_id,state,deadline_us)",
    "CREATE INDEX decisions_sender ON decisions(sender_id,state,sequence)",
    "CREATE INDEX decisions_parent ON decisions(parent_id,routing_state,state)",
    """CREATE TABLE decision_paths (
        decision_id TEXT NOT NULL REFERENCES decisions(id), path TEXT NOT NULL,
        PRIMARY KEY(decision_id,path))""",
    "CREATE INDEX decision_paths_scope ON decision_paths(path,decision_id)",
    """CREATE TABLE decision_notifications (
        decision_id TEXT NOT NULL REFERENCES decisions(id), message_id TEXT NOT NULL REFERENCES messages(id),
        kind TEXT NOT NULL, PRIMARY KEY(decision_id,message_id))""",
    "CREATE INDEX decision_notice_message ON decision_notifications(message_id)",
    """CREATE TABLE deferrals (
        decision_id TEXT PRIMARY KEY REFERENCES decisions(id), actor_id TEXT NOT NULL REFERENCES actors(id),
        until_us INTEGER NOT NULL, note TEXT NOT NULL, version INTEGER NOT NULL CHECK(version>0))""",
    """CREATE TABLE followers (
        decision_id TEXT NOT NULL REFERENCES decisions(id), actor_id TEXT NOT NULL REFERENCES actors(id),
        task_generation TEXT NOT NULL REFERENCES assignments(generation), active INTEGER NOT NULL CHECK(active IN (0,1)),
        seen_version INTEGER NOT NULL DEFAULT 0, handled_version INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(decision_id,actor_id))""",
    "CREATE INDEX followers_actor ON followers(actor_id,active,seen_version)",
    """CREATE TABLE answer_proposals (
        id TEXT PRIMARY KEY, decision_id TEXT NOT NULL REFERENCES decisions(id), actor_id TEXT NOT NULL REFERENCES actors(id),
        actor_generation TEXT NOT NULL REFERENCES assignments(generation),
        state TEXT NOT NULL CHECK(state IN ('pending','accepted','rejected','superseded')),
        response TEXT NOT NULL, proposed_state TEXT NOT NULL CHECK(proposed_state IN ('answered','declined')),
        created_us INTEGER NOT NULL, reconciled_by TEXT REFERENCES actors(id), reconciled_us INTEGER)""",
    "CREATE INDEX proposals_pending ON answer_proposals(decision_id,state,created_us)",
    """CREATE TABLE decision_transitions (
        id TEXT PRIMARY KEY, decision_id TEXT NOT NULL REFERENCES decisions(id), actor_id TEXT NOT NULL REFERENCES actors(id),
        from_state TEXT NOT NULL, to_state TEXT NOT NULL, detail_json TEXT NOT NULL,
        sequence INTEGER NOT NULL UNIQUE REFERENCES events(sequence))""",
)


def _fail(message, code="INVALID_ARGUMENT"):
    raise CoordinationError(code, message)


def _decision(tx, decision_id):
    identifier(decision_id, "decision")
    row = tx.connection.execute("SELECT * FROM decisions WHERE id=?", (decision_id,)).fetchone()
    if row is None:
        _fail("Decision does not exist", "NOT_FOUND")
    return dict(row)


def _notice(tx, context, decision, kind, recipient_ids, body):
    if len(body.encode("utf-8")) > 65536:
        body = messages.text_chunk(body)["text"] + "\nExcerpt; retrieve the complete retained decision with decision.get."
    notice = messages.append_message(tx, context, recipient_ids=recipient_ids, kind=kind,
        subject=decision["subject"], body=body, thread=decision["task"],
        paths=[r[0] for r in tx.connection.execute("SELECT path FROM decision_paths WHERE decision_id=? ORDER BY path", (decision["id"],))],
        declared_context={"decision_id": decision["id"], "version": decision["version"]})
    tx.connection.execute("INSERT INTO decision_notifications VALUES (?,?,?)", (decision["id"], notice["id"], kind))
    return notice


def _transition(tx, context, decision, to_state, detail):
    transition_id = str(uuid.uuid4())
    seq = tx.event("decisions", to_state, decision["id"], context.actor_id, detail)
    tx.connection.execute("INSERT INTO decision_transitions VALUES (?,?,?,?,?,?,?)", (
        transition_id, decision["id"], context.actor_id, decision["state"], to_state, canonical_json(detail), seq))
    tx.connection.execute("UPDATE decisions SET version=version+1,sequence=? WHERE id=?", (seq, decision["id"]))


def _actor(service, tx, context):
    row = service.require_actor(tx, context)
    service.require_generation(tx, row["id"], context.task_generation)
    return row


def request(service, context, args, tx):
    validate_fields(args, {"recipient", "subject", "body", "paths", "deadline_us"}, {"recipient", "subject", "body", "paths"})
    sender = _actor(service, tx, context)
    recipient = messages.actor(tx, args["recipient"])
    if recipient["archived"] or recipient["id"] == sender["id"]:
        _fail("A request requires a different current recipient")
    scoped = normalize_paths(args["paths"], allow_root=True)
    if not scoped:
        _fail("A decision requires explicit scope")
    subject = bounded_text(args["subject"], "subject", 1024)
    body = bounded_text(args["body"], "body", 65536)
    deadline = integer(args.get("deadline_us", tx.now_us + 4*3600*1_000_000), "deadline_us", tx.now_us, tx.now_us + 7*86400*1_000_000)
    parent = identity.authorized_parent(tx, recipient["id"], recipient["current_task_generation"])
    if parent and parent["id"] == sender["id"]:
        parent = None
    assignment = tx.connection.execute("SELECT task FROM assignments WHERE generation=?", (sender["current_task_generation"],)).fetchone()
    decision_id = str(uuid.uuid4())
    seq = tx.event("decisions", "requested", decision_id, sender["id"], {})
    tx.connection.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?, 'open','waiting',NULL,NULL,NULL,1,?)", (
        decision_id, sender["id"], recipient["id"], recipient["current_task_generation"],
        parent["id"] if parent else None, parent["current_task_generation"] if parent else None,
        assignment["task"], subject, body, deadline, seq))
    tx.connection.executemany("INSERT INTO decision_paths VALUES (?,?)", [(decision_id, p) for p in scoped])
    decision = _decision(tx, decision_id)
    _notice(tx, context, decision, "request", [recipient["id"]], body)
    return decision


def _can_answer(tx, decision, actor_id):
    if actor_id == decision["recipient_id"]:
        return True
    return actor_id == decision["parent_id"] and decision["routing_state"] == "escalated"


def _stale_answer(tx, decision, actor_id):
    recipient = messages.actor(tx, decision["recipient_id"])
    if recipient["current_task_generation"] != decision["recipient_task_generation"]:
        return True
    if actor_id == decision["parent_id"]:
        parent = (identity.authorized_parent(tx, recipient["id"], decision["recipient_task_generation"])
                  if recipient["current_task_generation"] == decision["recipient_task_generation"] else None)
        return not parent or parent["id"] != actor_id or parent["current_task_generation"] != decision["parent_task_generation"]
    return False


def _finish(tx, context, decision, state, response, detail=None):
    _transition(tx, context, decision, state, detail or {})
    tx.connection.execute("UPDATE decisions SET state=?,response=?,resolved_by=?,resolved_us=? WHERE id=?",
                          (state, response, context.actor_id, tx.now_us, decision["id"]))
    tx.connection.execute("UPDATE answer_proposals SET state='superseded',reconciled_by=?,reconciled_us=? WHERE decision_id=? AND state='pending'",
                          (context.actor_id, tx.now_us, decision["id"]))
    current = _decision(tx, decision["id"])
    targets = [decision["sender_id"]] if state != "cancelled" else [decision["recipient_id"]]
    if state == "cancelled" and decision["routing_state"] == "escalated":
        targets.append(decision["parent_id"])
    targets = [target for target in dict.fromkeys(targets) if target != context.actor_id and not messages.actor(tx, target)["archived"]]
    if targets:
        _notice(tx, context, current, state, targets, response or "The requester cancelled this decision.")
    return current


def resolve(service, context, args, tx):
    validate_fields(args, {"id", "state", "response", "external_settlement"}, {"id", "state", "response"})
    actor = _actor(service, tx, context)
    decision = _decision(tx, args["id"])
    state = args["state"]
    if state not in {"answered", "declined", "cancelled"}:
        _fail("Choose answered, declined or cancelled")
    response = bounded_text(args["response"], "response", 65536, allow_empty=state == "cancelled")
    requester = actor["id"] == decision["sender_id"]
    settlement = args.get("external_settlement")
    if state == "cancelled":
        if not requester:
            _fail("Only the requester cancels a decision", "NOT_AUTHORIZED")
    elif requester:
        if state != "answered" or not isinstance(settlement, dict):
            _fail("Requester settlement requires an actual externally supplied answer", "NOT_AUTHORIZED")
        validate_fields(settlement, {"answered_by", "evidence"}, {"answered_by", "evidence"})
        identifier(settlement["answered_by"], "external answer actor")
        if not _can_answer(tx, decision, settlement["answered_by"]):
            _fail("External answer came from an unauthorized decision actor", "NOT_AUTHORIZED")
        bounded_text(settlement["evidence"], "answer evidence", 4096)
        if _stale_answer(tx, decision, settlement["answered_by"]):
            _fail("Changed-generation external answers require exact proposal reconciliation", "STALE_GENERATION")
    elif not _can_answer(tx, decision, actor["id"]):
        _fail("Only the recipient or explicitly escalated parent answers", "NOT_AUTHORIZED")
    if decision["state"] != "open":
        if (decision["state"], decision["resolved_by"], decision["response"]) == (state, actor["id"], response):
            return decision
        _fail("Decision already has a different resolution", "STALE_VERSION")
    if not requester and _stale_answer(tx, decision, actor["id"]):
        existing = tx.connection.execute("SELECT * FROM answer_proposals WHERE decision_id=? AND actor_id=? AND actor_generation=? AND proposed_state=? AND response=? AND state='pending' ORDER BY created_us DESC LIMIT 1",
            (decision["id"], actor["id"], actor["current_task_generation"], state, response)).fetchone()
        if existing:
            proposal = dict(existing)
        else:
            proposal_id = str(uuid.uuid4())
            tx.connection.execute("UPDATE answer_proposals SET state='superseded',reconciled_by=?,reconciled_us=? WHERE decision_id=? AND actor_id=? AND state='pending'",
                (actor["id"], tx.now_us, decision["id"], actor["id"]))
            tx.connection.execute("INSERT INTO answer_proposals VALUES (?,?,?,?, 'pending',?,?,?,NULL,NULL)",
                (proposal_id, decision["id"], actor["id"], actor["current_task_generation"], response, state, tx.now_us))
            _transition(tx, context, decision, "proposal", {"proposal_id": proposal_id})
            current = _decision(tx, decision["id"])
            _notice(tx, context, current, "proposal", [decision["sender_id"]], response)
            proposal = dict(tx.connection.execute("SELECT * FROM answer_proposals WHERE id=?", (proposal_id,)).fetchone())
        return {"state": "reconciliation_required", "decision_id": decision["id"], "proposal": proposal}
    return _finish(tx, context, decision, state, response, {"external_settlement": settlement} if requester and settlement else None)


def reconcile(service, context, args, tx):
    validate_fields(args, {"id", "proposal_id", "accept"}, {"id", "proposal_id", "accept"})
    actor = _actor(service, tx, context)
    decision = _decision(tx, args["id"])
    if actor["id"] != decision["sender_id"] or type(args["accept"]) is not bool:
        _fail("Requester must explicitly accept or reject the exact proposal", "NOT_AUTHORIZED")
    identifier(args["proposal_id"], "proposal")
    row = tx.connection.execute("SELECT * FROM answer_proposals WHERE id=? AND decision_id=?", (args["proposal_id"], decision["id"])).fetchone()
    if row is None:
        _fail("Proposal does not belong to this decision", "NOT_FOUND")
    proposal = dict(row)
    terminal = "accepted" if args["accept"] else "rejected"
    if proposal["state"] != "pending":
        if proposal["state"] == terminal:
            return decision
        _fail("Proposal already has a different disposition", "STALE_VERSION")
    if decision["state"] != "open":
        _fail("Only open decisions accept proposals", "STALE_VERSION")
    if args["accept"]:
        supplier = messages.actor(tx, proposal["actor_id"])
        if supplier["current_task_generation"] != proposal["actor_generation"] or supplier["archived"]:
            _fail("Proposal supplier changed generation again", "STALE_GENERATION")
        result = _finish(tx, context, decision, proposal["proposed_state"], proposal["response"],
            {"accepted_proposal": proposal["id"], "answered_by": proposal["actor_id"]})
        tx.connection.execute("UPDATE decisions SET resolved_by=? WHERE id=?", (proposal["actor_id"], decision["id"]))
        result["resolved_by"] = proposal["actor_id"]
    else:
        _transition(tx, context, decision, "proposal_rejected", {"proposal_id": proposal["id"]})
        result = _decision(tx, decision["id"])
    tx.connection.execute("UPDATE answer_proposals SET state=?,reconciled_by=?,reconciled_us=? WHERE id=?",
                          (terminal, actor["id"], tx.now_us, proposal["id"]))
    return result


def defer(service, context, args, tx):
    validate_fields(args, {"id", "until_us", "note"}, {"id", "until_us", "note"})
    actor = _actor(service, tx, context)
    decision = _decision(tx, args["id"])
    if decision["state"] != "open" or not _can_answer(tx, decision, actor["id"]):
        _fail("Only the current recipient or escalated parent defers an open decision", "NOT_AUTHORIZED")
    if _stale_answer(tx, decision, actor["id"]):
        _fail("Decision authority changed", "STALE_GENERATION")
    until = integer(args["until_us"], "until_us", tx.now_us + 1, decision["deadline_us"])
    note = bounded_text(args["note"], "note", 2000)
    prior = tx.connection.execute("SELECT * FROM deferrals WHERE decision_id=?", (decision["id"],)).fetchone()
    if prior and (prior["actor_id"], prior["until_us"], prior["note"]) == (actor["id"], until, note):
        return dict(prior)
    _transition(tx, context, decision, "deferred", {"until_us": until})
    current = _decision(tx, decision["id"])
    tx.connection.execute("INSERT INTO deferrals VALUES (?,?,?,?,?) ON CONFLICT(decision_id) DO UPDATE SET actor_id=excluded.actor_id,until_us=excluded.until_us,note=excluded.note,version=excluded.version",
                          (decision["id"], actor["id"], until, note, current["version"]))
    _notice(tx, context, current, "deferred", [decision["sender_id"]], note)
    return {"decision_id": decision["id"], "until_us": until, "note": note, "version": current["version"]}


def _view(tx, context, decision, *, offset=0, response_offset=0, limit=8192, proposal_id=None, proposals_after=None):
    follower = tx.connection.execute("SELECT active FROM followers WHERE decision_id=? AND actor_id=?", (decision["id"], context.actor_id)).fetchone()
    if not context.operator and context.actor_id not in {decision["sender_id"], decision["recipient_id"]} and not _can_answer(tx, decision, context.actor_id) and not (follower and follower["active"]):
        _fail("Decision is unavailable to this actor", "NOT_AUTHORIZED")
    result = dict(decision)
    for field, position in (("body", offset), ("response", response_offset)):
        chunk = messages.text_chunk(result[field], offset=position, limit=limit)
        result[field] = chunk.pop("text")
        result[field + "_chunk"] = chunk
    result["paths"] = [row[0] for row in tx.connection.execute("SELECT path FROM decision_paths WHERE decision_id=? ORDER BY path", (decision["id"],))]
    result["proposals"] = []
    if context.operator or context.actor_id == decision["sender_id"]:
        if proposal_id:
            identifier(proposal_id, "proposal")
            row = tx.connection.execute("SELECT * FROM answer_proposals WHERE id=? AND decision_id=?", (proposal_id, decision["id"])).fetchone()
            if row is None:
                _fail("Proposal does not belong to this decision", "NOT_FOUND")
            proposal = dict(row)
            proposal["response_chunk"] = messages.text_chunk(proposal.pop("response"), offset=response_offset, limit=limit)
            result["proposal"] = proposal
        else:
            if proposals_after is not None:
                identifier(proposals_after, "proposals_after")
            rows = tx.connection.execute("""SELECT id,actor_id,actor_generation,state,proposed_state,
                created_us,length(CAST(response AS BLOB)) AS response_bytes FROM answer_proposals
                WHERE decision_id=? AND state='pending' AND id>? ORDER BY id LIMIT 21""",
                (decision["id"], proposals_after or "")).fetchall()
            result["proposals"] = [dict(row) for row in rows[:20]]
            result["proposals_after"] = rows[19]["id"] if len(rows)>20 else None
    elif proposal_id is not None:
        _fail("Only the requester reads answer proposals", "NOT_AUTHORIZED")
    return result


def get(service, context, args, tx):
    validate_fields(args, {"id", "offset", "response_offset", "limit", "proposal_id", "proposals_after"}, {"id"})
    return _view(tx, context, _decision(tx, args["id"]), **{key: value for key, value in args.items() if key != "id"})


def find(service, context, args, tx):
    validate_fields(args, {"paths", "limit", "after"}, {"paths"})
    scoped = normalize_paths(args["paths"], allow_root=True)
    if not scoped:
        _fail("Discovery requires scope")
    limit = integer(args.get("limit", 20), "limit", 1, 100)
    after = integer(args.get("after", 0), "after", 0, 2**63-1)
    rows = tx.connection.execute("""SELECT d.id,d.sender_id,d.recipient_id,d.subject,d.state,d.routing_state,d.deadline_us,d.version,d.sequence
        FROM decisions d WHERE d.state='open' AND d.sequence>? AND EXISTS (
            SELECT 1 FROM decision_paths p JOIN json_each(?) q ON p.path=q.value OR p.path='.' OR q.value='.'
                OR instr(p.path,q.value||'/')=1 OR instr(q.value,p.path||'/')=1 WHERE p.decision_id=d.id)
        ORDER BY d.sequence LIMIT ?""", (after, canonical_json(scoped), limit + 1)).fetchall()
    selected = [dict(row) for row in rows[:limit]]
    return {"decisions": selected, "after": selected[-1]["sequence"] if len(rows) > limit else None,
            "instruction": "Scope is a discovery lead. Following grants no authority."}


def follow(service, context, args, tx):
    validate_fields(args, {"id"}, {"id"})
    actor = _actor(service, tx, context)
    decision = _decision(tx, args["id"])
    tx.connection.execute("INSERT INTO followers VALUES (?,?,?,1,?,0) ON CONFLICT(decision_id,actor_id) DO UPDATE SET task_generation=excluded.task_generation,active=1,seen_version=excluded.seen_version",
                          (decision["id"], actor["id"], actor["current_task_generation"], decision["version"]))
    return {"following": True, "decision": _view(tx, context, decision)}


def unfollow(service, context, args, tx):
    validate_fields(args, {"id"}, {"id"})
    _actor(service, tx, context)
    _decision(tx, args["id"])
    tx.connection.execute("UPDATE followers SET active=0 WHERE decision_id=? AND actor_id=?", (args["id"], context.actor_id))
    return {"id": args["id"], "following": False}


def transfer(service, context, args, tx):
    validate_fields(args, {"id", "recipient", "reason"}, {"id", "recipient", "reason"})
    _actor(service, tx, context)
    decision = _decision(tx, args["id"])
    if decision["state"] != "open" or context.actor_id != decision["sender_id"]:
        _fail("Only the requester explicitly transfers an open decision", "NOT_AUTHORIZED")
    recipient = messages.actor(tx, args["recipient"])
    if recipient["archived"] or recipient["id"] == context.actor_id:
        _fail("Transfer requires another current actor")
    reason = bounded_text(args["reason"], "reason", 2000)
    parent = identity.authorized_parent(tx, recipient["id"], recipient["current_task_generation"])
    if parent and parent["id"] == context.actor_id:
        parent = None
    _transition(tx, context, decision, "transferred", {"old_recipient": decision["recipient_id"], "new_recipient": recipient["id"], "reason": reason})
    tx.connection.execute("UPDATE decisions SET recipient_id=?,recipient_task_generation=?,parent_id=?,parent_task_generation=?,routing_state='waiting' WHERE id=?",
        (recipient["id"], recipient["current_task_generation"], parent["id"] if parent else None,
         parent["current_task_generation"] if parent else None, decision["id"]))
    tx.connection.execute("DELETE FROM deferrals WHERE decision_id=?", (decision["id"],))
    tx.connection.execute("UPDATE answer_proposals SET state='superseded',reconciled_by=?,reconciled_us=? WHERE decision_id=? AND state='pending'", (context.actor_id, tx.now_us, decision["id"]))
    current = _decision(tx, decision["id"])
    targets = list(dict.fromkeys([decision["recipient_id"], recipient["id"]]))
    targets = [item for item in targets if not messages.actor(tx, item)["archived"]]
    _notice(tx, context, current, "transfer", targets, reason)
    return current


def route_due(tx, context, *, limit=20):
    """One bounded daemon pass; unavailable authority stays explicitly unresolved."""
    rows = tx.connection.execute("""SELECT d.* FROM decisions d LEFT JOIN presence p ON p.actor_id=d.recipient_id
        JOIN actors a ON a.id=d.recipient_id WHERE d.state='open' AND d.routing_state='waiting' AND
        (d.deadline_us<=? OR a.archived=1 OR a.reported_state='completed' OR p.observed_state='offline'
         OR a.current_task_generation!=d.recipient_task_generation) ORDER BY d.deadline_us,d.id LIMIT ?""",
        (tx.now_us, integer(limit, "limit", 1, 100))).fetchall()
    result = []
    for raw in rows:
        decision = dict(raw)
        sender = messages.actor(tx, decision["sender_id"])
        bound = replace(context, actor_id=sender["id"], task_generation=sender["current_task_generation"], operator=False)
        recipient = messages.actor(tx, decision["recipient_id"])
        parent = (identity.authorized_parent(tx, recipient["id"], decision["recipient_task_generation"])
                  if recipient["current_task_generation"] == decision["recipient_task_generation"] else None)
        reason = None
        if recipient["current_task_generation"] != decision["recipient_task_generation"]:
            reason = "stale_recipient"
        elif not parent or parent["id"] != decision["parent_id"] or parent["current_task_generation"] != decision["parent_task_generation"]:
            reason = "missing_or_stale_parent"
        else:
            presence = tx.connection.execute("SELECT observed_state FROM presence WHERE actor_id=?", (parent["id"],)).fetchone()
            if parent["archived"] or parent["reported_state"] == "completed" or presence and presence[0] == "offline":
                reason = "parent_unavailable"
        routing = "diagnostic" if reason else "escalated"
        _transition(tx, bound, decision, routing, {"reason": reason} if reason else {"parent_id": parent["id"]})
        tx.connection.execute("UPDATE decisions SET routing_state=? WHERE id=?", (routing, decision["id"]))
        current = _decision(tx, decision["id"])
        target = sender["id"] if reason else parent["id"]
        if not messages.actor(tx, target)["archived"]:
            _notice(tx, bound, current, routing, [target], reason or decision["body"])
        result.append({"id": decision["id"], "routing_state": routing, "reason": reason})
    return result


def _pending_query(context, now_us, *, new_only=False, filters=None):
    filters = messages.pending_filters(filters)
    if context.operator:
        candidates, source = "", "decisions d"
        involved = "d.state='open'"
        notice = "EXISTS (SELECT 1 FROM decision_notifications n JOIN recipients r ON r.message_id=n.message_id WHERE n.decision_id=d.id AND r.handled_us IS NULL)"
        followed = "EXISTS (SELECT 1 FROM followers f WHERE f.decision_id=d.id AND f.active=1)"
        kind = f"CASE WHEN d.state!='open' AND NOT ({notice}) AND ({followed}) THEN 'decision_follow' ELSE 'decision' END"
        owner = """CASE WHEN d.state='open' THEN d.recipient_id ELSE COALESCE(
            (SELECT MIN(r.actor_id) FROM decision_notifications n JOIN recipients r ON r.message_id=n.message_id
             WHERE n.decision_id=d.id AND r.handled_us IS NULL),
            (SELECT MIN(f.actor_id) FROM followers f WHERE f.decision_id=d.id AND f.active=1)) END"""
    else:
        # Bound actor views before the cross-table visibility predicate. UNION
        # deduplicates records reached through several authorized routes.
        candidates = """WITH relevant(id) AS (
            SELECT id FROM decisions WHERE sender_id=:actor AND state='open'
            UNION SELECT id FROM decisions WHERE recipient_id=:actor AND state='open'
            UNION SELECT id FROM decisions WHERE parent_id=:actor
                AND routing_state='escalated' AND state='open'
            UNION SELECT n.decision_id FROM recipients r JOIN decision_notifications n
                ON n.message_id=r.message_id WHERE r.actor_id=:actor AND r.handled_us IS NULL
            UNION SELECT decision_id FROM followers WHERE actor_id=:actor AND active=1
        ) """
        source = "relevant v JOIN decisions d ON d.id=v.id"
        involved = """(d.state='open' AND (d.sender_id=:actor OR ((d.recipient_id=:actor OR
            (d.parent_id=:actor AND d.routing_state='escalated')) AND
            (defer.until_us IS NULL OR defer.until_us<=:now OR d.deadline_us<=:now))))"""
        notice = "EXISTS (SELECT 1 FROM decision_notifications n JOIN recipients r ON r.message_id=n.message_id WHERE n.decision_id=d.id AND r.actor_id=:actor AND r.handled_us IS NULL)"
        followed = "EXISTS (SELECT 1 FROM followers f WHERE f.decision_id=d.id AND f.actor_id=:actor AND f.active=1)"
        kind = """CASE WHEN d.sender_id=:actor THEN 'decision_wait' WHEN d.recipient_id!=:actor
            AND NOT (d.parent_id=:actor AND d.routing_state='escalated') THEN 'decision_follow' ELSE 'decision' END"""
        owner = ":actor"
    # Named parameters permit one predicate to serve selection and exact counts.
    values = {"actor": context.actor_id, "now": now_us}
    sql = candidates + f"""SELECT d.id,d.sender_id,d.recipient_id,d.recipient_task_generation,d.parent_id,
        d.parent_task_generation,d.subject,d.state,d.routing_state,d.deadline_us,d.version,d.sequence,
        {kind} AS action_kind,{owner} AS actor_id,
        recipient.current_task_generation AS current_recipient_generation,
        recipient.reported_state AS recipient_state,recipient.archived AS recipient_archived,
        presence.observed_state AS recipient_presence,
        EXISTS (SELECT 1 FROM followers f JOIN actors a ON a.id=f.actor_id
            WHERE f.decision_id=d.id AND f.active=1 AND f.actor_id={owner}
              AND f.task_generation!=a.current_task_generation) AS stale_follow FROM {source}
        JOIN actors recipient ON recipient.id=d.recipient_id
        LEFT JOIN presence ON presence.actor_id=d.recipient_id
        LEFT JOIN deferrals defer ON defer.decision_id=d.id WHERE ({involved} OR {notice} OR {followed})"""
    if "actor_id" in filters:
        sql += """ AND (d.sender_id=:filter_actor OR d.recipient_id=:filter_actor OR
            (d.parent_id=:filter_actor AND d.routing_state='escalated') OR EXISTS
            (SELECT 1 FROM decision_notifications n JOIN recipients r ON r.message_id=n.message_id
             WHERE n.decision_id=d.id AND r.actor_id=:filter_actor AND r.handled_us IS NULL) OR EXISTS
            (SELECT 1 FROM followers f WHERE f.decision_id=d.id AND f.actor_id=:filter_actor AND f.active=1))"""
        values["filter_actor"] = filters["actor_id"]
        if not context.operator:
            sql += " AND :actor=:filter_actor"
    if "task" in filters:
        sql += " AND d.task=:task"
        values["task"] = filters["task"]
    if "path" in filters:
        sql += """ AND EXISTS (SELECT 1 FROM decision_paths p WHERE p.decision_id=d.id AND
            (p.path=:path OR p.path='.' OR :path='.' OR instr(p.path,:path||'/')=1 OR instr(:path,p.path||'/')=1))"""
        values["path"] = filters["path"]
    if new_only and not context.operator:
        sql = """SELECT * FROM (""" + sql + """) candidate WHERE NOT EXISTS
            (SELECT 1 FROM action_presentations p WHERE p.actor_id=:actor AND p.kind=candidate.action_kind
             AND p.record_id=candidate.id AND p.version=candidate.version)"""
    return sql, values


def select_actions(tx, context, *, after=None, limit=20, new_only=False, filters=None):
    after = 0 if after is None else integer(after, "after", 0, 2**63-1)
    sql, values = _pending_query(context, tx.now_us, new_only=new_only, filters=filters)
    values.update(after=after, limit=integer(limit, "limit", 1, 100))
    rows = tx.connection.execute("SELECT * FROM (" + sql + ") WHERE sequence>:after ORDER BY sequence LIMIT :limit", values).fetchall()
    return [{"kind": row["action_kind"], "id": row["id"], "version": row["version"],
        "actor_id": row["actor_id"], "sender_id": row["sender_id"], "recipient_id": row["recipient_id"],
        "sequence": row["sequence"], "summary": row["subject"], "state": row["state"],
        "status": ("obsolete_generation" if row["action_kind"] == "decision_follow" and row["stale_follow"]
                   or row["state"] == "open" and row["current_recipient_generation"] != row["recipient_task_generation"]
                   else "owner_unavailable" if row["state"] == "open" and (row["recipient_archived"]
                        or row["recipient_state"] == "completed" or row["recipient_presence"] == "offline") else row["state"]),
        "routing_state": row["routing_state"], "deadline_us": row["deadline_us"],
        **messages.notice_ids(tx, context, "decision_notifications", "decision_id", row["id"]),
        "next_action": "decision.get"} for row in rows]


def select_waits(tx, context, *, after=None, limit=20, new_only=False, filters=None):
    sql, values = _pending_query(context, tx.now_us, new_only=new_only, filters=filters)
    values.update(after=0 if after is None else integer(after, "after", 0, 2**63-1),
                  limit=integer(limit, "limit", 1, 100))
    rows = tx.connection.execute("SELECT * FROM (" + sql + ") WHERE action_kind='decision_wait' AND sequence>:after ORDER BY sequence LIMIT :limit", values).fetchall()
    return [{"kind": row["action_kind"], "id": row["id"], "version": row["version"],
        "sequence": row["sequence"], "summary": row["subject"], "state": row["state"],
        "next_action": "decision.get", **messages.notice_ids(tx, context, "decision_notifications", "decision_id", row["id"])} for row in rows]


def count_pending(tx, context, *, new_only=False, filters=None):
    sql, values = _pending_query(context, tx.now_us, new_only=new_only, filters=filters)
    rows = tx.connection.execute("SELECT action_kind,COUNT(*) FROM (" + sql + ") GROUP BY action_kind", values)
    counts = {row[0]: row[1] for row in rows}
    return {"decisions": counts.get("decision", 0), "decision_waits": counts.get("decision_wait", 0),
            "follow_updates": counts.get("decision_follow", 0)}


def mark_presented(tx, context, actions):
    if context.operator:
        _fail("Operator reads cannot present another actor's decisions", "NOT_AUTHORIZED")
    for action in actions:
        tx.connection.execute("UPDATE followers SET seen_version=MAX(seen_version,?) WHERE decision_id=? AND actor_id=? AND active=1",
            (action["version"], action["id"], context.actor_id))


def listing(service, context, args, tx):
    validate_fields(args, {"after", "limit"})
    return {"actions": select_actions(tx, context, after=args.get("after"), limit=args.get("limit", 20)), "pending_counts": count_pending(tx, context)}


def operations():
    return tuple(Operation(name, handler, mutation, mutation, not name.endswith(".get")) for name, handler, mutation in (
        ("decision.request", request, True), ("decision.find", find, False), ("decision.get", get, False),
        ("decision.list", listing, False), ("decision.follow", follow, True), ("decision.unfollow", unfollow, True),
        ("decision.defer", defer, True), ("decision.resolve", resolve, True), ("decision.reconcile", reconcile, True),
        ("decision.transfer", transfer, True)))
