"""Actor pending selection keeps authority while excluding unrelated history."""
from __future__ import annotations

import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agentcoord import decisions, identity
from agentcoord.application import build_service
from agentcoord.core import Call, Context


def uid():
    return str(uuid.uuid4())


@pytest.fixture
def graph(tmp_path):
    workspace = SimpleNamespace(id=uid(), root=tmp_path, state_dir=tmp_path / "state",
                                database_path=tmp_path / "state" / "runtime.sqlite3")
    service = build_service(workspace)
    bindings = {name: identity.bind_native(service.store, {
        "harness": "codex", "native_session_id": uid(), "task": name,
    }) for name in ("viewer", "sender", "supplier", "outsider")}
    parent = bindings["viewer"]["context"].actor_id
    with service.store.read() as tx:
        native = tx.connection.execute("SELECT native_session_id FROM actors WHERE id=?",
                                       (parent,)).fetchone()[0]
    bindings["child"] = identity.bind_native(service.store, {
        "harness": "codex", "native_session_id": native, "child_id": uid(),
        "parent_id": parent, "source": "native_child_start", "task": "child",
    })
    return service, bindings


def context(graph, actor):
    service, bindings = graph
    return identity.context_from_token(service.store, bindings[actor]["token"])


def invoke(graph, actor, operation, arguments):
    service, _ = graph
    spec = service.operations[operation]
    result = service.execute(context(graph, actor), Call(operation, arguments,
                                                       uid() if spec.keyed else None))
    assert result["ok"], result
    return result["data"]


def request(graph, sender, recipient, label):
    return invoke(graph, sender, "decision.request", {
        "recipient": context(graph, recipient).actor_id, "subject": label,
        "body": "Explicit decision evidence", "paths": [f"src/{label}"],
    })


def consume_notices(graph, actor, decision_id):
    with graph[0].store.read() as tx:
        notices = [row[0] for row in tx.connection.execute(
            """SELECT n.message_id FROM decision_notifications n JOIN recipients r
            ON r.message_id=n.message_id WHERE n.decision_id=? AND r.actor_id=?
            AND r.handled_us IS NULL""", (decision_id, context(graph, actor).actor_id))]
    for notice in notices:
        invoke(graph, actor, "message.consume", {"id": notice})


@pytest.fixture
def routes(graph):
    outgoing = request(graph, "viewer", "supplier", "outgoing")
    incoming = request(graph, "sender", "viewer", "incoming")
    # Three routes to one record must never inflate selection or exact counts.
    invoke(graph, "viewer", "decision.follow", {"id": incoming["id"]})
    escalated = request(graph, "sender", "child", "escalated")
    with graph[0].store.write() as tx:
        tx.connection.execute("UPDATE decisions SET deadline_us=? WHERE id=?",
                              (tx.now_us - 1, escalated["id"]))
        assert decisions.route_due(tx, context(graph, "sender"))[0]["routing_state"] == "escalated"
    consume_notices(graph, "viewer", escalated["id"])
    closed = request(graph, "sender", "viewer", "closed")
    invoke(graph, "sender", "decision.resolve", {
        "id": closed["id"], "state": "cancelled", "response": "Cancelled explicitly",
    })
    followed = request(graph, "sender", "supplier", "followed")
    invoke(graph, "viewer", "decision.follow", {"id": followed["id"]})
    invoke(graph, "sender", "decision.resolve", {
        "id": followed["id"], "state": "cancelled", "response": "Resolved before follow inspection",
    })
    deferred = request(graph, "sender", "viewer", "deferred")
    invoke(graph, "viewer", "decision.defer", {
        "id": deferred["id"], "until_us": deferred["deadline_us"] - 1,
        "note": "Continue independent work first",
    })
    consume_notices(graph, "viewer", deferred["id"])
    un_escalated = request(graph, "sender", "child", "waiting-child")
    unrelated = request(graph, "outsider", "supplier", "unrelated")
    invoke(graph, "outsider", "decision.resolve", {
        "id": unrelated["id"], "state": "cancelled", "response": "Historical result",
    })
    return {name: record for name, record in zip(
        ("outgoing", "incoming", "escalated", "closed", "followed", "deferred",
         "un_escalated", "unrelated"),
        (outgoing, incoming, escalated, closed, followed, deferred, un_escalated, unrelated),
        strict=True)}


def test_all_authorized_routes_deduplicate_and_keep_closed_notices(graph, routes):
    viewer = context(graph, "viewer")
    expected = {routes[name]["id"] for name in
                ("outgoing", "incoming", "escalated", "closed", "followed")}
    with graph[0].store.read() as tx:
        actions = decisions.select_actions(tx, viewer)
        assert {action["id"] for action in actions} == expected
        assert len(actions) == len(expected)
        assert decisions.count_pending(tx, viewer) == {
            "decisions": 3, "decision_waits": 1, "follow_updates": 1,
        }
        by_id = {action["id"]: action for action in actions}
        assert by_id[routes["outgoing"]["id"]]["kind"] == "decision_wait"
        assert by_id[routes["followed"]["id"]]["kind"] == "decision_follow"
        assert by_id[routes["closed"]["id"]]["state"] == "cancelled"
        assert by_id[routes["closed"]["id"]]["notice_count"] == 2
    consume_notices(graph, "viewer", routes["closed"]["id"])
    with graph[0].store.read() as tx:
        assert routes["closed"]["id"] not in {
            action["id"] for action in decisions.select_actions(tx, viewer)}


def test_deferral_hides_only_until_deadline_or_deferral_expiry(graph, routes):
    viewer = context(graph, "viewer")
    deferred = routes["deferred"]
    with graph[0].store.read() as tx:
        tx.now_us = deferred["deadline_us"] - 2
        assert deferred["id"] not in {row["id"] for row in decisions.select_actions(tx, viewer)}
        tx.now_us = deferred["deadline_us"] - 1
        assert deferred["id"] in {row["id"] for row in decisions.select_actions(tx, viewer)}
    with graph[0].store.write() as tx:
        tx.connection.execute("UPDATE decisions SET deadline_us=? WHERE id=?",
                              (tx.now_us - 1, deferred["id"]))
    with graph[0].store.read() as tx:
        assert deferred["id"] in {row["id"] for row in decisions.select_actions(tx, viewer)}


def test_filters_presentation_versions_and_keyset_apply_before_limit(graph, routes):
    viewer = context(graph, "viewer")
    with graph[0].store.write() as tx:
        incoming = next(row for row in decisions.select_actions(tx, viewer)
                        if row["id"] == routes["incoming"]["id"])
        tx.connection.execute("INSERT INTO action_presentations VALUES (?,?,?,?,?)",
                              (viewer.actor_id, incoming["kind"], incoming["id"],
                               incoming["version"], tx.now_us))
    with graph[0].store.read() as tx:
        selected = decisions.select_actions(tx, viewer, limit=1, new_only=True,
                                           filters={"path": "src/incoming"})
        assert selected == []
        assert not sum(decisions.count_pending(tx, viewer, filters={
            "actor_id": context(graph, "sender").actor_id}).values())
        assert [row["id"] for row in decisions.select_actions(
            tx, viewer, limit=1, filters={"task": "viewer"})] == [routes["outgoing"]["id"]]
        page, after = [], 0
        while batch := decisions.select_actions(tx, viewer, after=after, limit=1):
            page.extend(batch)
            after = batch[-1]["sequence"]
        assert len(page) == len({row["id"] for row in page}) == 5
        assert [row["sequence"] for row in page] == sorted(row["sequence"] for row in page)
    invoke(graph, "sender", "decision.resolve", {
        "id": routes["incoming"]["id"], "state": "cancelled", "response": "New explicit version",
    })
    with graph[0].store.read() as tx:
        updated = decisions.select_actions(tx, viewer, limit=1, new_only=True,
                                          filters={"path": "src/incoming"})
        assert len(updated) == 1 and updated[0]["version"] > incoming["version"]


# The original visibility contract expressed independently as a full-scan oracle.
# It deliberately retains the expensive algorithm only in this test.
_REFERENCE_VISIBILITY = """SELECT d.id,d.sequence,
    CASE WHEN d.sender_id=:actor THEN 'decision_wait'
      WHEN d.recipient_id!=:actor AND NOT(d.parent_id=:actor AND d.routing_state='escalated')
      THEN 'decision_follow' ELSE 'decision' END AS kind
    FROM decisions d LEFT JOIN deferrals defer ON defer.decision_id=d.id WHERE
    (d.state='open' AND (d.sender_id=:actor OR
      ((d.recipient_id=:actor OR (d.parent_id=:actor AND d.routing_state='escalated'))
       AND (defer.until_us IS NULL OR defer.until_us<=:now OR d.deadline_us<=:now))))
    OR EXISTS(SELECT 1 FROM decision_notifications n JOIN recipients r ON r.message_id=n.message_id
      WHERE n.decision_id=d.id AND r.actor_id=:actor AND r.handled_us IS NULL)
    OR EXISTS(SELECT 1 FROM followers f WHERE f.decision_id=d.id AND f.actor_id=:actor AND f.active=1)
    ORDER BY d.sequence"""


def test_selection_and_counts_match_original_visibility_for_every_actor(graph, routes):
    for name in graph[1]:
        actor = context(graph, name)
        with graph[0].store.read() as tx:
            expected = [tuple(row) for row in tx.connection.execute(
                _REFERENCE_VISIBILITY, {"actor": actor.actor_id, "now": tx.now_us})]
            actual = decisions.select_actions(tx, actor)
            assert [(row["id"], row["sequence"], row["kind"]) for row in actual] == expected
            counts = decisions.count_pending(tx, actor)
            assert counts == {
                "decisions": sum(row[2] == "decision" for row in expected),
                "decision_waits": sum(row[2] == "decision_wait" for row in expected),
                "follow_updates": sum(row[2] == "decision_follow" for row in expected),
            }
    operator = Context(graph[0].workspace.id, None, operator=True, transport="operator")
    with graph[0].store.read() as tx:
        expected = {record["id"] for record in routes.values()}
        actions = decisions.select_actions(tx, operator)
        assert {row["id"] for row in actions} == expected
        assert sum(decisions.count_pending(tx, operator).values()) == len(expected)


def _selection_steps(tx, actor):
    ticks = 0
    def progress():
        nonlocal ticks
        ticks += 1
        return 0
    tx.connection.set_progress_handler(progress, 100)
    try:
        actions = decisions.select_actions(tx, actor, limit=3)
        counts = decisions.count_pending(tx, actor)
    finally:
        tx.connection.set_progress_handler(None, 0)
    assert ticks > 0
    return actions, counts, ticks


def test_actor_selection_work_does_not_grow_with_unrelated_history(graph, routes):
    viewer = context(graph, "viewer")
    outsider = context(graph, "outsider").actor_id
    with graph[0].store.read() as tx:
        before_actions, before_counts, before_steps = _selection_steps(tx, viewer)
    with graph[0].store.write() as tx:
        # Offline retained history has valid actor/generation/state/event links;
        # it belongs to other actors and has no unhandled notice for this viewer.
        for _ in range(2000):
            identifier = uid()
            sequence = tx.event("decisions", "cancelled", identifier, outsider, {})
            tx.connection.execute("""INSERT INTO decisions SELECT ?,sender_id,recipient_id,
                recipient_task_generation,parent_id,parent_task_generation,task,subject,body,
                deadline_us,state,routing_state,response,resolved_by,resolved_us,version,?
                FROM decisions WHERE id=?""", (identifier, sequence, routes["unrelated"]["id"]))
        assert tx.connection.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 2008
    with graph[0].store.read() as tx:
        after_actions, after_counts, after_steps = _selection_steps(tx, viewer)
    assert after_actions == before_actions
    assert after_counts == before_counts
    # SQLite instruction work, not wall time on a shared host: unrelated retained
    # records must not multiply the actor's selection/count work.
    assert after_steps <= before_steps * 2 + 10, (before_steps, after_steps)
