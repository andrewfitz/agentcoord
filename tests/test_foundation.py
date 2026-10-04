"""Native authority, durable receipts and private workspace/store boundaries."""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agentcoord import core, identity, pending
from agentcoord.config import (
    Config,
    discover_workspace,
    load_config,
    register_workspace,
    workspace_from_id,
)
from agentcoord.core import (
    Call,
    Context,
    CoordinationError,
    Operation,
    Service,
    canonical_json,
)
from agentcoord.store import Store


def uid():
    return str(uuid.uuid4())


@pytest.fixture
def system(tmp_path):
    workspace = SimpleNamespace(id=uid(), root=tmp_path)
    store = Store(tmp_path / "state" / "runtime.sqlite3", workspace.id)
    store.initialize((identity.SCHEMA, pending.SCHEMA))
    binding = identity.bind_native(store, {"harness": "codex", "native_session_id": uid(), "task": "foundation"})
    service = Service(store, workspace, Config(), (*core.operations(), *identity.operations()))
    return service, binding


def invoke(system, name, arguments=None, *, key=None, context=None, **guards):
    service, binding = system
    return service.execute(context or binding["context"], Call(name, arguments or {}, key, **guards))


def checkpoint(system, state="working", note="Current task", *, key=None, context=None):
    return invoke(system, "identity.checkpoint", {"state": state, "note": note}, key=key or uid(), context=context)


def queue(service, context, kind="example.execute", arguments=None):
    with service.store.write() as tx:
        return service.enqueue(tx, context, kind, arguments or {}, key=uid())["operation_id"]


def start(system, run, *, binding=None):
    service, original = system
    binding = binding or original
    with service.store.write() as tx:
        result = identity.start_execution(tx, binding["context"], native_run_id=run)
    return result, identity.context_from_token(service.store, binding["token"])


def test_uninitialized_reads_create_nothing_and_wrong_workspace_fails(tmp_path, system):
    absent = tmp_path / "missing" / "runtime.sqlite3"
    with pytest.raises(CoordinationError, match="not initialized"), Store(absent, uid()).read():
        pass
    assert not absent.parent.exists()
    service, _ = system
    with pytest.raises(CoordinationError) as failure, Store(service.store.path, uid()).read():
        pass
    assert failure.value.code == "WRONG_WORKSPACE"


def test_schema_initialization_is_atomic_and_signature_is_exact(tmp_path):
    store = Store(tmp_path / "state" / "runtime.sqlite3", uid())
    with pytest.raises(sqlite3.Error):
        store.initialize((identity.SCHEMA, ("CREATE TABLE partial(id TEXT)", "INVALID DDL")))
    with sqlite3.connect(store.path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='partial'").fetchall()
    store.initialize((identity.SCHEMA,))
    store.initialize((identity.SCHEMA,))
    with pytest.raises(CoordinationError) as failure:
        store.initialize((identity.SCHEMA, ("CREATE TABLE changed(id TEXT)",)))
    assert failure.value.code == "SCHEMA_MISMATCH"


def test_read_transactions_are_read_only_and_failed_writes_roll_back(system):
    service, binding = system
    with pytest.raises(sqlite3.Error), service.store.read() as tx:
        tx.connection.execute("DELETE FROM actors")
    with pytest.raises(RuntimeError), service.store.write() as tx:
        tx.connection.execute("UPDATE actors SET label='lost' WHERE id=?", (binding["context"].actor_id,))
        tx.event("example", "lost", uid(), binding["context"].actor_id)
        raise RuntimeError("rollback")
    with service.store.read() as tx:
        assert identity.actor_for_context(tx, binding["context"])["label"] != "lost"
        assert tx.connection.execute("SELECT COUNT(*) FROM events WHERE kind='lost'").fetchone()[0] == 0


def test_private_paths_refuse_symlinks_and_backup_retains_wal_records(system, tmp_path):
    service, binding = system
    assert service.store.path.stat().st_mode & 0o777 == 0o600
    assert service.store.path.parent.stat().st_mode & 0o777 == 0o700
    assert checkpoint(system)["ok"]
    destination = tmp_path / "backups" / "runtime.sqlite3"
    assert service.store.backup(destination)["verified"]
    with Store(destination, service.workspace.id).read() as tx:
        assert identity.actor_for_context(tx, binding["context"])["reported_state"] == "working"
        assert tx.connection.execute("SELECT COUNT(*) FROM idempotency").fetchone()[0] == 1
    alias = tmp_path / "alias"
    alias.symlink_to(service.store.path.parent, target_is_directory=True)
    with pytest.raises(CoordinationError), Store(alias / "runtime.sqlite3", service.workspace.id).read():
        pass
    with pytest.raises(FileExistsError):
        service.store.backup(destination)


@pytest.mark.parametrize("value", [{"bad": "\ud800"}, {"bad": float("nan")}, {"bad": object()}, {"bad": {1: "aliases a different JSON key"}}])
def test_invalid_json_is_typed_and_never_creates_receipt(system, value):
    with pytest.raises(CoordinationError) as failure:
        canonical_json(value)
    assert failure.value.code == "INVALID_ARGUMENT"
    result = invoke(system, "identity.checkpoint", {"state": "working", "note": value["bad"]}, key=uid())
    assert result["error"]["code"] == "INVALID_ARGUMENT"
    with system[0].store.read() as tx:
        assert tx.connection.execute("SELECT COUNT(*) FROM idempotency").fetchone()[0] == 0


def test_retry_survives_assignment_change_and_archiving_without_renewing_authority(system):
    service, binding = system
    key = uid()
    arguments = {"state": "working", "note": "Saved exactly"}
    first = invoke(system, "identity.checkpoint", arguments, key=key)
    assert first["ok"]
    original_generation = binding["context"].task_generation
    with service.store.write() as tx:
        assigned = identity.assign_task(tx, binding["context"], "new task")
        tx.connection.execute("UPDATE actors SET archived=1,reported_state='completed' WHERE id=?", (binding["context"].actor_id,))
    fresh = identity.context_from_token(service.store, binding["token"])
    replay = invoke(system, "identity.checkpoint", arguments, key=key, context=fresh)
    assert replay["ok"] and replay["replayed"]
    assert replay["data"] == first["data"]
    assert replay["receipt_context"]["task_generation"] == original_generation
    assert replay["next_context"]["task_generation"] == assigned["current_task_generation"]
    with service.store.read() as tx:
        actor = identity.actor_for_context(tx, fresh)
        assert actor["archived"] and actor["reported_state"] == "completed"
    conflict = invoke(system, "identity.checkpoint", dict(arguments, note="Changed"), key=key, context=fresh)
    assert conflict["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_fresh_call_generation_guards_are_not_silently_refreshed(system):
    service, binding = system
    with service.store.write() as tx:
        assigned = identity.assign_task(tx, binding["context"], "new task")
    stale = checkpoint(system)
    assert stale["error"]["code"] == "STALE_GENERATION"
    fresh = identity.context_from_token(service.store, binding["token"])
    assert fresh.connection_id == binding["context"].connection_id
    assert checkpoint(system, context=fresh)["ok"]
    explicitly_stale = invoke(system, "identity.checkpoint", {"state": "working", "note": "late"}, key=uid(),
                              context=fresh, expected_task_generation=binding["context"].task_generation)
    assert explicitly_stale["error"]["code"] == "STALE_GENERATION"
    assert assigned["current_task_generation"] == fresh.task_generation


def test_unknown_execution_guard_remains_unknown_after_reconnect(system):
    _, current = start(system, "new-native-run")
    assert current.execution_generation is not None
    retried = invoke(system, "identity.checkpoint", {"state": "working", "note": "old unknown invocation"},
                     key=uid(), context=current, expected_execution_generation=None)
    assert retried["error"]["code"] == "STALE_GENERATION"
    assert checkpoint(system, context=current)["ok"]


def test_same_retry_key_concurrently_commits_exactly_once(system):
    key = uid()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: checkpoint(system, key=key), range(4)))
    assert all(result["ok"] for result in results)
    assert sum(bool(result.get("replayed")) for result in results) == 3
    assert all(result["data"] == results[0]["data"] for result in results)
    with system[0].store.read() as tx:
        assert tx.connection.execute("SELECT COUNT(*) FROM idempotency WHERE retry_key=?", (key,)).fetchone()[0] == 1
        assert tx.connection.execute("SELECT COUNT(*) FROM events WHERE kind='checkpoint'").fetchone()[0] == 1


def test_actor_override_is_rejected_even_for_common_reads(system):
    service, binding = system
    other = identity.bind_native(service.store, {"harness": "codex", "native_session_id": uid()})
    forged = replace(binding["context"], actor_id=other["context"].actor_id)
    assert invoke(system, "operation.list", context=forged)["error"]["code"] == "UNBOUND_ACTOR"
    wrong_workspace = replace(binding["context"], workspace_id=uid())
    assert invoke(system, "identity.get", context=wrong_workspace)["error"]["code"] == "WRONG_WORKSPACE"


def test_shared_mcp_cannot_change_lifecycle_and_status_separates_observation(system):
    service, binding = system
    native = {"harness": "codex", "native_session_id": invoke(system, "identity.get")["data"]["actor"]["native_session_id"]}
    shared = identity.bind_native(service.store, native, transport="mcp")["context"]
    assert shared.identity_mode == "shared_group"
    assert checkpoint(system, context=shared)["error"]["code"] == "NOT_AUTHORIZED"
    event = invoke(system, "identity.event", {"state": "completed", "event": "stop"}, key=uid(), context=shared)
    assert event["error"]["code"] == "NOT_AUTHORIZED"
    assert checkpoint(system)["ok"]
    status = invoke(system, "identity.status", {"all": True, "limit": 100})
    assert status["ok"]
    actor = next(row for row in status["data"]["actors"] if row["id"] == binding["context"].actor_id)
    assert actor["reported_state"] == "working" and actor["observed_state"] == "unknown"
    assert actor["observed_age_us"] is None


def test_reconnect_never_creates_rewinds_or_reactivates_execution(system):
    service, _binding = system
    actor = invoke(system, "identity.get")["data"]["actor"]
    native = {"harness": "codex", "native_session_id": actor["native_session_id"], "native_run_id": "old"}
    early = identity.bind_native(service.store, native, transport="hook")
    assert early["context"].execution_generation is None
    old, _ = start(system, "old")
    new, current = start(system, "new")
    reconnected = identity.bind_native(service.store, native, transport="hook")
    assert reconnected["context"].execution_generation == current.execution_generation == new["execution_generation"]
    late = invoke(system, "identity.event", {"state": "completed", "event": "stop", "native_run_id": "old"},
                  key=uid(), context=reconnected["context"])
    assert late["ok"] and late["data"]["reason"] == "stale_generation"
    with service.store.write() as tx:
        tx.connection.execute("UPDATE executions SET state='ended',ended_us=? WHERE generation=?", (tx.now_us, current.execution_generation))
    ended = invoke(system, "identity.event", {"state": "working", "event": "start", "native_run_id": "new"},
                   key=uid(), context=reconnected["context"])
    assert ended["data"]["reason"] == "ended_run"
    assert old["execution_generation"] != new["execution_generation"]


def test_unknown_stop_is_only_observation_and_explicit_pause_stays_paused(system):
    service, _binding = system
    _, current = start(system, "native-run")
    assert checkpoint(system, "paused", context=current)["ok"]
    unknown = invoke(system, "identity.event", {"state": "completed", "event": "stop"}, key=uid(), context=current)
    assert unknown["data"] == {"applied": False, "reason": "ambiguous_generation"}
    late_work = invoke(system, "identity.event", {"state": "working", "event": "start", "execution_generation": current.execution_generation}, key=uid(), context=current)
    assert late_work["data"]["reason"] == "explicit_resume_required"
    with service.store.read() as tx:
        assert identity.actor_for_context(tx, current)["reported_state"] == "paused"


def test_verified_native_start_uses_origin_process_proof_but_stop_does_not_guess(system):
    service, _ = system
    proof = identity.process_identity(os.getpid())
    assert proof and identity.process_status(proof) == "alive"
    bound = identity.bind_native(service.store, {"harness": "claude", "native_session_id": uid(), "process_identity": proof}, transport="hook")
    started = invoke(system, "identity.event", {"event": "start", "state": "working"}, key=uid(), context=bound["context"])
    assert started["ok"] and started["data"]["applied"]
    assert started["next_context"]["execution_generation"]
    stopped = invoke(system, "identity.event", {"event": "stop", "state": "completed"}, key=uid(), context=bound["context"])
    assert stopped["data"]["reason"] == "ambiguous_generation"


def test_draining_allows_exact_reconnect_but_no_registration_or_run_start(system):
    service, binding = system
    native = invoke(system, "identity.get")["data"]["actor"]
    with service.store.write() as tx:
        tx.connection.execute("UPDATE meta SET value_json='\"draining\"' WHERE key='service_state'")
    reconnected = identity.bind_native(service.store, {"harness": "codex", "native_session_id": native["native_session_id"]})
    assert invoke(system, "identity.get", context=reconnected["context"])["ok"]
    with pytest.raises(CoordinationError) as failure:
        identity.bind_native(service.store, {"harness": "codex", "native_session_id": uid()})
    assert failure.value.code == "AUTHORITY_FENCED"
    with pytest.raises(CoordinationError) as failure, service.store.write() as tx:
        identity.start_execution(tx, binding["context"], native_run_id="new")
    assert failure.value.code == "AUTHORITY_FENCED"


def test_imported_actor_stays_historical_until_authentic_bind(system):
    service, _ = system
    actor_id, generation, native_id = uid(), uid(), uid()
    with service.store.write() as tx:
        identity.insert_imported_actor(tx, {"id": actor_id, "harness": "cursor", "native_session_id": native_id,
            "label": "Imported", "current_task_generation": generation, "task": "Retained", "reported_state": "completed"})
        assert tx.connection.execute("SELECT archived FROM actors WHERE id=?", (actor_id,)).fetchone()[0] == 1
    binding = identity.bind_native(service.store, {"harness": "cursor", "native_session_id": native_id})
    assert binding["context"].actor_id == actor_id
    with service.store.read() as tx:
        actor = identity.actor_for_context(tx, binding["context"])
        assert not actor["archived"] and actor["reported_state"] == "completed" and actor["task"] == "Retained"
        assert not tx.connection.execute("SELECT 1 FROM presence WHERE actor_id=?", (actor_id,)).fetchall()


def test_parent_route_requires_exact_native_parent_and_captured_assignments(system):
    service, binding = system
    parent = invoke(system, "identity.get")["data"]["actor"]
    child = identity.bind_native(service.store, {"harness": "codex", "native_session_id": parent["native_session_id"],
        "child_id": "child-one", "parent_id": parent["id"], "source": "native_child_start"})
    with service.store.write() as tx:
        assert identity.authorized_parent(tx, child["context"].actor_id, child["context"].task_generation)["id"] == parent["id"]
        identity.assign_task(tx, binding["context"], "next parent task")
        assert identity.authorized_parent(tx, child["context"].actor_id, child["context"].task_generation) is None
    other = identity.bind_native(service.store, {"harness": "codex", "native_session_id": uid()})
    with pytest.raises(CoordinationError) as failure:
        identity.bind_native(service.store, {"harness": "codex", "native_session_id": parent["native_session_id"],
            "child_id": "child-one", "parent_id": other["context"].actor_id, "source": "native_child_start"})
    assert failure.value.code == "NOT_AUTHORIZED"


def test_stale_queued_execution_gets_failed_receipt_and_never_runs(system):
    service, binding = system
    called = []
    service.adapters["slow_handlers"] = {"example.execute": lambda *_: called.append(True)}
    operation_id = queue(service, binding["context"])
    start(system, "new-native-run")
    result = service.run_operation(operation_id)
    assert result["state"] == "failed" and result["error"]["code"] == "STALE_GENERATION"
    assert called == []
    assert service.run_operation(operation_id)["state"] == "failed"


def test_slow_handler_runs_outside_writer_and_effect_fence_rechecks_authority(system):
    service, binding = system

    def execute(actual, operation):
        with actual.store.write() as tx:
            actual.require_effect(tx, operation)
            tx.connection.execute("UPDATE meta SET value_json='\"draining\"' WHERE key='service_state'")
        with actual.store.write() as tx:
            actual.require_effect(tx, operation)
        pytest.fail("draining authority admitted another effect")

    service.adapters["slow_handlers"] = {"example.execute": execute}
    operation_id = queue(service, binding["context"])
    result = service.run_operation(operation_id)
    assert result["state"] == "failed" and result["error"]["code"] == "AUTHORITY_FENCED"
    with service.store.write() as tx:
        tx.connection.execute("UPDATE meta SET value_json='\"active\"' WHERE key='service_state'")


@pytest.mark.parametrize("effect", [False, True])
def test_worker_crash_after_effect_intent_is_uncertain_and_never_reexecuted(system, effect):
    service, binding = system
    calls = []

    def execute(actual, operation):
        calls.append(operation["id"])
        if effect:
            with actual.store.write() as tx:
                tx.connection.execute("UPDATE operations SET effect_started_us=? WHERE id=?", (tx.now_us, operation["id"]))
        raise RuntimeError("private contents must not appear in receipt")

    service.adapters["slow_handlers"] = {"example.execute": execute}
    operation_id = queue(service, binding["context"])
    result = service.run_operation(operation_id)
    assert result["state"] == ("uncertain" if effect else "failed")
    assert "private contents" not in canonical_json(result["error"])
    assert service.run_operation(operation_id)["state"] == result["state"]
    assert calls == [operation_id]


def test_receipt_size_failure_is_atomic_and_terminal_read_has_no_claim_secret(system):
    service, binding = system
    service.config = Config(frame_bytes=4096, action_bytes=1024)
    service.adapters["slow_handlers"] = {"example.execute": lambda *_: {"body": "x" * 8192}}
    operation_id = queue(service, binding["context"])
    result = service.run_operation(operation_id)
    assert result["state"] == "failed" and result["error"]["code"] == "INVALID_ARGUMENT"
    receipt = invoke(system, "operation.get", {"operation_id": operation_id})
    assert receipt["ok"] and "claim_token" not in receipt["data"]
    assert "arguments_json" not in receipt["data"]


def test_configured_request_budget_rejects_before_mutation(system):
    service, _ = system
    service.config = Config(frame_bytes=4096, action_bytes=1024)
    result = checkpoint(system, note="x" * 4000)
    assert result["error"]["code"] == "INVALID_ARGUMENT"
    with service.store.read() as tx:
        assert tx.connection.execute("SELECT COUNT(*) FROM idempotency").fetchone()[0] == 0


def test_failure_ack_is_versioned_preserves_history_and_never_settles_uncertainty(system):
    service, binding = system
    service.adapters["slow_handlers"] = {"example.execute": lambda *_: (_ for _ in ()).throw(RuntimeError())}
    failed_id = queue(service, binding["context"], arguments={"paths": ["src/file.py"]})
    failed = service.run_operation(failed_id)
    with service.store.read() as tx:
        assert core.count_pending(tx, binding["context"], filters={"task": "foundation", "path": "src"}) == {"operations": 1}
        assert core.select_actions(tx, binding["context"], filters={"path": "elsewhere"}) == []
        action = core.select_actions(tx, binding["context"], limit=1)[0]
        assert action["version"] == failed["sequence"]
    stale = invoke(system, "operation.ack", {"operation_id": failed_id, "version": failed["sequence"] + 1}, key=uid())
    assert stale["error"]["code"] == "STALE_VERSION"
    ack = invoke(system, "operation.ack", {"operation_id": failed_id, "version": failed["sequence"]}, key=uid())
    assert ack["ok"] and ack["data"]["acknowledged"]
    with service.store.read() as tx:
        assert core.count_pending(tx, binding["context"]) == {"operations": 0}
        assert tx.connection.execute("SELECT state FROM operations WHERE id=?", (failed_id,)).fetchone()[0] == "failed"
        assert tx.connection.execute("SELECT COUNT(*) FROM events WHERE record_id=? AND kind='acknowledged'", (failed_id,)).fetchone()[0] == 1
    uncertain_id = queue(service, binding["context"])
    with service.store.write() as tx:
        uncertain = service.finish_operation(tx, uncertain_id, "uncertain", error={"code": "UNKNOWN"})
    rejected = invoke(system, "operation.ack", {"operation_id": uncertain_id, "version": uncertain["sequence"]}, key=uid())
    assert rejected["error"]["code"] == "RECONCILIATION_REQUIRED"


def test_presented_failure_filter_applies_before_page_limit(system):
    service, binding = system
    service.adapters["slow_handlers"] = {"example.execute": lambda *_: None}
    ids = [queue(service, binding["context"]) for _ in range(3)]
    with service.store.write() as tx:
        rows = [service.finish_operation(tx, operation_id, "failed") for operation_id in ids]
        for row in rows[:2]:
            tx.connection.execute("INSERT INTO action_presentations VALUES (?,?,?,?,?)", (binding["context"].actor_id,
                "operation_failure", row["id"], row["sequence"], tx.now_us))
        assert [a["id"] for a in core.select_actions(tx, binding["context"], new_only=True, limit=1)] == [ids[2]]
        assert core.count_pending(tx, binding["context"], new_only=True) == {"operations": 1}
        operator = Context(service.workspace.id, None, transport="operator", operator=True, identity_mode="operator")
        assert core.count_pending(tx, operator) == {"operations": 3}


def test_wake_occurs_after_commit_once_and_terminal_read_is_meaningful_boundary(system):
    service, _binding = system
    wakes, digests = [], []
    service.adapters.update(slow_handlers={"commit.execute": lambda *_: {"commit": "saved"}},
                            work_available=lambda: wakes.append(True), boundary_digest=lambda context: digests.append(context) or {"items": []})

    def enqueue(actual, context, arguments, tx):
        return actual.enqueue(tx, context, "commit.execute", arguments, key="accepted")

    service.operations["example.enqueue"] = Operation("example.enqueue", enqueue, True, True)
    key = uid()
    accepted = invoke(system, "example.enqueue", key=key)
    assert accepted["ok"] and wakes == [True]
    assert invoke(system, "example.enqueue", key=key)["replayed"] and wakes == [True]
    operation_id = accepted["data"]["operation_id"]
    assert invoke(system, "operation.get", {"operation_id": operation_id})["action_digest"] is None
    service.run_operation(operation_id)
    terminal = invoke(system, "operation.get", {"operation_id": operation_id})
    assert terminal["action_digest"] == {"items": []} and len(digests) == 1


def test_workspace_discovery_is_explicit_private_and_chooses_nearest_registered_root(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTCOORD_WORKSPACE", raising=False)
    home = tmp_path / "state"
    outer = tmp_path / "workspace"
    nested = outer / "nested"
    nested.mkdir(parents=True)
    before = tmp_path / "absent-state"
    with pytest.raises(CoordinationError):
        discover_workspace(outer, state_root=before)
    assert not before.exists()
    parent = register_workspace(outer, state_root=home)
    child = register_workspace(nested, state_root=home)
    assert discover_workspace(nested / "deep", state_root=home).id == child.id
    assert discover_workspace(explicit_root=outer, state_root=home).id == parent.id
    assert register_workspace(outer, state_root=home).id == parent.id
    assert parent.socket_path != child.socket_path and len(os.fsencode(parent.socket_path)) <= 103
    assert (home / "registry.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(CoordinationError) as failure:
        register_workspace(outer, state_root=home, workspace_id=uid())
    assert failure.value.code == "WRONG_WORKSPACE"


def test_registry_corruption_and_implicit_relocation_fail_explicitly(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    home = tmp_path / "state"
    workspace = register_workspace(root, state_root=home)
    moved = tmp_path / "moved"
    root.rename(moved)
    with pytest.raises(CoordinationError) as failure:
        workspace_from_id(workspace.id, state_root=home)
    assert failure.value.code == "WRONG_WORKSPACE"
    assert register_workspace(moved, state_root=home, workspace_id=workspace.id, relocate=True).id == workspace.id
    (home / "registry.json").write_text('{"version":1,"workspaces":{"bad":{"root":3}}}')
    with pytest.raises(CoordinationError):
        discover_workspace(moved, state_root=home)


@pytest.mark.parametrize("contents", ["[limits]\nfast_workers = true\n", "[limits]\naction_bytes=8192\nframe_bytes=4096\n", "[unknown]\na=1\n", "[version]\npath='../escape'\nmatch='bad'\nreplacement='bad'\n"])
def test_required_configuration_errors_are_not_defaults(tmp_path, contents):
    root = tmp_path / "workspace"
    root.mkdir()
    workspace = register_workspace(root, state_root=tmp_path / "state")
    (root / ".agentcoord.toml").write_text(contents)
    with pytest.raises(CoordinationError) as failure:
        load_config(workspace)
    assert failure.value.code == "INVALID_ARGUMENT"


def test_native_ancestor_requires_real_harness_image_and_never_pane_identity(monkeypatch):
    proof = {"pid": 123, "pgid": 123, "started": "123.000000", "source": "darwin-libproc"}
    monkeypatch.setattr(identity.os, "getppid", lambda: 123)
    monkeypatch.setattr(identity, "_process_info", lambda pid: {**proof, "ppid": 1, "name": "agent", "executable": "/unrelated/agent"})
    with pytest.raises(CoordinationError):
        identity.native_context(None, {"HERDR_PANE_ID": "a-different-pane"})
    monkeypatch.setattr(identity, "_process_info", lambda pid: {**proof, "ppid": 1, "name": "agent", "executable": "/opt/cursor-agent/versions/2026.10.04/agent"})
    captured = identity.native_context(None, {})
    assert captured["harness"] == "cursor" and captured["process_identity"] == proof
    assert "native_session_id" not in captured
    monkeypatch.setattr(identity, "_process_info", lambda pid: {**proof, "ppid": 1, "name": "grok-1.0.46-macos-aarch64", "executable": "/opt/grok-1.0.46-macos-aarch64"})
    assert identity.native_context("grok", {"GROK_SESSION_ID": "actual"})["process_identity"] == proof
    monkeypatch.setattr(identity, "_process_info", lambda pid: {**proof, "ppid": 1, "name": "codex", "executable": "/opt/codex"})
    assert identity.native_context("codex", {"CODEX_THREAD_ID": "correct", "HERDR_PANE_ID": "wrong"})["native_session_id"] == "correct"
    with pytest.raises(CoordinationError):
        identity.native_context("claude", {"CODEX_THREAD_ID": "foreign"})


@pytest.mark.parametrize("process_state", ["gone", "unknown"])
@pytest.mark.parametrize("failure,expected", [
    (None, "alive"),
    (ProcessLookupError(), "gone"),
    (PermissionError(), "unknown"),
    (OSError(), "unknown"),
])
def test_group_probe_preserves_kernel_outcome(monkeypatch, process_state, failure, expected):
    proof = {"pid": 123, "pgid": 123, "started": "123.000000", "source": "darwin-libproc"}
    monkeypatch.setattr(identity, "process_status", lambda _: process_state)
    calls = []

    def probe(pgid, signal):
        calls.append((pgid, signal))
        if failure is not None:
            raise failure

    monkeypatch.setattr(identity.os, "killpg", probe)
    assert identity.group_status(proof) == expected
    assert calls == [(123, 0)]


def test_invalid_group_identity_never_probes_kernel(monkeypatch):
    monkeypatch.setattr(identity, "process_status", lambda _: "unknown")
    monkeypatch.setattr(identity.os, "killpg", lambda *_: pytest.fail("Invalid group must not be probed"))
    proof = {"pid": 123, "pgid": 0, "started": "123.000000", "source": "darwin-libproc"}
    assert identity.group_status(proof) == "unknown"


def test_group_probe_observes_owned_native_group():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"], start_new_session=True)
    try:
        proof = identity.process_identity(child.pid)
        assert proof is not None and proof["pgid"] == child.pid
        assert identity.group_status(proof) == "alive"
    finally:
        child.terminate()
        child.wait(timeout=5)
    assert identity.group_status(proof) == "gone"


@pytest.mark.parametrize("reader", ["readiness", "patch", "configuration", "doctor"])
def test_required_regular_file_readers_reject_fifo_without_writer(tmp_path, reader):
    import threading

    from agentcoord import commits, doctor, readiness

    path = tmp_path / (".agentcoord.toml" if reader == "configuration" else "input")
    os.mkfifo(path)
    result = {}

    def read():
        try:
            if reader == "readiness":
                result["value"] = readiness.prepare_hashes(tmp_path, [path.name])
            elif reader == "patch":
                result["value"] = commits._read_patch(tmp_path, {
                    "patch_file": path.name, "patch_sha256": "0" * 64})
            elif reader == "configuration":
                result["value"] = load_config(SimpleNamespace(root=tmp_path))
            else:
                result["value"] = doctor._configuration(path)
        except Exception as error:  # noqa: BLE001 - preserve any spawned reader failure for the main test thread.
            result["error"] = error

    worker = threading.Thread(target=read, daemon=True)
    worker.start()
    worker.join(1)
    completed_without_writer = not worker.is_alive()
    if not completed_without_writer:
        # Release the defective blocking open so a failed regression does not
        # strand a worker or prevent the test process from exiting.
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
        except OSError:
            pass
        else:
            os.close(descriptor)
        worker.join(2)
    assert completed_without_writer, f"{reader} blocked opening a FIFO without a writer"
    if reader == "doctor":
        assert result == {"value": (None, "invalid")}
    else:
        assert isinstance(result.get("error"), CoordinationError), result
        assert result["error"].code == "INVALID_ARGUMENT"
