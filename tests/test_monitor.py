import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agentcoord import monitor


class Client:
    def __init__(self):
        self.calls = []

    def call(self, operation, arguments):
        self.calls.append((operation, arguments))
        return {"ok": True, "data": {"items": [], "cursor": None}}


def test_queries_are_read_only_and_retain_explicit_filter_cursor_parameters():
    client = Client()
    monitor.snapshot(client, cursor="bound", filters={"actor_id": "a"})
    monitor.history(client, cursor="history-bound", query="reason")
    monitor.detail(client, {"kind": "message", "id": "m1"})
    monitor.detail(client, {"kind": "history", "id": "e1", "item": {"sequence": 7}})
    assert [call[0] for call in client.calls] == ["operator.snapshot", "operator.history", "message.get", "operator.history"]
    assert client.calls[0][1]["cursor"] == "bound"
    assert client.calls[1][1]["query"] == "reason"
    assert client.calls[-1][1] == {"sequence": 7}


def test_reported_state_observed_presence_and_age_are_separate():
    data = {"actors": [{"id": "a", "label": "Writer", "task": "prepare", "reported_state": "working", "observed_state": "offline", "observed_us": 1_000_000}]}
    text = monitor.rows(data, 0, now_us=5_000_000)[0]["text"]
    assert "reported=working" in text
    assert "observed=offline" in text
    assert "age=4s" in text
    data["actors"][0]["observed_us"] = None
    text = monitor.rows(data, 0, now_us=5_000_000)[0]["text"]
    assert "observed=unknown" in text


def test_durable_message_history_uses_domain_pointer_for_full_body():
    data = {"items": [{"domain": "messages", "kind": "sent", "record_id": "message-one", "sequence": 8, "summary": "preview"}]}
    row = monitor.rows(data, 3)[0]
    assert row["kind"] == "message"
    assert row["id"] == "message-one"


def test_unavailable_projection_never_looks_like_an_empty_inbox():
    with pytest.raises(monitor.MonitorError, match="SERVICE_BUSY"):
        monitor._data({"ok": False, "error": {"code": "SERVICE_BUSY", "message": "Admission is full"}})


def test_terminal_control_bytes_cannot_inject_escape_sequences():
    assert monitor._safe("hello\x1b[31m\x00\nworld") == "hello [31m  world"


def test_page_reset_discards_stale_display_and_cursors():
    view = monitor.View(cursor="old", next_cursor="next", previous=[None], data={"actors": [{"id": "old"}]})
    view.reset_page()
    assert view.cursor is None and view.next_cursor is None
    assert not view.previous and not view.data


def test_terminal_failure_reports_actionable_error(capsys):
    def fail(*args):
        raise OSError("terminal unavailable")
    assert monitor.run(Client(), screen_runner=fail) == 2
    assert "terminal unavailable" in capsys.readouterr().err
