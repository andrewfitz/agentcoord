"""Actual assembled service: delivery, lifecycle, slow work and offline ownership."""

from __future__ import annotations

import json
import threading
import time
import uuid
from contextlib import contextmanager

import pytest

from agentcoord.application import (
    backup_workspace,
    build_service,
    make_server,
    open_offline_store,
    restore_workspace,
)
from agentcoord.config import register_workspace
from agentcoord.core import CoordinationError
from agentcoord.transport import Client


@pytest.fixture
def service(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    return build_service(register_workspace(root, state_root=tmp_path / "state"))


@contextmanager
def running(service):
    with make_server(service) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join(5)
            assert not thread.is_alive()


def client(service, native="sender", harness="codex", operator=False):
    return Client(
        service.workspace.socket_path,
        None if operator else {"harness": harness, "native_session_id": native},
        workspace_id=service.workspace.id,
        operator=operator,
    )


def call(connection, operation, arguments=None):
    result = connection.call(
        operation, arguments or {}, key=None if operation.endswith(".get") else str(uuid.uuid4())
    )
    assert result["ok"], result
    return result["data"]


def terminal(connection, operation_id):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        result = call(connection, "operation.get", {"operation_id": operation_id})
        if result["state"] not in {"queued", "running"}:
            return result
        time.sleep(0.02)
    pytest.fail("Real daemon did not complete its queued operation")


def test_cross_harness_delivery_decision_coalescing_and_handling(service):
    with running(service):  # noqa: SIM117 — distinguish daemon lifetime from client connections.
        with client(service) as sender, client(service, "recipient", "claude") as recipient:
            actor = call(recipient, "identity.get")["actor"]["id"]
            message = call(
                sender,
                "message.send",
                {
                    "recipients": [actor],
                    "kind": "note",
                    "subject": "Preserve edit",
                    "body": "Intentional repair of the shared parser.",
                },
            )
            decision = call(
                sender,
                "decision.request",
                {
                    "recipient": actor,
                    "subject": "Shared API",
                    "body": "Can the consumer accept the reviewed contract?",
                    "paths": ["src/api"],
                },
            )
            view = call(recipient, "message.sync")
            assert len(view["items"]) == 2, view
            assert sum(view["counts"].values()) == 2, view
            assert len({(item["kind"], item["id"]) for item in view["items"]}) == 2
            assert call(recipient, "message.sync", {"new_only": True})["items"] == []
            call(recipient, "message.consume", {"id": message["id"]})
            # Presentation or consuming another message cannot answer a decision.
            assert call(recipient, "message.sync")["total"] == 1
            assert call(recipient, "decision.get", {"id": decision["id"]})["state"] == "open"
            assert call(sender, "message.sync")["total"] == 1


def test_slow_readiness_runs_and_clean_restart_retains_actor_and_history(service):
    (service.workspace.root / "contract.txt").write_text("contract v1")
    with running(service), client(service) as sender:
        actor = call(sender, "identity.get")["actor"]["id"]
        queued = call(
            sender,
            "readiness.publish",
            {
                "artifact": "contract",
                "paths": ["contract.txt"],
                "evidence": {"check": "fixture"},
            },
        )
        result = terminal(sender, queued["operation_id"])
        assert result["state"] == "succeeded", result
    with service.store.read() as tx:
        assert tx.connection.execute("SELECT COUNT(*) FROM bindings").fetchone()[0] == 0
    with running(service):  # noqa: SIM117 — this is a separate daemon restart.
        with client(service) as sender, client(service, operator=True) as operator:
            assert call(sender, "identity.get")["actor"]["id"] == actor
            assert terminal(sender, queued["operation_id"])["state"] == "succeeded"
            assert operator.health()["data"]["service_state"] == "active"
            assert operator.health()["data"]["maintenance_error"] is None


def test_explicit_drain_survives_restart_and_allows_exact_outcome_reconnect(service):
    with running(service):
        with client(service) as sender:
            actor = call(sender, "identity.get")["actor"]["id"]
        with client(service, operator=True) as operator:
            assert operator.drain()["ok"]
        with client(service) as sender:
            assert call(sender, "identity.get")["actor"]["id"] == actor
            denied = sender.call("message.sync", {}, key="drained")
            assert denied["error"]["code"] == "AUTHORITY_FENCED"
        with pytest.raises(CoordinationError) as error:
            client(service, "new-during-drain").connect()
        assert error.value.code == "AUTHORITY_FENCED"
    with running(service):
        with client(service, operator=True) as operator:
            assert operator.health()["data"]["service_state"] == "quiescent"
            assert operator.activate()["ok"]
        with client(service, "new-after-activation") as sender:
            assert call(sender, "identity.get")["actor"]["id"] != actor


def test_offline_operations_refuse_live_service_and_restore_retained_writes(service, tmp_path):
    backup = tmp_path / "backups" / "initial.sqlite3"
    assert backup_workspace(service.workspace, backup)["verified"]
    assert restore_workspace(service.workspace, backup)["restored"]
    with running(service):
        with pytest.raises(CoordinationError) as error:  # noqa: SIM117 — failure must occur while entering offline ownership.
            with open_offline_store(service.workspace):
                pass
        assert error.value.code == "SERVICE_BUSY"
        with client(service) as sender:
            call(sender, "identity.get")
    with pytest.raises(CoordinationError) as error:
        restore_workspace(service.workspace, backup)
    assert error.value.code == "RECONCILIATION_REQUIRED"


def test_unknown_external_operation_after_restart_is_not_reexecuted(service):
    from agentcoord import identity

    binding = identity.bind_native(
        service.store, {"harness": "codex", "native_session_id": "lost-worker"}
    )
    with service.store.write() as tx:
        receipt = service.enqueue(
            tx,
            binding["context"],
            "readiness.publish",
            {"artifact": "missing", "paths": ["missing"]},
            key="original",
        )
        tx.connection.execute(
            "UPDATE operations SET state='running',claim_token='lost',effect_started_us=? WHERE id=?",
            (tx.now_us, receipt["operation_id"]),
        )
    with running(service), client(service, "lost-worker") as sender:
        result = terminal(sender, receipt["operation_id"])
        assert result["state"] == "uncertain"
        assert result["error"]["code"] == "RECONCILIATION_REQUIRED"
        assert call(sender, "message.sync")["counts"]["operations"] == 1
    with service.store.read() as tx:
        assert (
            json.loads(
                tx.connection.execute(
                    "SELECT arguments_json FROM operations WHERE id=?", (receipt["operation_id"],)
                ).fetchone()[0]
            )["artifact"]
            == "missing"
        )


def test_incomplete_import_cannot_activate(service):
    with service.store.write(maintenance=True) as tx:
        tx.connection.execute(
            "INSERT INTO import_runs(id,source_manifest_sha256,state,source_metadata_json,created_us) VALUES ('partial','hash','applying','{}',?)",
            (tx.now_us,),
        )
        tx.connection.execute(
            "UPDATE meta SET value_json='\"importing\"' WHERE key='service_state'"
        )
    with running(service), client(service, operator=True) as operator:
        result = operator.activate()
        assert not result["ok"] and result["error"]["code"] == "RECONCILIATION_REQUIRED"
        assert operator.health()["data"]["service_state"] == "quiescent"


def test_interrupted_atomic_readiness_is_failed_and_inspectable(service):
    from agentcoord import identity

    binding = identity.bind_native(
        service.store, {"harness": "codex", "native_session_id": "hash-worker"}
    )
    with service.store.write() as tx:
        receipt = service.enqueue(
            tx,
            binding["context"],
            "readiness.publish",
            {"artifact": "contract", "paths": ["contract"]},
            key="original",
        )
        tx.connection.execute(
            "UPDATE operations SET state='running',claim_token='lost' WHERE id=?",
            (receipt["operation_id"],),
        )
    with running(service), client(service, "hash-worker") as sender:
        result = terminal(sender, receipt["operation_id"])
        assert result["state"] == "failed"
        assert result["error"]["code"] == "OPERATION_FAILED"
        call(sender, "operation.ack", {"operation_id": result["id"], "version": result["sequence"]})
        assert call(sender, "message.sync")["total"] == 0
        assert terminal(sender, result["id"])["state"] == "failed"


def test_completed_offline_scope_cleanup_preserves_history_and_live_binding(service, monkeypatch):
    from agentcoord import application, identity
    from agentcoord.core import Call
    owner = identity.bind_native(service.store, {'harness': 'codex', 'native_session_id': 'retired-owner', 'task': 'parser-repair'})['context']
    result = service.execute(owner, Call('work.activity', {'note': 'Intentional repair',
        'paths': ['src/parser.py'], 'state': 'completed'}, 'completed-scope'))
    assert result['ok'], result
    monkeypatch.setattr(identity, 'process_status', lambda *_: 'gone')
    # A missing proof remains unknown, so supply a fixture observation explicitly.
    with service.store.write() as tx:
        tx.connection.execute("UPDATE actors SET process_identity_json='{}' WHERE id=?", (owner.actor_id,))
    application._presence_batch(service, '')
    with service.store.read() as tx:
        assert not tx.connection.execute('SELECT archived FROM actors WHERE id=?', (owner.actor_id,)).fetchone()[0]
    with service.store.write() as tx:
        tx.connection.execute('DELETE FROM bindings WHERE actor_id=?', (owner.actor_id,))
    application._presence_batch(service, '')
    with service.store.read() as tx:
        assert tx.connection.execute('SELECT archived FROM actors WHERE id=?', (owner.actor_id,)).fetchone()[0]
        assert not tx.connection.execute('SELECT 1 FROM current_activity WHERE actor_id=?', (owner.actor_id,)).fetchone()
        assert tx.connection.execute('SELECT 1 FROM activities WHERE id=?', (result['data']['activity']['id'],)).fetchone()


@pytest.mark.parametrize('observation', ['gone', 'unknown'])
def test_abandoned_working_session_retirement_requires_verified_absence(service, monkeypatch, observation):
    from agentcoord import application, identity
    from agentcoord.core import Call
    owner = identity.bind_native(service.store, {'harness': 'claude', 'native_session_id': 'abandoned-owner',
                                                'task': 'interrupted-task'})['context']
    result = service.execute(owner, Call('work.activity', {'note': 'Unfinished repair',
        'paths': ['src/parser.py']}, 'unfinished-scope'))
    assert result['ok'], result
    monkeypatch.setattr(identity, 'process_status', lambda *_: observation)
    with service.store.write() as tx:
        tx.connection.execute("UPDATE actors SET process_identity_json='{}' WHERE id=?", (owner.actor_id,))
        tx.connection.execute('DELETE FROM bindings WHERE actor_id=?', (owner.actor_id,))
    application._presence_batch(service, '')
    with service.store.read() as tx:
        actor = tx.connection.execute('SELECT archived,reported_state FROM actors WHERE id=?', (owner.actor_id,)).fetchone()
        assert bool(actor['archived']) == (observation == 'gone')
        assert actor['reported_state'] == 'working'
        assert tx.connection.execute('SELECT 1 FROM activities WHERE id=?', (result['data']['activity']['id'],)).fetchone()


@pytest.mark.parametrize('protection', ['job', 'operation', 'commit'])
def test_offline_retirement_preserves_pending_execution_authority(service, monkeypatch, protection):
    from agentcoord import application, identity
    from agentcoord.core import Call
    owner = identity.bind_native(service.store, {'harness': 'codex', 'native_session_id': 'protected-owner',
                                                'task': 'protected-task'})['context']
    if protection == 'job':
        queued = service.execute(owner, Call('job.schedule', {'kind': 'reminder',
            'due_us': time.time_ns() // 1000 + 3600_000000, 'note': 'Keep scheduled work'}, 'protected-job'))
        assert queued['ok'], queued
    elif protection == 'operation':
        (service.workspace.root / 'fixture.txt').write_text('pending input')
        queued = service.execute(owner, Call('readiness.publish', {'artifact': 'fixture',
            'paths': ['fixture.txt'], 'evidence': 'Await calculation'}, 'protected-operation'))
        assert queued['ok'], queued
    with service.store.write() as tx:
        if protection == 'commit':
            admission, grant = str(uuid.uuid4()), str(uuid.uuid4())
            tx.connection.execute("INSERT INTO commit_admissions VALUES (?,?,?,'manual','granted',?,?,?)",
                (admission, owner.actor_id, owner.task_generation, tx.now_us, tx.now_us + 60_000000, grant))
            tx.connection.execute("INSERT INTO commit_grants VALUES (?,?,?,'active',?,NULL)",
                (grant, admission, '{}', tx.now_us))
        tx.connection.execute("UPDATE actors SET process_identity_json='{}' WHERE id=?", (owner.actor_id,))
        tx.connection.execute('DELETE FROM bindings WHERE actor_id=?', (owner.actor_id,))
    monkeypatch.setattr(identity, 'process_status', lambda *_: 'gone')
    application._presence_batch(service, '')
    with service.store.read() as tx:
        assert not tx.connection.execute('SELECT archived FROM actors WHERE id=?', (owner.actor_id,)).fetchone()[0]


def test_child_presence_requires_independent_process_or_correlated_end(service, monkeypatch):
    import os

    from agentcoord import application, identity
    from agentcoord.core import Call
    proof = identity.process_identity(os.getpid())
    native = {'harness': 'claude', 'native_session_id': 'shared-parent', 'process_identity': proof}
    parent = identity.bind_native(service.store, native, transport='hook')
    child = identity.bind_native(service.store, {**native, 'child_id': 'actual-child'}, transport='hook')
    independent = identity.bind_native(service.store, {**native, 'child_id': 'independent-child',
        'process_identity': {**proof, 'pid': proof['pid'] + 1}}, transport='hook')
    for binding in (parent, child, independent):
        result = service.execute(binding['context'], Call('identity.event', {'event': 'start', 'state': 'working'}, str(uuid.uuid4())))
        assert result['ok'], result
    monkeypatch.setattr(identity, 'process_status', lambda *_: 'alive')
    application._presence_batch(service, '')
    with service.store.read() as tx:
        states = {r['actor_id']: (r['observed_state'], r['confidence']) for r in tx.connection.execute('SELECT * FROM presence')}
    assert states[parent['context'].actor_id] == ('running', 'verified')
    assert states[child['context'].actor_id] == ('unknown', 'unknown')
    assert states[independent['context'].actor_id] == ('running', 'verified')
    current = identity.context_from_token(service.store, child['token'])
    ended = service.execute(current, Call('identity.event', {'event': 'child_stop', 'state': 'completed'}, str(uuid.uuid4())))
    assert ended['ok'] and ended['data']['applied'], ended
    application._presence_batch(service, '')
    with service.store.read() as tx:
        observed = tx.connection.execute('SELECT observed_state,confidence FROM presence WHERE actor_id=?', (current.actor_id,)).fetchone()
        assert tuple(observed) == ('offline', 'verified')


def test_parent_registration_during_presence_observation_cannot_mark_child_running(service, monkeypatch):
    import os

    from agentcoord import application, identity
    proof = identity.process_identity(os.getpid())
    native = {'harness': 'claude', 'native_session_id': 'registration-race', 'process_identity': proof}
    child = identity.bind_native(service.store, {**native, 'child_id': 'child'}, transport='hook')
    def register_parent(_):
        identity.bind_native(service.store, native, transport='hook')
        return 'alive'
    monkeypatch.setattr(identity, 'process_status', register_parent)
    application._presence_batch(service, '')
    with service.store.read() as tx:
        assert tx.connection.execute('SELECT 1 FROM presence WHERE actor_id=?', (child['context'].actor_id,)).fetchone() is None
    application._presence_batch(service, '')
    with service.store.read() as tx:
        assert tx.connection.execute('SELECT observed_state FROM presence WHERE actor_id=?', (child['context'].actor_id,)).fetchone()[0] == 'unknown'
