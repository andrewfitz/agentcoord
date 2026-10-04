"""A closed Unix peer's buffered rejection remains a structured native error."""
from __future__ import annotations

import socket
import tempfile
from pathlib import Path

import pytest

from agentcoord import transport
from agentcoord.core import CoordinationError
from agentcoord.transport import Client, encode_frame, error_envelope


class ConnectedSocket:
    """Fault at bind sending using a real, already-closed Unix peer."""

    def __init__(self, connection):
        self.connection = connection

    def settimeout(self, timeout):
        self.connection.settimeout(timeout)

    def connect(self, _path):
        pass

    def makefile(self, mode):
        return self.connection.makefile(mode)

    def sendall(self, data):
        return self.connection.sendall(data)

    def close(self):
        self.connection.close()


def closed_peer(monkeypatch, frame=None):
    connection, peer = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    if frame is not None:
        peer.sendall(encode_frame(frame))
    peer.close()
    facade = ConnectedSocket(connection)
    monkeypatch.setattr(transport.socket, "socket", lambda *_args, **_kwargs: facade)
    return connection


def test_buffered_busy_rejection_survives_real_broken_pipe_before_bind(monkeypatch):
    rejection = error_envelope("SERVICE_BUSY", "Connection budget exhausted", retryable=True,
                               details={"budget": 1}, next_action="Reconnect after admitted clients finish")
    connection = closed_peer(monkeypatch, rejection)
    with tempfile.TemporaryDirectory(prefix="ac-busy-", dir="/tmp") as directory:
        client = Client(Path(directory) / "service.sock", {"harness": "codex", "native_session_id": "original"}, workspace_id="workspace")
        with pytest.raises(CoordinationError) as failure:
            client.connect()
        assert failure.value.code == "SERVICE_BUSY" and failure.value.retryable
        assert failure.value.details == {"budget": 1}
        assert failure.value.next_action == rejection["error"]["next_action"]
        assert client._socket is None and client._stream is None
    connection.close()


@pytest.mark.parametrize("frame", [None, {"ok": True, "protocol": 1, "next_context": {"task_generation": "must-not-bind"}}, {"ok": False, "protocol": 1, "error": {}}])
def test_missing_rejection_or_success_after_failed_send_is_not_fabricated_busy(monkeypatch, frame):
    connection = closed_peer(monkeypatch, frame)
    with tempfile.TemporaryDirectory(prefix="ac-close-", dir="/tmp") as directory:
        client = Client(Path(directory) / "service.sock", {}, workspace_id="workspace")
        original_context = dict(client._next_context)
        with pytest.raises(OSError):
            client.connect()
        assert client._next_context == original_context
        assert client._socket is None and client._stream is None
    connection.close()


def test_buffered_rejection_still_checks_protocol(monkeypatch):
    rejection = error_envelope("SERVICE_BUSY", "wrong release", retryable=True)
    rejection["protocol"] = 2
    connection = closed_peer(monkeypatch, rejection)
    with tempfile.TemporaryDirectory(prefix="ac-proto-", dir="/tmp") as directory:
        client = Client(Path(directory) / "service.sock", {}, workspace_id="workspace")
        with pytest.raises(CoordinationError) as failure:
            client.connect()
        assert failure.value.code == "PROTOCOL_MISMATCH"
        assert client._socket is None and client._stream is None
    connection.close()
