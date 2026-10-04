import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agentcoord import identity, messages, operator, work
from agentcoord.config import Config
from agentcoord.core import Call, Context, Service
from agentcoord.store import Store


@pytest.fixture
def runtime(tmp_path):
    workspace_id = str(uuid.uuid4())
    store = Store(tmp_path / "state/runtime.sqlite3", workspace_id)
    store.initialize((identity.SCHEMA, work.SCHEMA, messages.SCHEMA))
    service = Service(store, SimpleNamespace(id=workspace_id), Config(), (*operator.operations(), *messages.operations(), *work.operations()))
    context = Context(workspace_id, None, operator=True, transport="operator")
    return store, service, context


def register(store, native, *, task="prepare"):
    with store.write() as tx:
        return identity.register_native(tx, {"harness": "codex", "native_session_id": native, "task": task})


def invoke(service, context, operation, arguments):
    reply = service.execute(context, Call(operation, arguments))
    assert reply["ok"], reply
    return reply["data"]


def test_actor_traversal_reaches_archived_rows_without_inflating_current_count(runtime):
    store, service, context = runtime
    created = [register(store, f"actor-{number}") for number in range(26)]
    with store.write() as tx:
        tx.connection.execute("UPDATE actors SET archived=1 WHERE id=?", (created[0]["id"],))
    data = invoke(service, context, "operator.snapshot", {"section": "actors", "limit": 7})
    seen = [item["id"] for item in data["actors"]]
    assert data["counts"]["current_actors"] == 25
    while data["cursor"]:
        data = invoke(service, context, "operator.snapshot", {"section": "actors", "limit": 7, "cursor": data["cursor"]})
        seen.extend(item["id"] for item in data["actors"])
    assert len(seen) == len(set(seen)) == 25
    assert created[0]["id"] not in seen
    data = invoke(service, context, "operator.snapshot", {"section": "actors", "view": "all", "limit": 100})
    assert len(data["actors"]) == 26


def test_actor_scope_and_task_filters_apply_before_limit(runtime):
    store, service, context = runtime
    for number in range(8):
        register(store, f"unrelated-{number}", task="other")
    target = register(store, "target", task="target")
    actor_context = identity.bind_native(store, {"harness": "codex", "native_session_id": "target"})["context"]
    result = service.execute(actor_context, Call("work.activity", {"task": "target", "paths": ["src/module"], "note": "purpose", "state": "working"}, "activity-target"))
    assert result["ok"], result
    data = invoke(service, context, "operator.snapshot", {"section": "actors", "limit": 1, "filters": {"task": "target", "path": "src"}})
    assert [item["id"] for item in data["actors"]] == [target["id"]]
    data = invoke(service, context, "operator.snapshot", {"section": "actors", "limit": 1, "filters": {"path": "src/mod"}})
    assert not data["actors"]


def test_history_defaults_to_latest_and_retains_bound_highwater_during_traversal(runtime):
    store, service, context = runtime
    actor = register(store, "history")
    with store.write() as tx:
        original = [tx.event("work", "evidence", str(uuid.uuid4()), actor["id"], {"note": f"event {i}"}) for i in range(8)]
    first = invoke(service, context, "operator.history", {"limit": 3})
    assert [item["sequence"] for item in first["items"]] == original[-3:][::-1]
    with store.write() as tx:
        new_sequence = tx.event("work", "evidence", str(uuid.uuid4()), actor["id"], {"note": "after snapshot"})
    seen = [item["sequence"] for item in first["items"]]
    data = first
    while data["cursor"]:
        data = invoke(service, context, "operator.history", {"limit": 3, "cursor": data["cursor"]})
        seen.extend(item["sequence"] for item in data["items"])
    assert new_sequence not in seen
    assert len(seen) == len(set(seen))
    forward = invoke(service, context, "operator.history", {"limit": 100, "direction": "forward"})
    assert [item["sequence"] for item in forward["items"]] == sorted(seen + [new_sequence])


def test_changed_history_filters_and_snapshot_view_reject_prior_cursor(runtime):
    store, service, context = runtime
    for number in range(3):
        register(store, f"cursor-{number}")
    page = invoke(service, context, "operator.history", {"limit": 1})
    reply = service.execute(context, Call("operator.history", {"limit": 1, "domain": "work", "cursor": page["cursor"]}))
    assert not reply["ok"] and reply["error"]["code"] == "INVALID_ARGUMENT"
    page = invoke(service, context, "operator.snapshot", {"section": "actors", "limit": 1})
    reply = service.execute(context, Call("operator.snapshot", {"section": "actors", "view": "all", "limit": 1, "cursor": page["cursor"]}))
    assert not reply["ok"] and reply["error"]["code"] == "INVALID_ARGUMENT"


def test_full_body_search_and_operator_reads_leave_recipient_unhandled(runtime):
    store, service, context = runtime
    register(store, "sender")
    recipient = register(store, "recipient")
    sender_context = identity.bind_native(store, {"harness": "codex", "native_session_id": "sender"})["context"]
    with store.write() as tx:
        message = messages.append_message(tx, sender_context, recipient_ids=[recipient["id"]], kind="HANDOFF", subject="small preview", body="x" * 5000 + "needle-body", thread="thread", paths=["src/file"])
    data = invoke(service, context, "operator.history", {"query": "needle-body"})
    assert len(data["items"]) == 1
    assert data["items"][0]["record_id"] == message["id"]
    body = invoke(service, context, "message.get", {"id": message["id"]})
    assert body["body"].endswith("needle-body")
    with store.read() as tx:
        recipient_row = tx.connection.execute("SELECT presented_us,handled_us FROM recipients WHERE message_id=?", (message["id"],)).fetchone()
        assert tuple(recipient_row) == (None, None)


def test_operator_projection_refuses_actor_connection(runtime):
    store, service, _ = runtime
    register(store, "ordinary")
    context = identity.bind_native(store, {"harness": "codex", "native_session_id": "ordinary"})["context"]
    reply = service.execute(context, Call("operator.snapshot", {"section": "actors"}))
    assert not reply["ok"] and reply["error"]["code"] == "NOT_AUTHORIZED"


def test_assembled_actions_share_canonical_pending_and_coalesce_linked_notices(tmp_path):
    from agentcoord import pending
    from agentcoord.application import build_service
    workspace_id = str(uuid.uuid4())
    workspace = SimpleNamespace(id=workspace_id, root=tmp_path, state_dir=tmp_path / "state",
                                database_path=tmp_path / "state/runtime.sqlite3")
    service = build_service(workspace)
    sender = identity.bind_native(service.store, {"harness": "codex", "native_session_id": "assembled-sender", "task": "producer"})["context"]
    recipient = identity.bind_native(service.store, {"harness": "claude", "native_session_id": "assembled-recipient", "task": "consumer"})["context"]
    request = service.execute(sender, Call("decision.request", {"recipient": recipient.actor_id, "subject": "Review scoped input", "body": "Choose the prepared counterpart", "paths": ["src/file"]}, "assembled-request"))
    assert request["ok"], request
    plain = service.execute(sender, Call("message.send", {"recipients": [recipient.actor_id], "kind": "HANDOFF", "subject": "Ordinary note", "body": "Full authored content", "paths": ["src/file"]}, "assembled-message"))
    assert plain["ok"], plain
    context = Context(workspace_id, None, operator=True, transport="operator")
    with service.store.read() as tx:
        canonical = pending.select(tx, context, limit=20, filters={"path": "src"}, byte_budget=98000)
        before = [tuple(row) for row in tx.connection.execute("SELECT message_id,presented_us,handled_us FROM recipients ORDER BY message_id")]
    projection = invoke(service, context, "operator.snapshot", {"section": "actions", "filters": {"path": "src"}})
    assert projection["actions"] == canonical
    assert projection["actions"]["total"] == 2
    assert {item["kind"] for item in projection["actions"]["items"]} == {"decision", "message"}
    with service.store.read() as tx:
        assert [tuple(row) for row in tx.connection.execute("SELECT message_id,presented_us,handled_us FROM recipients ORDER BY message_id")] == before
    unrelated = invoke(service, context, "operator.snapshot", {"section": "actions", "filters": {"path": "src/fil"}})
    assert unrelated["actions"]["total"] == 0


def test_assembled_work_pages_do_not_repeat_finished_sections(tmp_path):
    from agentcoord.application import build_service
    from agentcoord.config import register_workspace
    workspace = register_workspace(tmp_path, state_root=tmp_path / "state")
    workspace_id = workspace.id
    service = build_service(workspace)
    actor = identity.bind_native(service.store, {"harness": "codex", "native_session_id": "work-pages", "task": "work"})["context"]
    for index in range(6):
        result = service.execute(actor, Call("work.activity", {"note": f"Evidence {index}", "paths": ["src/file"], "state": "working"}, f"work-page-{index}"))
        assert result["ok"], result
    context = Context(workspace_id, None, operator=True, transport="operator")
    data = invoke(service, context, "operator.snapshot", {"section": "work", "limit": 2})
    ids = [item["id"] for item in data["activities"]]
    while data["cursor"]:
        data = invoke(service, context, "operator.snapshot", {"section": "work", "limit": 2, "cursor": data["cursor"]})
        ids.extend(item["id"] for item in data["activities"])
    assert len(ids) == len(set(ids)) == 6
