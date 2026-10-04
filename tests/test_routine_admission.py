"""Bounded routine admission and complete cleanup of admitted native bindings."""

from __future__ import annotations

import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest

from agentcoord import transport
from agentcoord.application import build_service, make_server
from agentcoord.config import Config, register_workspace
from agentcoord.core import CoordinationError
from agentcoord.transport import Client


def wait_for(predicate, message):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if predicate():
            return
        threading.Event().wait(0.002)
    pytest.fail(message)


def pending(gate):
    with gate._mutex:
        return len(gate._pending)


def make_service(tmp_path, config):
    root = tmp_path / "repo"
    root.mkdir()
    return build_service(register_workspace(root, state_root=tmp_path / "state"), config)


@contextmanager
def running(service):
    with make_server(service) as server:
        errors = []
        original = server.handle_error

        def handle_error(request, address):
            errors.append(sys.exception())
            original(request, address)

        server.handle_error = handle_error
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server, errors
        finally:
            server.shutdown()
            thread.join(5)
            assert not thread.is_alive()


def native_client(service, session):
    return Client(service.workspace.socket_path,
                  {"harness": "codex", "native_session_id": session},
                  workspace_id=service.workspace.id)


def test_routine_queue_is_bounded_and_deadline_releases_waiter():
    gate = transport._RoutineAdmission(1, 1)
    assert gate.acquire(0)
    with ThreadPoolExecutor(max_workers=1) as pool:
        request = pool.submit(gate.acquire, 1)
        wait_for(lambda: pending(gate) == 1, "Routine waiter did not queue")
        try:
            assert not gate.acquire(0)
            assert request.result(timeout=3) is False
            assert pending(gate) == 0
        finally:
            gate.release()
    assert gate.acquire(0)
    gate.release()
    assert gate._running == 0


def test_interrupted_wait_releases_pending_admission(monkeypatch):
    gate = transport._RoutineAdmission(1, 1)
    assert gate.acquire(0)
    native_event = threading.Event

    def interrupted_event():
        event = native_event()

        def wait(_timeout):
            raise KeyboardInterrupt

        event.wait = wait
        return event

    with monkeypatch.context() as patch:
        patch.setattr(transport.threading, "Event", interrupted_event)
        with pytest.raises(KeyboardInterrupt):
            gate.acquire(1)
    assert pending(gate) == 0 and gate._running == 1
    gate.release()
    assert gate.acquire(0)
    gate.release()


def test_real_routine_calls_keep_fifo_order_before_fresh_calls(tmp_path):
    service = make_service(tmp_path, Config(fast_workers=1, fast_queue=4))
    entered, release = threading.Event(), threading.Event()
    order = []
    with running(service) as (server, errors):
        clients = [native_client(service, f"routine-{index}").connect() for index in range(4)]
        try:
            actors = [client.call("identity.get")["data"]["actor"]["id"] for client in clients]
            original = service.execute

            def execute(context, call):
                if context.actor_id == actors[0] and not entered.is_set():
                    entered.set()
                    assert release.wait(5)
                order.append(context.actor_id)
                return original(context, call)

            service.execute = execute

            def first_and_fresh():
                assert clients[0].call("identity.get")["ok"]
                assert clients[0].call("identity.get")["ok"]

            with ThreadPoolExecutor(max_workers=4) as pool:
                requests = [pool.submit(first_and_fresh)]
                try:
                    assert entered.wait(5)
                    for index in range(1, 4):
                        requests.append(pool.submit(clients[index].call, "identity.get"))
                        wait_for(lambda index=index: pending(server._routine) == index,
                                 "Real routine call did not enter FIFO admission")
                finally:
                    release.set()
                for request in requests:
                    result = request.result(timeout=5)
                    assert result is None or result["ok"]
            assert order == [*actors, actors[0]]
            assert not errors
        finally:
            release.set()
            for client in clients:
                client.close()


def test_maximum_admitted_connections_all_unbind_without_handler_errors(tmp_path):
    config = Config(fast_workers=16, fast_queue=128, slow_workers=4)
    service = make_service(tmp_path, config)
    maximum = config.fast_workers + config.fast_queue
    assert service.store._writer.capacity == maximum + config.slow_workers + 2
    with running(service) as (server, errors):
        clients = []
        try:
            for index in range(maximum):
                clients.append(native_client(service, f"held-{index}").connect())
            with service.store.read() as tx:
                assert tx.connection.execute("SELECT COUNT(*) FROM bindings").fetchone()[0] == maximum
            with pytest.raises(CoordinationError) as rejection:
                native_client(service, "over-budget").connect()
            assert rejection.value.code == "SERVICE_BUSY"
            # Hold the actual writer while every admitted socket begins its
            # final cleanup. This reproduces the previous 128-slot overflow.
            with service.store.write(maintenance=True):
                for client in clients:
                    client.close()
                wait_for(lambda: pending(service.store._writer) >= maximum,
                         "All admitted connection cleanups did not queue")
            wait_for(lambda: not server._sockets, "Admitted connection cleanup did not finish")
            with service.store.read() as tx:
                assert tx.connection.execute("SELECT COUNT(*) FROM bindings").fetchone()[0] == 0
            assert not errors
        finally:
            for client in clients:
                client.close()
    assert not errors


@pytest.mark.parametrize("operation", ["service.health", "service.drain", "service.activate"])
def test_maintenance_failure_keeps_structured_request_identity(tmp_path, operation):
    service = make_service(tmp_path, Config())
    with running(service) as (server, errors):
        original_health, original_lifecycle = server.health_provider, server.lifecycle

        def fail(*_arguments):
            raise CoordinationError("STORAGE_UNAVAILABLE", "Actual maintenance storage failure")

        try:
            if operation == "service.health":
                server.health_provider = fail
            else:
                server.lifecycle = fail
            with Client(service.workspace.socket_path, workspace_id=service.workspace.id,
                        operator=True) as client:
                result = client.call(operation)
            assert result["error"]["code"] == "STORAGE_UNAVAILABLE"
            assert result["error"]["message"] == "Actual maintenance storage failure"
            assert result["request_id"] and not errors
        finally:
            server.health_provider, server.lifecycle = original_health, original_lifecycle
