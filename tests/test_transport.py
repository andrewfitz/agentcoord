"""Socket-bound attribution, bounded admission and uncertain response regressions."""

from __future__ import annotations

import io
import tempfile
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from agentcoord.core import Context, CoordinationError
from agentcoord.transport import (
    MAX_FRAME,
    Client,
    encode_frame,
    owned_server,
    read_frame,
)


class Service:
    def __init__(self):
        self.operations = {
            "read": SimpleNamespace(mutation=False),
            "write": SimpleNamespace(mutation=True),
        }
        self.calls = []
        self.block = None

    def execute(self, context, call):
        self.calls.append((context, call))
        if call.operation == "write" and self.block is not None:
            self.block.wait(3)
        return {
            "ok": True,
            "data": {"actor": context.actor_id, "operation": call.operation},
            "action_digest": None,
        }


@contextmanager
def running(tmp_path, *, service=None, bind_gate=None, **options):
    # Darwin socket addresses are limited to 104 bytes; supervisor scratch names
    # deliberately carry more provenance than fits inside a Unix address.
    directory = tempfile.TemporaryDirectory(prefix="ac-", dir="/tmp")
    from pathlib import Path

    path = Path(directory.name) / "service.sock"
    service = service or Service()
    bindings = []

    def bind(native, transport, connection_id):
        if bind_gate:
            entered, release = bind_gate
            entered.set()
            release.wait(3)
        bindings.append(dict(native))
        return Context(
            "workspace", native["native_session_id"], connection_id, transport, "task-1", None
        )

    def operator(connection_id):
        return Context("workspace", None, connection_id, "operator", None, None, True)

    with owned_server(
        path,
        service,
        workspace_id="workspace",
        bind=bind,
        operator_context=operator,
        health=lambda: {
            "schema_version": 1,
            "release": "test",
            "running_effects": 0,
            "uncertain_effects": 0,
        },
        **options,
    ) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server, service, bindings, path
        finally:
            server.shutdown()
            thread.join(3)
            directory.cleanup()


def test_connection_keeps_originating_actor_and_rejects_override(tmp_path):
    with running(tmp_path) as (_, service, bindings, path):
        with Client(path, {"native_session_id": "native-a"}, workspace_id="workspace") as client:
            client.native_context["native_session_id"] = "native-b"
            assert client.call("read")["data"]["actor"] == "native-a"
            frame = {
                "protocol": 1,
                "workspace_id": "workspace",
                "request_id": "override",
                "operation": "write",
                "arguments": {},
                "key": "retry",
                "actor_id": "native-b",
            }
            client._socket.sendall(encode_frame(frame))
            assert read_frame(client._stream)["error"]["code"] == "INVALID_ARGUMENT"
            assert len(service.calls) == 1
        assert bindings == [{"native_session_id": "native-a"}]


def test_workspace_and_protocol_fail_before_binding(tmp_path):
    with running(tmp_path) as (_, service, bindings, path):
        with pytest.raises(CoordinationError) as exc:
            Client(path, {"native_session_id": "native-a"}, workspace_id="other").connect()
        assert exc.value.code == "WRONG_WORKSPACE"
        assert not service.calls and not bindings


def test_slow_budget_does_not_hold_routine_capacity_and_drain_is_explicit(tmp_path):
    release = threading.Event()
    entered = threading.Event()
    with running(tmp_path, routine_workers=1, slow_workers=1, slow_queue=1) as (server, _, _, path):

        def slow(_):
            entered.set()
            release.wait(3)

        try:
            assert server.submit_slow("slow-a", slow)
            assert entered.wait(1)
            with Client(
                path, {"native_session_id": "native-a"}, workspace_id="workspace"
            ) as client:
                assert client.call("read")["ok"]
                assert server.begin_drain()["service_state"] == "draining"
                assert client.call("write", key="write-key")["error"]["code"] == "AUTHORITY_FENCED"
                assert client.call("read")["ok"]
                assert not server.submit_slow("slow-b", slow)
            release.set()
            assert server.wait_quiescent(2)
            assert server.activate()["service_state"] == "active"
        finally:
            release.set()


def test_timeout_retains_retry_identity_and_never_replays(tmp_path):
    service = Service()
    service.block = threading.Event()
    with running(tmp_path, service=service) as (_, _, _, path):
        with Client(
            path, {"native_session_id": "native-a"}, workspace_id="workspace", timeout=0.1
        ) as client:
            reply = client.call("write", {}, key="original-key")
            assert reply["error"]["code"] == "RECONCILIATION_REQUIRED"
            assert reply["error"]["details"]["retry_key"] == "original-key"
            assert len(service.calls) == 1
            assert service.calls[0][1].key == "original-key"
        service.block.set()


def test_service_owner_does_not_unlink_live_socket(tmp_path):
    with running(tmp_path) as (server, _, _, path):
        inode = path.stat().st_ino
        with (
            pytest.raises(CoordinationError) as exc,
            owned_server(
                path,
                server.service,
                workspace_id="workspace",
                bind=server.bind,
                operator_context=server.operator_context,
            ),
        ):
            pass
        assert exc.value.code == "SERVICE_BUSY"
        assert path.stat().st_ino == inode


def test_transport_rejects_oversize_and_nonfinite_before_dispatch():
    with pytest.raises(CoordinationError):
        read_frame(io.BytesIO(b'"' + b"x" * MAX_FRAME + b'"\n'))
    with pytest.raises(CoordinationError):
        read_frame(io.BytesIO(b'{"x":NaN}\n'))
    with pytest.raises(CoordinationError):
        encode_frame({"body": "x" * MAX_FRAME})
    with pytest.raises(CoordinationError):
        encode_frame({"x": float("inf")})


def test_operator_close_interrupts_blocked_read_without_call_lock(tmp_path):
    service = Service()
    entered, release = threading.Event(), threading.Event()
    original = service.execute

    def blocked(context, call):
        entered.set()
        release.wait(3)
        return original(context, call)

    service.execute = blocked
    with running(tmp_path, service=service) as (_, _, _, path):
        client = Client(path, workspace_id="workspace", operator=True)
        replies = []
        thread = threading.Thread(target=lambda: replies.append(client.call("read")))
        thread.start()
        try:
            assert entered.wait(1)
            client.close()
            thread.join(1)
            assert not thread.is_alive()
            assert replies[0]["error"]["code"] == "STORAGE_UNAVAILABLE"
        finally:
            release.set()
            thread.join(3)


def test_disconnected_binding_is_released_once_and_retry_guards_survive_reconnect(tmp_path):
    released = []
    event = threading.Event()

    def unbind(context):
        released.append(context.connection_id)
        event.set()

    class UpdatingService(Service):
        def execute(self, context, call):
            result = super().execute(context, call)
            result["next_context"] = {"task_generation": "task-2", "execution_generation": None}
            return result

    with running(tmp_path, service=UpdatingService(), unbind=unbind) as (
        _,
        service,
        bindings,
        path,
    ):
        client = Client(path, {"native_session_id": "native-a"}, workspace_id="workspace")
        assert client.call("write", key="retry")["ok"]
        client.native_context["native_session_id"] = "other-session"
        client.close()
        assert event.wait(1)
        assert len(released) == 1
        assert client.call("write", key="retry")["ok"]
        assert client.call("write", key="fresh")["ok"]
        assert [call.expected_task_generation for _, call in service.calls] == [
            "task-1",
            "task-1",
            "task-2",
        ]
        assert bindings == [{"native_session_id": "native-a"}] * 2
        client.close()


def test_explicit_origin_guards_survive_response_updates_and_reconnect(tmp_path):
    import uuid

    task, execution = str(uuid.uuid4()), str(uuid.uuid4())

    class UpdatingService(Service):
        def execute(self, context, call):
            result = super().execute(context, call)
            result["next_context"] = {"task_generation": "later-task", "execution_generation": None}
            return result

    with running(tmp_path, service=UpdatingService()) as (_, service, bindings, path):
        client = Client(path, {"native_session_id": "native-a"}, workspace_id="workspace")
        guards = {"expected_task_generation": task, "expected_execution_generation": execution}
        assert client.call("write", key="origin-retry", **guards)["ok"]
        client.close()
        assert client.call("write", key="origin-retry", **guards)["ok"]
        assert client.call("write", key="origin-retry")["ok"]
        assert client.call("write", key="new-origin-call", **guards)["ok"]
        assert [(call.expected_task_generation, call.expected_execution_generation)
                for _, call in service.calls] == [(task, execution)] * 4
        assert bindings == [{"native_session_id": "native-a"}] * 2
        rejected = client.call("write", key="origin-retry", expected_task_generation=str(uuid.uuid4()),
                               expected_execution_generation=execution)
        assert not rejected["ok"] and rejected["error"]["code"] == "IDEMPOTENCY_CONFLICT"
        assert len(service.calls) == 4
        client.close()


def test_close_interrupts_binding_handshake_read(tmp_path):
    entered, release = threading.Event(), threading.Event()
    with running(tmp_path, bind_gate=(entered, release)) as (_, _, _, path):
        client = Client(path, {"native_session_id": "native-a"}, workspace_id="workspace")
        replies = []
        thread = threading.Thread(target=lambda: replies.append(client.call("read")))
        thread.start()
        try:
            assert entered.wait(1)
            client.close()
            thread.join(1)
            assert not thread.is_alive()
            assert replies[0]["error"]["code"] == "STORAGE_UNAVAILABLE"
        finally:
            release.set()
            thread.join(3)
