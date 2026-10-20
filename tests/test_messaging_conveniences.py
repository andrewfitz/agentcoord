"""Conveniences retain explicit addressing and durable obligations."""
import uuid
from types import SimpleNamespace

import pytest

from agentcoord import identity, readiness
from agentcoord.application import build_service
from agentcoord.core import Call, Context


@pytest.fixture
def runtime(tmp_path):
    service = build_service(SimpleNamespace(id=str(uuid.uuid4()), root=tmp_path,
        state_dir=tmp_path / "state", database_path=tmp_path / "state/runtime.sqlite3"))
    actors = [identity.bind_native(service.store, {"harness": "codex",
        "native_session_id": str(uuid.uuid4()), "task": "messaging"})["context"] for _ in range(3)]
    return service, actors


def call(service, actor, operation, arguments):
    result = service.execute(actor, Call(operation, arguments, None if operation in {"message.get", "operator.snapshot"} else str(uuid.uuid4())))
    assert result["ok"], result
    return result["data"]


def test_reply_inherits_context_but_only_deliberate_recipients(runtime):
    service, actors = runtime
    original = call(service, actors[0], "message.send", {"recipients": [actors[1].actor_id],
        "kind": "handoff", "subject": "Parser", "body": "Keep escapes", "thread": "parser",
        "paths": ["src/parser.py"], "context": {"artifact": "parser"}, "wake": False})
    reply = call(service, actors[1], "message.send", {"reply_to": original["id"],
        "recipients": [actors[2].actor_id], "kind": "reply", "subject": "Parser result",
        "body": "Done", "wake": False})
    read = call(service, actors[2], "message.get", {"id": reply["id"]})
    assert read["thread"] == "parser"
    with service.store.read() as tx:
        assert tx.connection.execute("SELECT sender_id FROM messages WHERE id=?", (reply["id"],)).fetchone()[0] == actors[1].actor_id
        assert [r[0] for r in tx.connection.execute("SELECT actor_id FROM recipients WHERE message_id=?", (reply["id"],))] == [actors[2].actor_id]
        assert tx.connection.execute("SELECT path FROM message_paths WHERE message_id=?", (reply["id"],)).fetchone()[0] == "src/parser.py"
    hidden = service.execute(actors[2], Call("message.send", {"reply_to": original["id"],
        "recipients": [actors[0].actor_id], "kind": "reply", "subject": "hidden", "body": "no"}, str(uuid.uuid4())))
    assert not hidden["ok"]


def test_question_reuses_only_identical_request(runtime):
    service, actors = runtime
    args = {"recipient": actors[1].actor_id, "subject": "Parser ownership", "body": "May I edit?", "paths": ["src/parser.py"]}
    first = call(service, actors[0], "decision.question", args)
    second = call(service, actors[0], "decision.question", args)
    assert first["id"] == second["id"] and second["reused"]
    third = call(service, actors[0], "decision.question", {**args, "body": "May I replace it?"})
    assert third["id"] != first["id"]
    with service.store.read() as tx:
        assert tx.connection.execute("SELECT COUNT(*) FROM decision_notifications WHERE decision_id=?", (first["id"],)).fetchone()[0] == 1


def test_history_excluded_from_actions_counts_and_retained_for_reads(runtime):
    service, actors = runtime
    args = {"recipients": [actors[1].actor_id], "subject": "Imported chatter", "body": "old queue", "wake": False}
    history = call(service, actors[0], "message.send", {**args, "kind": "HISTORY"})
    live = call(service, actors[0], "message.send", {**args, "kind": "handoff", "body": "short result"})
    sync = call(service, actors[1], "message.sync", {})
    assert sync["counts"]["messages"] == 1
    assert [item["id"] for item in sync["items"]] == [live["id"]]
    assert sync["items"][0]["body"] == "short result"
    assert call(service, actors[1], "message.get", {"id": history["id"]})["body"] == "old queue"
    operator = Context(service.store.workspace_id, None, operator=True, transport="operator")
    snapshot = call(service, operator, "operator.snapshot", {"section": "actions"})
    assert snapshot["carryover"]["imported_history"] == 1


def test_wait_registers_without_polling_and_returns_existing_update(runtime):
    service, actors = runtime
    args = {"producer": actors[0].actor_id, "artifact": "parser", "paths": ["src/parser.py"], "next_action": "inspect parser"}
    first = call(service, actors[1], "readiness.wait", args)
    assert first["receipt"] is None and first["next_action"] == "continue_independent_work"
    second = call(service, actors[1], "readiness.wait", args)
    assert first["subscription"]["id"] == second["subscription"]["id"]


def test_wait_observes_already_published_receipt_atomically(runtime):
    service, actors = runtime
    with service.store.write() as tx:
        receipt = readiness.publish_prepared(service, actors[0],
            {"artifact": "parser", "paths": ["src/parser.py"], "evidence": {"test": "passed"}},
            {"latest_id": None, "latest_version": 0}, {"src/parser.py": "a" * 64}, tx)
    result = call(service, actors[1], "readiness.wait", {"producer": actors[0].actor_id,
        "artifact": "parser", "paths": ["src/parser.py"], "next_action": "inspect"})
    assert result["receipt"]["id"] == receipt["id"]
    assert result["update_id"] is not None
    again = call(service, actors[1], "readiness.wait", {"producer": actors[0].actor_id,
        "artifact": "parser", "paths": ["src/parser.py"], "next_action": "inspect"})
    assert again["update_id"] == result["update_id"]


def test_operator_groups_decisions_by_their_scope_and_preserves_old_obligations(runtime):
    service, actors = runtime
    decision = call(service, actors[0], "decision.question", {"recipient": actors[1].actor_id,
        "subject": "Review parser", "body": "Proceed?", "paths": ["src/parser.py"]})
    with service.store.write() as tx:
        tx.connection.execute("UPDATE actors SET archived=1 WHERE id=?", (actors[1].actor_id,))
    operator = Context(service.store.workspace_id, None, operator=True, transport="operator")
    snapshot = call(service, operator, "operator.snapshot", {"section": "actions", "filters": {"path": "src"}})
    assert [item["id"] for item in snapshot["decisions"]] == [decision["id"]]
    assert snapshot["carryover"]["archived_recipient"] == 1
    unrelated = call(service, operator, "operator.snapshot", {"section": "actions", "filters": {"path": "other"}})
    assert unrelated["decisions"] == [] and unrelated["carryover"] == {}


def test_reply_to_unthreaded_message_persists_authenticated_relation(runtime):
    service, actors = runtime
    original = call(service, actors[0], "message.send", {"recipients": [actors[1].actor_id],
        "kind": "handoff", "subject": "Unthreaded", "body": "Existing message", "wake": False})
    reply = call(service, actors[1], "message.send", {"reply_to": original["id"],
        "recipients": [actors[0].actor_id], "kind": "reply", "subject": "Result", "body": "Done",
        "context": {"reply_to": actors[2].actor_id}, "wake": False})
    read = call(service, actors[0], "message.get", {"id": reply["id"]})
    assert read["thread"] == f"message:{original['id']}"
    assert read["context"]["reply_to"] == original["id"]
    assert read["sender_id"] == actors[1].actor_id
    override = call(service, actors[1], "message.send", {"reply_to": original["id"],
        "recipients": [actors[0].actor_id], "kind": "reply", "subject": "Result", "body": "Done",
        "thread": "deliberate-thread", "wake": False})
    assert call(service, actors[0], "message.get", {"id": override["id"]})["thread"] == "deliberate-thread"
    forged = service.execute(actors[1], Call("message.send", {"recipients": [actors[0].actor_id],
        "kind": "reply", "subject": "Result", "body": "Done", "context": {"reply_to": original["id"]}}, str(uuid.uuid4())))
    assert not forged["ok"] and "server-derived" in forged["error"]["message"]


@pytest.mark.parametrize("status,paths", [("withdrawn", ["src/parser.py"]), ("ready", ["src/other.py"])])
def test_wait_never_suggests_accepting_withdrawn_or_incomplete_receipt(runtime, status, paths):
    service, actors = runtime
    with service.store.write() as tx:
        receipt_id = str(uuid.uuid4())
        sequence = tx.event("readiness", "published", receipt_id, actors[0].actor_id, {})
        tx.connection.execute("INSERT INTO receipts VALUES (?,?,?,?,?,?,?,?,?,?)", (
            receipt_id, actors[0].actor_id, actors[0].task_generation, "parser", status,
            '{"' + paths[0] + '":"' + 'a' * 64 + '"}', '{}', 1, sequence, tx.now_us))
    result = call(service, actors[1], "readiness.wait", {"producer": actors[0].actor_id,
        "artifact": "parser", "paths": ["src/parser.py"], "next_action": "inspect"})
    assert result["receipt"]["id"] == receipt_id
    assert result["next_action"] == "continue_independent_work"
    assert result["unavailable_reason"]


def test_reply_retry_is_exact_and_changed_reply_conflicts(runtime):
    service, actors = runtime
    original = call(service, actors[0], "message.send", {"recipients": [actors[1].actor_id],
        "kind": "handoff", "subject": "Parser", "body": "Inspect", "wake": False})
    args = {"reply_to": original["id"], "recipients": [actors[0].actor_id],
        "kind": "reply", "subject": "Result", "body": "Done", "wake": False}
    key = str(uuid.uuid4())
    first = service.execute(actors[1], Call("message.send", args, key))
    second = service.execute(actors[1], Call("message.send", args, key))
    assert first["ok"] and second["ok"]
    assert first["data"] == second["data"]
    conflict = service.execute(actors[1], Call("message.send", {**args, "thread": "different"}, key))
    assert not conflict["ok"] and conflict["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    with service.store.read() as tx:
        assert tx.connection.execute("SELECT COUNT(*) FROM messages WHERE sender_id=?", (actors[1].actor_id,)).fetchone()[0] == 1


def test_stale_current_activity_does_not_advertise_uncorrelated_child(runtime):
    service, actors = runtime
    call(service, actors[1], "work.activity", {"state": "working", "note": "old scope", "paths": ["src/parser.py"]})
    with service.store.write() as tx:
        newer = str(uuid.uuid4())
        tx.connection.execute("INSERT INTO assignments VALUES (?,?,?,?)", (newer, actors[1].actor_id, "new-task", tx.now_us))
        tx.connection.execute("UPDATE actors SET child_id='uncorrelated-child',current_task_generation=? WHERE id=?", (newer, actors[1].actor_id))
    operator = Context(service.store.workspace_id, None, operator=True, transport="operator")
    current = call(service, operator, "operator.snapshot", {"section": "actors"})
    assert actors[1].actor_id not in [row["id"] for row in current["actors"]]
    history = call(service, operator, "operator.snapshot", {"section": "actors", "view": "all"})
    assert actors[1].actor_id in [row["id"] for row in history["actors"]]
