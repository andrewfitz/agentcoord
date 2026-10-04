"""Real SQLite writes stay ordered, bounded and isolated from WAL reads."""
from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from agentcoord import identity
from agentcoord.core import CoordinationError
from agentcoord.store import Store


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "state" / "runtime.sqlite3", str(uuid.uuid4()))
    value.initialize((identity.SCHEMA,))
    return value


def queued(store, count):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with store._writer._mutex:
            if len(store._writer._pending) == count:
                return
        threading.Event().wait(0.002)
    pytest.fail(f"Expected {count} writer requests to enter admission")


def test_writers_keep_fifo_order_without_opening_waiting_connections_and_reads_progress(store, monkeypatch):
    opened = []
    original_connect = store._connect
    tracking = threading.Lock()

    def connect(**options):
        if not options.get("readonly"):
            with tracking:
                opened.append(threading.current_thread().name)
        return original_connect(**options)

    monkeypatch.setattr(store, "_connect", connect)
    completed = []

    def write(index):
        with store.write() as tx:
            tx.event("admission", "completed", str(index), None)
            completed.append(index)

    with ThreadPoolExecutor(max_workers=4) as pool:
        with store.write() as tx:
            tx.event("admission", "uncommitted", "owner", None)
            requests = []
            for index in range(4):
                requests.append(pool.submit(write, index))
                queued(store, index + 1)
            assert len(opened) == 1
            with store.read() as read:
                assert read.connection.execute("SELECT COUNT(*) FROM events WHERE kind='uncommitted'").fetchone()[0] == 0
        # The previous owner requests another write immediately. It cannot jump
        # ahead of the existing waiters while their first thread wakes up.
        write(4)
        for request in requests:
            request.result(timeout=5)
    assert completed == list(range(5))
    with store.read() as tx:
        assert [row[0] for row in tx.connection.execute("SELECT record_id FROM events WHERE kind='completed' ORDER BY sequence")] == [str(i) for i in range(5)]


def test_full_queue_and_expired_waiter_fail_before_sqlite_and_leave_no_stale_slot(store, monkeypatch):
    store._writer.capacity = 1
    store._writer.timeout = 0.25
    entered, release = threading.Event(), threading.Event()
    opened = []
    original_connect = store._connect

    def connect(**options):
        opened.append(options)
        return original_connect(**options)

    monkeypatch.setattr(store, "_connect", connect)

    def owner():
        with store.write():
            entered.set()
            assert release.wait(5)

    def waiter():
        try:
            with store.write():
                pytest.fail("Expired waiter opened a transaction")
        except CoordinationError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        holding = pool.submit(owner)
        assert entered.wait(5)
        waiting = pool.submit(waiter)
        queued(store, 1)
        try:
            with pytest.raises(CoordinationError) as failure, store.write():
                pass
            assert failure.value.code == "SERVICE_BUSY" and failure.value.retryable
            expired = waiting.result(timeout=5)
            assert expired.code == "SERVICE_BUSY" and expired.retryable
            assert len(opened) == 1
            queued(store, 0)
        finally:
            release.set()
        holding.result(timeout=5)
    with store.write() as tx:
        tx.event("admission", "healthy", "after-timeout", None)
    assert store._writer._owner is None


def test_connection_failure_releases_admission_for_next_real_write(store, monkeypatch):
    original_connect = store._connect
    fail = True

    def connect(**options):
        nonlocal fail
        if fail:
            fail = False
            raise sqlite3.OperationalError("Unavailable before a transaction starts")
        return original_connect(**options)

    monkeypatch.setattr(store, "_connect", connect)
    with pytest.raises(sqlite3.OperationalError), store.write():
        pass
    with store.write() as tx:
        tx.event("admission", "healthy", "after-connect-error", None)
    assert store._writer._owner is None


def test_sqlite_still_excludes_an_external_writer_and_failure_closes_connection(store, monkeypatch):
    store._writer.timeout = 0.1
    connections = []
    original_connect = store._connect

    def connect(**options):
        value = original_connect(**options)
        connections.append(value)
        return value

    monkeypatch.setattr(store, "_connect", connect)
    external = sqlite3.connect(store.path, isolation_level=None)
    try:
        external.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError), store.write() as tx:
            tx.event("admission", "must-not-commit", "blocked", None)
        with pytest.raises(sqlite3.ProgrammingError):
            connections[0].execute("SELECT 1")
        assert store._writer._owner is None
    finally:
        external.rollback()
        external.close()
    with store.write() as tx:
        assert tx.connection.execute("SELECT COUNT(*) FROM events WHERE kind='must-not-commit'").fetchone()[0] == 0
        tx.event("admission", "healthy", "after-external-writer", None)


def test_nested_write_fails_explicitly_without_abandoning_outer_transaction(store):
    with store.write() as tx:
        with pytest.raises(CoordinationError) as failure, store.write():
            pass
        assert failure.value.code == "INVALID_ARGUMENT"
        tx.event("admission", "healthy", "outer-still-owned", None)
    with store.read() as tx:
        assert tx.connection.execute("SELECT COUNT(*) FROM events WHERE kind='healthy'").fetchone()[0] == 1
