"""Adapter recovery proves real Git publication without coordination admission."""
import json
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest
from mcp import ClientSession

from agentcoord import cli
from agentcoord.config import register_workspace
from agentcoord.core import CoordinationError
from agentcoord.mcp import create_server
from agentcoord.transport import Client, error_envelope


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args],
                          check=True, capture_output=True).stdout


@pytest.fixture
def repository(tmp_path, monkeypatch):
    for name in ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE", "AGENTCOORD_WORKSPACE"):
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setenv("AGENTCOORD_STATE_HOME", str(tmp_path / "state"))
    git(root, "init", "-q")
    for name, value in [("user.name", "Fallback test"), ("user.email", "test@example.invalid"),
                        ("core.hooksPath", "/dev/null"), ("commit.gpgsign", "false")]:
        git(root, "config", name, value)
    (root / "owned").write_text("before\n")
    (root / "peer").write_text("before\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "initial")
    (root / "owned").write_text("after\n")
    (root / "peer").write_text("staged peer\n")
    git(root, "add", "peer")
    with tempfile.TemporaryDirectory(prefix="ac-fb-", dir="/tmp") as socket_home:
        monkeypatch.setenv("AGENTCOORD_SOCKET_HOME", str(Path(socket_home).resolve()))
        yield root


def committed(root):
    assert git(root, "show", "HEAD:owned") == b"after\n"
    assert git(root, "show", "HEAD:peer") == b"before\n"
    assert git(root, "diff", "--cached", "--name-only") == b"peer\n"


def test_unregistered_cli_commits_without_native_identity(repository, capsys):
    status = cli.main(["--project", str(repository), "commit", "execute", "--paths", "owned",
                       "--message", "owned fallback", "--key", "unregistered"])
    captured = capsys.readouterr()
    reply = json.loads(captured.out)
    assert status == 0 and reply["data"]["mode"] == "local", reply
    assert "unregistered" in captured.err
    assert "Local Git result" in captured.err
    assert reply["diagnostics"]
    committed(repository)


@pytest.mark.parametrize("explicit", [False, True])
def test_registered_cli_missing_socket_or_explicit_local(repository, monkeypatch, capsys, explicit):
    register_workspace(repository)
    monkeypatch.setattr(cli, "native_context", lambda *a, **kw: {
        "harness": "codex", "native_session_id": "test-session"})
    argv = ["--project", str(repository), "commit", "execute", "--paths", "owned",
            "--message", "owned fallback", "--key", "registered"]
    if explicit:
        argv.append("--local")
        monkeypatch.setattr(cli, "native_context", lambda *a, **kw: pytest.fail("Local Git needs no binding"))
    status = cli.main(argv)
    captured = capsys.readouterr()
    reply = json.loads(captured.out)
    assert status == 0 and reply["data"]["mode"] == "local", reply
    assert "preserving peer staging" in captured.err
    committed(repository)


def test_mcp_automatic_fallback_and_explicit_replay_use_one_git_effect(repository):
    workspace = register_workspace(repository)
    client = Client(workspace.socket_path, {"harness": "codex", "native_session_id": "test"},
                    workspace_id=workspace.id)
    client.workspace = workspace

    async def exercise():
        server = create_server(client)
        send, read = anyio.create_memory_object_stream(4)
        server_send, client_read = anyio.create_memory_object_stream(4)
        async with anyio.create_task_group() as group:
            group.start_soon(server.run, read, server_send, server.create_initialization_options())
            async with ClientSession(client_read, send) as session:
                await session.initialize()
                args = {"paths": ["owned"], "message": "MCP fallback", "key": "mcp-fallback"}
                reply = await session.call_tool("commit_execute", args)
                envelope = json.loads(reply.content[0].text)
                assert not reply.is_error and envelope["data"]["mode"] == "local", envelope
                assert "not sent" in envelope["fallback_reason"]
                assert envelope["diagnostics"]
                head = git(repository, "rev-parse", "HEAD")
                replay = await session.call_tool("commit_execute", {**args, "local": True})
                assert not replay.is_error
                assert git(repository, "rev-parse", "HEAD") == head
            group.cancel_scope.cancel()

    try:
        anyio.run(exercise)
    finally:
        client.close()
    committed(repository)


@pytest.mark.parametrize("code", ["RECONCILIATION_REQUIRED", "NOT_AUTHORIZED", "STALE_GENERATION"])
def test_uncertain_or_refused_native_result_only_adds_guidance(repository, code):
    class Adapter:
        workspace = SimpleNamespace(root=repository, database_path=None)

        def call(self, *a, **kw):
            return error_envelope(code, "native publication or authority needs inspection")

    before = git(repository, "rev-parse", "HEAD")
    result = cli.invoke(Adapter(), cli.BY_TOOL["commit_execute"],
                        {"paths": ["owned"], "message": "reviewed", "key": "native"})
    assert result["error"]["code"] == code
    if code == "RECONCILIATION_REQUIRED":
        assert "--local" in result["error"]["details"]["local_fallback"]["command"]
    else:
        assert "local_fallback" not in result["error"].get("details", {})
    assert git(repository, "rev-parse", "HEAD") == before


def test_missing_binding_exception_uses_local_git_but_prior_uncertainty_does_not(repository):
    class Adapter:
        workspace = SimpleNamespace(root=repository, database_path=None)

        def __init__(self):
            self._uncertain_keys = {"prior"}

        def call(self, *a, **kw):
            raise CoordinationError("UNBOUND_ACTOR", "Native binding absent")

    before = git(repository, "rev-parse", "HEAD")
    uncertain = cli.invoke(Adapter(), cli.BY_TOOL["commit_execute"],
                           {"paths": ["owned"], "message": "reviewed", "key": "prior"})
    assert not uncertain["ok"]
    assert git(repository, "rev-parse", "HEAD") == before
    result = cli.invoke(Adapter(), cli.BY_TOOL["commit_execute"],
                        {"paths": ["owned"], "message": "reviewed", "key": "fresh"})
    assert result["ok"], result
    committed(repository)
