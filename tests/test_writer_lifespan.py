"""Daemon writer reuse preserves rollback, file authority and owned cleanup."""

from __future__ import annotations

import json
import os
import select
import signal
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest

from agentcoord import application, identity
from agentcoord.config import register_workspace
from agentcoord.core import CoordinationError
from agentcoord.store import Store
from agentcoord.transport import Client, ownership_lock


def uid():
    return str(uuid.uuid4())


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "state" / "runtime.sqlite3", uid())
    value.initialize((identity.SCHEMA,))
    return value


@contextmanager
def owned_writer(store):
    with ownership_lock(store.path.parent / "service.lock"), store.writer_lifespan():
        yield


def write_event(store, kind):
    with store.write() as tx:
        assert tx.connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert tx.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        tx.event("lifespan", kind, uid(), None)
        return tx.connection


def events(store):
    with store.read() as tx:
        return [row[0] for row in tx.connection.execute("SELECT kind FROM events WHERE domain='lifespan' ORDER BY sequence")]


def test_failed_transaction_rolls_back_and_next_thread_reuses_healthy_writer(store):
    with owned_writer(store):
        connection = write_event(store, "before")
        with pytest.raises(RuntimeError, match="abort"), store.write() as tx:
            assert tx.connection is connection
            tx.event("lifespan", "must-rollback", uid(), None)
            raise RuntimeError("abort")
        assert not connection.in_transaction
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(write_event, store, "after").result(timeout=5) is connection
        assert events(store) == ["before", "after"]
    assert store._writer_connection is None and store._writer_pid is None
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")


def test_sqlite_failure_closes_bad_handle_then_next_thread_writes(store):
    with owned_writer(store):
        connection = write_event(store, "before")
        with pytest.raises(sqlite3.OperationalError), store.write() as tx:
            tx.event("lifespan", "must-rollback", uid(), None)
            tx.connection.execute("SELECT * FROM missing_lifespan_table")
        assert store._writer_connection is None
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")
        with ThreadPoolExecutor(max_workers=1) as pool:
            replacement = pool.submit(write_event, store, "after").result(timeout=5)
        assert replacement is not connection
        assert events(store) == ["before", "after"]


def test_rollback_failure_closes_actual_handle_and_next_writer_opens_fresh(store):
    class RollbackFailure:
        def __init__(self, connection):
            self.connection, self.closed = connection, False

        def execute(self, *arguments):
            return self.connection.execute(*arguments)

        def rollback(self):
            raise sqlite3.OperationalError("injected rollback failure")

        def close(self):
            self.connection.close()
            self.closed = True

    with owned_writer(store):
        connection = write_event(store, "before")
        fault = RollbackFailure(connection)
        store._writer_connection = fault
        with pytest.raises(sqlite3.OperationalError, match="injected rollback failure"), store.write() as tx:
            tx.event("lifespan", "must-rollback", uid(), None)
            raise RuntimeError("original transaction failure")
        assert fault.closed and store._writer_connection is None
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")
        with ThreadPoolExecutor(max_workers=1) as pool:
            replacement = pool.submit(write_event, store, "after").result(timeout=5)
        assert replacement is not connection
        assert events(store) == ["before", "after"]


@pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX process identity boundary")
def test_forked_child_is_fenced_before_inherited_gate_and_parent_keeps_writing(store):
    with owned_writer(store):
        write_event(store, "before")
        with store.write() as tx:
            receive, send = os.pipe()
            with store._writer._mutex:
                child = os.fork()
                if child == 0:
                    os.close(receive)
                    try:
                        with store.write():
                            os._exit(2)
                    except CoordinationError as error:
                        os.write(send, error.code.encode("ascii"))
                        os._exit(0)
                    except BaseException:  # noqa: BLE001 - child must exit without unwinding inherited daemon resources.
                        os._exit(3)
            os.close(send)
            reaped, status = 0, None
            try:
                readable, _, _ = select.select([receive], [], [], 5)
                assert readable, "Child blocked on the inherited writer gate"
                assert os.read(receive, 128) == b"AUTHORITY_FENCED"
            finally:
                os.close(receive)
                deadline = time.monotonic() + 5
                while not reaped and time.monotonic() < deadline:
                    reaped, status = os.waitpid(child, os.WNOHANG)
                    if not reaped:
                        threading.Event().wait(0.002)
                if not reaped:
                    # This unreaped, owned child cannot have a reused PID.
                    os.kill(child, signal.SIGKILL)
                    _, status = os.waitpid(child, 0)
            assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
            tx.event("lifespan", "parent-after-child", uid(), None)
        assert events(store) == ["before", "parent-after-child"]


@pytest.mark.parametrize("when", ["before", "during"])
def test_compatible_database_replacement_is_rejected_without_committing(store, when):
    backup, original = store.path.with_name("replacement.sqlite3"), store.path.with_name("original.sqlite3")
    with owned_writer(store):
        write_event(store, "before")
        assert store.backup(backup)["verified"]

        def replace():
            store.path.rename(original)
            # A WAL database is a bundle; retaining the original history needs
            # its committed WAL and shared-memory files alongside the main DB.
            for suffix in ("-wal", "-shm"):
                sidecar = store.path.with_name(store.path.name + suffix)
                if sidecar.exists():
                    sidecar.rename(original.with_name(original.name + suffix))
            backup.rename(store.path)

        with pytest.raises(CoordinationError) as failure:
            if when == "before":
                replace()
                with store.write() as tx:
                    tx.event("lifespan", "must-not-write", uid(), None)
            else:
                with store.write() as tx:
                    tx.event("lifespan", "must-rollback", uid(), None)
                    replace()
        assert failure.value.code == "AUTHORITY_FENCED"
        with pytest.raises(CoordinationError) as repeated:
            write_event(store, "must-not-rebind")
        assert repeated.value.code == "AUTHORITY_FENCED"
    assert events(Store(original, store.workspace_id)) == ["before"]
    assert events(Store(store.path, store.workspace_id)) == ["before"]


@pytest.mark.parametrize("field", ["schema", "workspace", "state"])
def test_retained_writer_reads_current_schema_workspace_and_service_state(store, field):
    with owned_writer(store):
        write_event(store, "before")
        with sqlite3.connect(store.path) as other:
            if field == "schema":
                other.execute("PRAGMA user_version=2")
            else:
                other.execute("UPDATE meta SET value_json=? WHERE key=?",
                              (json.dumps(uid() if field == "workspace" else "fenced"),
                               "workspace_id" if field == "workspace" else "service_state"))
        with pytest.raises(CoordinationError) as failure:
            write_event(store, "must-not-write")
        assert failure.value.code == {"schema": "SCHEMA_MISMATCH", "workspace": "WRONG_WORKSPACE",
                                      "state": "AUTHORITY_FENCED"}[field]
    assert store._writer_connection is None


def test_actual_daemon_restores_activation_then_closes_writer_before_unlock(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    service = application.build_service(register_workspace(root, state_root=tmp_path / "state"))
    lifecycle, close_writer = application._lifecycle, service.store._close_writer_connection
    order = []

    def transition(actual, state):
        assert actual.store._writer_connection is not None
        order.append(state)
        return lifecycle(actual, state)

    def close():
        with pytest.raises(CoordinationError) as blocked, ownership_lock(service.workspace.state_dir / "service.lock"):
            pass
        assert blocked.value.code == "SERVICE_BUSY"
        close_writer()
        order.append("closed")

    monkeypatch.setattr(application, "_lifecycle", transition)
    monkeypatch.setattr(service.store, "_close_writer_connection", close)
    native = {"harness": "codex", "native_session_id": "writer-lifespan-native"}

    def run_once():
        with application.make_server(service) as server:
            connection = service.store._writer_connection
            assert connection is not None
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with Client(service.workspace.socket_path, native, workspace_id=service.workspace.id) as client:
                    result = client.call("identity.get")
                    assert result["ok"]
                    actor_id = result["data"]["actor"]["id"]
            finally:
                server.shutdown()
                thread.join(5)
                assert not thread.is_alive()
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")
        assert service.store._writer_pid is None and service.store._writer_connection is None
        with ownership_lock(service.workspace.state_dir / "service.lock"), service.store.read() as tx:
            assert json.loads(tx.connection.execute("SELECT value_json FROM meta WHERE key='service_state'").fetchone()[0]) == "active"
            assert tx.connection.execute("SELECT COUNT(*) FROM bindings").fetchone()[0] == 0
        return actor_id, connection

    first_actor, first_connection = run_once()
    second_actor, second_connection = run_once()
    assert first_actor == second_actor and first_connection is not second_connection
    assert order == ["draining", "active", "closed"] * 2


def test_shutdown_authority_failure_still_joins_runtime_and_closes_writer(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    service = application.build_service(register_workspace(root, state_root=tmp_path / "state"))
    before = set(threading.enumerate())
    with pytest.raises(CoordinationError) as failure, application.make_server(service):
        connection = service.store._writer_connection
        maintenance = [thread for thread in threading.enumerate()
                       if thread not in before and thread.name == "agentcoord-maintenance"]
        assert len(maintenance) == 1
        with sqlite3.connect(service.store.path) as other:
            other.execute("UPDATE meta SET value_json=? WHERE key='workspace_id'", (json.dumps(uid()),))
    assert failure.value.code == "WRONG_WORKSPACE"
    assert not maintenance[0].is_alive()
    assert service.store._writer_connection is None and service.store._writer_pid is None
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")
    with ownership_lock(service.workspace.state_dir / "service.lock"):
        pass
