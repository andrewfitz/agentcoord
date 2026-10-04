"""Regressions for public action scope and required CLI outcomes."""

import curses
import hashlib
import json
import sqlite3
import uuid
from types import SimpleNamespace

import anyio
import pytest
from mcp import ClientSession

from agentcoord import application, cli, config, identity, migrate
from agentcoord.application import build_service
from agentcoord.core import Call, Context
from agentcoord.mcp import create_server
from agentcoord.transport import MAX_FRAME


def test_workspace_root_action_filter_matches_unfiltered_empty_world(tmp_path):
    workspace_id = str(uuid.uuid4())
    service = build_service(SimpleNamespace(
        id=workspace_id, root=tmp_path, database_path=tmp_path / "state/runtime.sqlite3",
        state_dir=tmp_path / "state",
    ))
    context = Context(workspace_id, None, operator=True, transport="operator")
    plain = service.execute(context, Call("operator.snapshot", {"section": "actions"}))
    scoped = service.execute(context, Call("operator.snapshot", {
        "section": "actions", "filters": {"path": "."},
    }))
    assert plain["ok"], plain
    assert scoped["ok"], scoped
    assert scoped["data"]["actions"]["items"] == plain["data"]["actions"]["items"]
    assert scoped["data"]["actions"]["counts"] == plain["data"]["actions"]["counts"]


def test_monitor_terminal_failure_reaches_cli_exit_status(tmp_path, monkeypatch, capsys):
    workspace = SimpleNamespace(id=str(uuid.uuid4()), root=tmp_path,
                                socket_path=tmp_path / "service.sock")
    monkeypatch.setattr(config, "discover_workspace", lambda **kwargs: workspace)

    def unavailable_terminal():
        raise curses.error("terminal unavailable")

    monkeypatch.setattr(curses, "initscr", unavailable_terminal)
    status = cli.main(["monitor"])
    assert "Cannot open coordination monitor" in capsys.readouterr().err
    assert status != 0


def test_imported_large_attachment_reference_keeps_body_and_reference_retrievable(tmp_path, monkeypatch, capsys):
    from test_migrate import conversation_source

    retained = tmp_path / "retained"
    retained.mkdir()
    attachment = retained / "note.txt"
    attachment.write_bytes(b"Retained attachment")
    reference = {
        "path": "note.txt", "bytes": attachment.stat().st_size,
        "sha256": hashlib.sha256(attachment.read_bytes()).hexdigest(),
        "caption": "界" * 100000,
    }
    source = tmp_path / "source.sqlite3"
    conversation_source(source, body="Body", attachment=reference)
    references = [reference, {**reference, "caption": "Small reference"}]
    with sqlite3.connect(source) as source_db:
        source_db.execute("UPDATE messages SET attachments=?", (json.dumps(references),))
        source_db.execute("""INSERT INTO agents SELECT 2,project_id,'Unrelated',program,
            model,task_description,inception_ts,last_active_ts,registration_token,
            contact_policy FROM agents WHERE id=1""")
    manifest = migrate.inspect_sources([
        migrate.SourceSpec(source, "/selected", attachments_root=retained),
    ])
    workspace = SimpleNamespace(
        id=str(uuid.uuid4()), root=tmp_path, state_dir=tmp_path / "state",
        socket_path=tmp_path / "state/service.sock",
        database_path=tmp_path / "state/runtime.sqlite3",
    )
    service = build_service(workspace)
    imported = migrate.apply_import(
        service.store, migrate.prepare_import(manifest, workspace), run_id="large-reference",
    )
    assert imported["state"] == "complete", imported
    with service.store.read() as transaction:
        message_id = transaction.connection.execute("SELECT id FROM messages").fetchone()[0]
    context = Context(workspace.id, None, operator=True, transport="operator")
    body = service.execute(context, Call("message.get", {"id": message_id, "limit": 1}))
    assert body["ok"], body
    assert body["data"]["body"] == "B"
    attachment_ids, after = [], None
    while True:
        index_args = {"id": message_id, "limit": 1}
        if after is not None:
            index_args["after"] = after
        index = service.execute(context, Call("message.attachments", index_args))
        assert index["ok"], index
        assert len(index["data"]["items"]) <= 1
        attachment_ids.extend(item["id"] for item in index["data"]["items"])
        after = index["data"]["next_after"]
        if after is None:
            break
    assert len(set(attachment_ids)) == len(attachment_ids) == len(references)
    recovered_references, large_attachment_id = [], None
    for attachment_id in attachment_ids:
        pieces, offset = [], 0
        while True:
            part = service.execute(context, Call("message.attachment", {
                "id": attachment_id, "offset": offset, "limit": 32768,
            }))
            assert part["ok"], part
            pieces.append(part["data"]["reference"])
            offset = part["data"]["next_offset"]
            if offset is None:
                break
        recovered = json.loads("".join(pieces))
        assert recovered["path"] == str(attachment.resolve())
        recovered_references.append(recovered["source"])
        if recovered["source"] == reference:
            large_attachment_id = attachment_id
    assert {item["caption"] for item in recovered_references} == {item["caption"] for item in references}
    assert reference in recovered_references
    application._lifecycle(service, "active")
    other_context = identity.bind_native(service.store, {
        "harness": "codex", "native_session_id": str(uuid.uuid4()), "task": "unrelated",
    })["context"]
    assert service.execute(other_context, Call("identity.get", {}))["ok"]
    for operation, record_id in (("message.attachments", message_id),
                                 ("message.attachment", attachment_ids[0])):
        denied = service.execute(other_context, Call(operation, {"id": record_id}))
        assert not denied["ok"]
        assert denied["error"]["code"] == "NOT_FOUND"
        assert "data" not in denied

    class ServiceClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def call(self, operation, arguments, key):
            return service.execute(context, Call(operation, arguments, key))

    monkeypatch.setattr(config, "discover_workspace", lambda **kwargs: workspace)
    for command, record_id in (("attachments", message_id), ("attachment", large_attachment_id)):
        status = cli.main(["--operator", command, record_id, "--limit", "1" if command == "attachments" else "32768"],
                          client_factory=lambda *_args, **_kwargs: ServiceClient())
        assert status == 0
        output = json.loads(capsys.readouterr().out)
        assert output["ok"]
        assert len(json.dumps(output).encode("utf-8")) < MAX_FRAME

    async def exercise_mcp():
        server = create_server(ServiceClient())
        client_send, server_read = anyio.create_memory_object_stream(4)
        server_send, client_read = anyio.create_memory_object_stream(4)
        async with anyio.create_task_group() as group:
            group.start_soon(server.run, server_read, server_send, server.create_initialization_options())
            async with ClientSession(client_read, client_send) as session:
                await session.initialize()
                catalog = await session.list_tools()
                declared = {tool.name: tool for tool in catalog.tools}
                for name in ("attachments", "attachment"):
                    assert declared[name].annotations.read_only_hint is True
                reply = await session.call_tool("attachment", {"id": large_attachment_id, "limit": 32768})
                assert not reply.is_error
                assert len(reply.model_dump_json().encode("utf-8")) < MAX_FRAME
                envelope = json.loads(reply.content[0].text)
                assert envelope["ok"]
                assert envelope["data"]["next_offset"] is not None
            group.cancel_scope.cancel()

    anyio.run(exercise_mcp)


@pytest.mark.parametrize("action", ["verify", "import"])
def test_migration_failed_required_outcome_reaches_cli_exit_status(
    tmp_path, monkeypatch, capsys, action,
):
    from test_migrate import native_source

    source = tmp_path / "source.sqlite3"
    native_source(source, grant=action == "import")
    manifest = migrate.inspect_sources([migrate.SourceSpec(source, "/selected")])
    manifest_path = migrate.save_manifest(manifest, tmp_path / "manifest.json")
    workspace = SimpleNamespace(
        id=str(uuid.uuid4()), root=tmp_path, state_dir=tmp_path / "state",
        socket_path=tmp_path / "state/service.sock",
        database_path=tmp_path / "state/runtime.sqlite3",
    )
    monkeypatch.setattr(config, "discover_workspace", lambda **kwargs: workspace)
    arguments = ["migrate", action, "--manifest", str(manifest_path)]
    if action == "import":
        arguments.extend(["--run-id", "blocked-import"])
    status = cli.main(arguments)
    envelope = json.loads(capsys.readouterr().out)
    report = envelope.get("data") or envelope["error"]["details"]
    if action == "verify":
        assert report["valid"] is False
        assert report["failures"]
    else:
        assert report["state"] == "blocked"
        assert report["required_issues"] > 0
    assert envelope["ok"] is False
    assert status != 0
