"""Workspace authority excludes alternate transport routes and maintenance."""

import threading
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from agentcoord.application import build_service, make_server, open_offline_store
from agentcoord.config import discover_workspace, register_workspace
from agentcoord.core import CoordinationError
from agentcoord.transport import Client


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


@pytest.fixture
def socket_root():
    # Supervisor scratch paths exceed macOS's Unix socket address budget.
    with TemporaryDirectory(prefix="ac-own-", dir="/tmp") as directory:
        yield Path(directory).resolve()


def routes(tmp_path, monkeypatch, socket_root):
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setenv("AGENTCOORD_SOCKET_HOME", str(socket_root / "a"))
    first = register_workspace(root, state_root=tmp_path / "state")
    monkeypatch.setenv("AGENTCOORD_SOCKET_HOME", str(socket_root / "b"))
    second = discover_workspace(explicit_root=root, state_root=tmp_path / "state")
    assert first.id == second.id
    assert first.database_path == second.database_path
    assert first.socket_path != second.socket_path
    return first, second


def test_alternate_socket_cannot_start_second_workspace_owner(tmp_path, monkeypatch, socket_root):
    first, second = routes(tmp_path, monkeypatch, socket_root)
    service = build_service(first)
    alternative = build_service(second)
    with running(service), Client(
        first.socket_path, {"harness": "codex", "native_session_id": "original"},
        workspace_id=first.id,
    ) as native:
        assert native.call("identity.get")["ok"]
        with pytest.raises(CoordinationError) as error, make_server(alternative):
            pass
        assert error.value.code == "SERVICE_BUSY"
        # Rejected startup cannot run recovery and revoke the owner's binding.
        assert native.call("identity.get")["ok"]


def test_alternate_socket_cannot_enter_offline_maintenance(tmp_path, monkeypatch, socket_root):
    first, second = routes(tmp_path, monkeypatch, socket_root)
    with running(build_service(first)):
        with pytest.raises(CoordinationError) as error, open_offline_store(second):
            pass
        assert error.value.code == "SERVICE_BUSY"


@pytest.mark.parametrize("lifecycle_failure", [False, True])
def test_server_close_waits_for_admitted_routine_request(
    tmp_path, monkeypatch, socket_root, lifecycle_failure
):
    first, _ = routes(tmp_path, monkeypatch, socket_root)
    service = build_service(first)
    admitted = threading.Event()
    release = threading.Event()
    closed = threading.Event()
    original_execute = service.execute

    def gated_execute(context, call):
        admitted.set()
        assert release.wait(5)
        return original_execute(context, call)

    monkeypatch.setattr(service, "execute", gated_execute)
    with running(service) as server:
        original_lifecycle = server.lifecycle
        persistence_error = CoordinationError("STORAGE_UNAVAILABLE", "Injected drain failure")
        errors = []
        if lifecycle_failure:
            def fail_lifecycle(state):
                raise persistence_error

            server.lifecycle = fail_lifecycle
        with Client(first.socket_path, {"harness": "codex", "native_session_id": "drain"},
                    workspace_id=first.id) as native:
            def request():
                # Closing the socket can discard the response, but admitted execution
                # and connection cleanup must finish before server_close returns.
                try:
                    native.call("identity.get")
                except (OSError, EOFError):
                    pass

            worker = threading.Thread(target=request, daemon=True)
            worker.start()
            assert admitted.wait(5)

            def close():
                try:
                    server.server_close()
                except CoordinationError as error:
                    errors.append(error)
                finally:
                    closed.set()

            closer = threading.Thread(target=close, daemon=True)
            closer.start()
            try:
                assert not closed.wait(0.2), "Shutdown returned while an admitted request was live"
            finally:
                release.set()
                closer.join(5)
                worker.join(5)
                server.lifecycle = original_lifecycle
            assert closed.is_set()
            assert not worker.is_alive()
            assert errors == ([persistence_error] if lifecycle_failure else [])


def test_normal_stop_restores_active_state_before_releasing_owner(tmp_path, monkeypatch, socket_root):
    import json

    from agentcoord import application, transport

    first, _ = routes(tmp_path, monkeypatch, socket_root)
    service = build_service(first)
    original_lifecycle = application._lifecycle
    restored = []

    def observe_lifecycle(service, state):
        if state == "active":
            # Probe the actual exclusion boundary immediately before the old
            # owner's terminal write; another owner must still be excluded.
            with pytest.raises(CoordinationError) as error, transport.ownership_lock(
                first.state_dir / "service.lock"
            ):
                pass
            assert error.value.code == "SERVICE_BUSY"
            restored.append(state)
        return original_lifecycle(service, state)

    monkeypatch.setattr(application, "_lifecycle", observe_lifecycle)
    with make_server(service):
        pass
    assert restored == ["active"]
    with service.store.read() as tx:
        assert json.loads(tx.connection.execute(
            "SELECT value_json FROM meta WHERE key='service_state'"
        ).fetchone()[0]) == "active"
