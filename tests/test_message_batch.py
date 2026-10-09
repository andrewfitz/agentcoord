"""Selected messages stay exact, visible, bounded and free of read side effects."""
import json
import uuid
from types import SimpleNamespace

import pytest

from agentcoord import cli, identity, messages, pending
from agentcoord.application import build_service
from agentcoord.core import Call, Context, canonical_json
from agentcoord.mcp import tool_result


@pytest.fixture
def runtime(tmp_path):
    service = build_service(SimpleNamespace(id=str(uuid.uuid4()), root=tmp_path,
        state_dir=tmp_path / "state", database_path=tmp_path / "state/runtime.sqlite3"))
    actors = [identity.bind_native(service.store, {"harness": "codex",
        "native_session_id": str(uuid.uuid4()), "task": "messaging"})["context"] for _ in range(3)]
    return service, actors


def send(runtime, body, *, recipient=1, declared_context=None):
    service, actors = runtime
    result = service.execute(actors[0], Call("message.send", {
        "recipients": [actors[recipient].actor_id], "kind": "handoff",
        "subject": "Preserve escaped delimiters", "body": body, "thread": "parser",
        "context": declared_context or {},
    }, str(uuid.uuid4())))
    assert result["ok"], result
    return result["data"]["id"]


def test_batch_reads_visible_messages_in_order_and_does_not_log_or_handle(runtime):
    service, actors = runtime
    ids = [send(runtime, "Keep quoted commas."), send(runtime, "Emoji 🧩 stays intact.")]
    hidden = send(runtime, "Private instruction", recipient=2)
    with service.store.read() as tx:
        events = tx.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        receipts = tx.connection.execute("SELECT COUNT(*) FROM idempotency").fetchone()[0]
    result = service.execute(actors[1], Call("message.get_batch", {"ids": [*ids, hidden]}))
    assert result["ok"], result
    data = result["data"]
    assert data["next_index"] is None
    assert [item["id"] for item in data["items"]] == [*ids, hidden]
    assert [item["body"] for item in data["items"][:2]] == ["Keep quoted commas.", "Emoji 🧩 stays intact."]
    assert data["items"][2]["error"]["code"] == "NOT_FOUND"
    assert "Private instruction" not in canonical_json(result)
    assert all(item["next_offset"] is None for item in data["items"][:2])
    with service.store.read() as tx:
        assert tx.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == events
        assert tx.connection.execute("SELECT COUNT(*) FROM idempotency").fetchone()[0] == receipts
        assert tx.connection.execute("SELECT COUNT(*) FROM recipients WHERE presented_us IS NOT NULL OR handled_us IS NOT NULL OR acknowledged_us IS NOT NULL").fetchone()[0] == 0


def test_batch_budget_pages_all_ids_and_unicode_chunks_without_loss(runtime):
    service, actors = runtime
    bodies = ["🧩" * 4000 + str(i) for i in range(4)]
    ids = [send(runtime, body, declared_context={"reason": "x" * 8000}) for body in bodies]
    index, seen, recovered = 0, [], []
    while index is not None:
        result = service.execute(actors[1], Call("message.get_batch", {
            "ids": ids, "index": index, "body_limit": 32768, "byte_budget": 16384,
        }))
        assert result["ok"], result
        data = result["data"]
        assert len(canonical_json(data).encode()) <= 16384
        assert data["items"]
        for item in data["items"]:
            assert item["ok"]
            seen.append(item["id"])
            body, offset = item["body"], item["next_offset"]
            assert offset is not None
            while offset is not None:
                chunk = service.execute(actors[1], Call("message.get", {
                    "id": item["id"], "offset": offset, "limit": 4096,
                }))
                assert chunk["ok"], chunk
                body += chunk["data"]["body"]
                offset = chunk["data"]["next_offset"]
            recovered.append(body)
        if data["next_index"] is not None:
            assert data["next_index"] > index
        index = data["next_index"]
    assert seen == ids
    assert recovered == bodies


@pytest.mark.parametrize("ids", [[], [{}], ["not-a-uuid"], ["00000000-0000-0000-0000-000000000001"] * 2])
def test_invalid_batch_ids_fail_explicitly(runtime, ids):
    service, actors = runtime
    result = service.execute(actors[1], Call("message.get_batch", {"ids": ids}))
    assert not result["ok"]
    assert result["error"]["code"] == "INVALID_ARGUMENT"


def test_digest_identifies_sender_and_marks_actual_truncation(runtime):
    service, actors = runtime
    short = send(runtime, "Preserve the parser fix.")
    long = send(runtime, "A" * 500)
    unicode_id = send(runtime, "🧩" * 300)
    with service.store.read() as tx:
        digest = pending.select(tx, actors[1], limit=3)
    previews = {item["id"]: item for item in digest["items"]}
    assert previews[short]["sender_id"] == actors[0].actor_id
    assert previews[short]["subject"] == "Preserve escaped delimiters"
    assert previews[short]["thread"] == "parser"
    assert previews[short]["summary"] == "Preserve the parser fix."
    assert previews[short]["summary_excerpt"] is False
    assert previews[long]["summary_excerpt"] is True
    assert len(previews[long]["summary"]) == 360
    assert previews[unicode_id]["summary_excerpt"] is True
    assert len(previews[unicode_id]["summary"].encode()) <= 720


def test_cli_batch_schema_and_compact_mcp_preserve_exact_content():
    message_id = "00000000-0000-0000-0000-000000000001"
    args = cli.parser().parse_args(["message-batch", "--ids", message_id, "--body-limit", "1024"])
    spec = cli.BY_TOOL["message_batch"]
    assert spec.operation == "message.get_batch" and not spec.mutation
    assert args.ids == [message_id] and args.body_limit == 1024
    envelope = {"ok": True, "protocol": 1, "data": {"body": "Keep literal spaces: a  b, 🧩"}, "action_digest": None}
    encoded = tool_result(envelope).content[0].text
    assert json.loads(encoded) == envelope
    assert len(encoded.encode()) < len(json.dumps(envelope, ensure_ascii=False).encode())


def test_small_batch_budget_is_valid_for_short_messages(runtime):
    service, actors = runtime
    ids = [send(runtime, "short one"), send(runtime, "short two")]
    result = service.execute(actors[1], Call("message.get_batch", {
        "ids": ids, "body_limit": 512, "byte_budget": 4096,
    }))
    assert result["ok"], result
    assert [item["body"] for item in result["data"]["items"]] == ["short one", "short two"]
    assert len(canonical_json(result["data"]).encode()) <= 4096


def test_batch_budget_contract_is_shared_by_cli_and_service():
    spec = cli.BY_TOOL["message_batch"]
    assert spec.schema()["properties"]["byte_budget"]["minimum"] == messages.BATCH_BYTE_BUDGET_MIN
    assert spec.schema()["properties"]["byte_budget"]["maximum"] == messages.BATCH_BYTE_BUDGET_MAX
    cli.validate_arguments(spec, {"ids": ["00000000-0000-0000-0000-000000000001"], "byte_budget": 4096})


def test_tight_batch_budget_does_not_refetch_bodies_when_shortening(runtime):
    service, actors = runtime
    ids = [send(runtime, '"\\\n🧩' * 4000, declared_context={"why": "x" * 8000}) for _ in range(2)]
    with service.store.read() as tx:
        queries = []
        tx.connection.set_trace_callback(queries.append)
        result = messages.read_batch(tx, actors[1], ids, body_limit=32768, byte_budget=16384)
        tx.connection.set_trace_callback(None)
    assert result["items"][0]["next_offset"] is not None
    assert len(canonical_json(result).encode()) <= 16384
    body_reads = [q for q in queries if "SELECT substr(body_utf8" in q]
    assert len(body_reads) <= len(ids)
    # Body, visibility and metadata are fetched at most once per candidate.
    assert len(queries) <= 3 * len(ids)


def test_batch_budget_accounts_for_escaping_and_two_digit_continuations(runtime):
    service, actors = runtime
    body = '"\\\n' * 600 + '🧩' * 100
    ids = [send(runtime, body, declared_context={"why": "x" * 6800}) for _ in range(16)]
    index, seen = 0, []
    with service.store.read() as tx:
        while index is not None:
            result = messages.read_batch(tx, actors[1], ids, index=index, body_limit=32768, byte_budget=16384)
            assert len(canonical_json(result).encode()) <= 16384
            assert result["items"]
            for item in result["items"]:
                assert item["ok"]
                seen.append(item["id"])
                prefix = body.encode()[:item["next_offset"]] if item["next_offset"] else body.encode()
                assert item["body"].encode() == prefix
            assert result["next_index"] is None or result["next_index"] > index
            index = result["next_index"]
    assert seen == ids


def test_counts_deduplicate_recipients_and_keep_filters_without_body_projection(runtime):
    service, actors = runtime
    result = service.execute(actors[0], Call("message.send", {
        "recipients": [a.actor_id for a in actors[1:]], "kind": "handoff",
        "subject": "Shared parser result", "body": "🧩" * 4000,
        "paths": ["src/parser.py"], "thread": "parser-task",
    }, str(uuid.uuid4())))
    assert result["ok"], result
    operator = Context(actors[0].workspace_id, None, operator=True)
    with service.store.read() as tx:
        queries = []
        tx.connection.set_trace_callback(queries.append)
        assert messages.count_pending(tx, operator) == {"messages": 1}
        assert messages.count_pending(tx, operator, filters={"path": "src"}) == {"messages": 1}
        assert messages.count_pending(tx, actors[1], filters={"task": "parser-task"}) == {"messages": 1}
        assert messages.count_pending(tx, operator, filters={"path": "unrelated"}) == {"messages": 0}
    assert all("body_utf8" not in query for query in queries)
    handled = service.execute(actors[1], Call("message.consume", {"id": result["data"]["id"]}, str(uuid.uuid4())))
    assert handled["ok"], handled
    with service.store.read() as tx:
        assert messages.count_pending(tx, actors[1]) == {"messages": 0}
        assert messages.count_pending(tx, operator) == {"messages": 1}


def test_routing_checks_do_not_allocate_large_retained_actor_payload(runtime):
    import tracemalloc

    service, actors = runtime
    with service.store.write() as tx:
        tx.connection.execute("UPDATE actors SET metadata_json=? WHERE id=?",
            (canonical_json({"retained": "x" * 512000}), actors[1].actor_id))
    with service.store.read() as tx:
        tracemalloc.start()
        try:
            actor = messages.actor(tx, actors[1].actor_id, active=True)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
    assert actor["id"] == actors[1].actor_id
    assert actor["current_task_generation"] == actors[1].task_generation
    assert peak < 64000
