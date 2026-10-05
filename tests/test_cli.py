"""Native evidence isolation and usable literal-file command adapters."""

import json
from types import SimpleNamespace

import pytest

from agentcoord.cli import (
    BY_TOOL,
    hook,
    invoke,
    native_context,
    parser,
    prepare_patch_file,
    validate_arguments,
    wait_operation,
)
from agentcoord.core import CoordinationError

MESSAGE_ID = "36f07549-04fb-4f83-b43a-b326ae64983b"


def test_selected_harness_never_borrows_another_native_session():
    env = {
        "CODEX_THREAD_ID": "codex-session",
        "CLAUDE_SESSION_ID": "claude-session",
        "HERDR_PANE_ID": "pane",
    }
    assert native_context("claude", env)["native_session_id"] == "claude-session"
    with pytest.raises(CoordinationError):
        native_context("cursor", env)
    with pytest.raises(CoordinationError):
        native_context(None, env)
    assert "pane" not in native_context("claude", env)


def test_installed_mcp_and_hook_candidate_commands_parse(tmp_path):
    args = parser().parse_args(["--project", str(tmp_path), "mcp", "--harness", "grok"])
    assert args.harness == "grok" and args.maintenance == "mcp"
    args = parser().parse_args(["--project", str(tmp_path), "hook", "claude", "start"])
    assert args.harness == "claude" and args.event == "start"


def test_literal_file_inputs_do_not_require_inline_message(tmp_path):
    message = tmp_path / "message.txt"
    message.write_text("Subject\n\nLiteral `value` and $(no shell)\n")
    args = parser().parse_args(
        ["commit", "execute", "--paths", "src/file.py", "--message-file", str(message)]
    )
    assert args.message_file == message
    assert args.spec.operation == "commit.execute"


def test_mutation_retry_key_is_separate_from_domain_arguments():
    class Client:
        def call(self, operation, arguments, key):
            self.call_value = operation, arguments, key
            return {"ok": True}

    client = Client()
    invoke(client, BY_TOOL["consume"], {"id": MESSAGE_ID, "key": "retained"})
    assert client.call_value == ("message.consume", {"id": MESSAGE_ID}, "retained")
    with pytest.raises(CoordinationError):
        invoke(client, BY_TOOL["consume"], {"id": "message", "actor_id": "other"})


def test_bool_is_not_an_integer_and_missing_fields_fail_explicitly():
    with pytest.raises(CoordinationError):
        validate_arguments(BY_TOOL["message"], {"id": "message", "offset": True})
    with pytest.raises(CoordinationError):
        validate_arguments(BY_TOOL["request"], {"recipient_id": "other"})


def test_retry_receipt_key_remains_a_domain_read_argument():
    class Client:
        def call(self, operation, arguments, key):
            assert (operation, arguments, key) == ("receipt.get", {"key": "original"}, None)
            return {"ok": True}

    assert invoke(Client(), BY_TOOL["receipt"], {"key": "original"})["ok"]


def test_cli_wait_returns_terminal_failure_without_resubmitting():
    class Client:
        def __init__(self):
            self.calls = []

        def call(self, operation, arguments):
            self.calls.append((operation, arguments))
            state = "running" if len(self.calls) == 1 else "failed"
            return {
                "ok": True,
                "action_digest": {"items": [{"id": "incoming"}]},
                "data": {
                    "state": state,
                    "error": {"code": "OPERATION_FAILED", "message": "Git conflict"},
                },
            }

    client = Client()
    reply = wait_operation(
        client, {"ok": True, "data": {"operation_id": "retained"}}, sleep=lambda _: None
    )
    assert not reply["ok"] and reply["error"]["details"]["operation_id"] == "retained"
    assert reply["action_digest"]["items"] == [{"id": "incoming"}]
    assert client.calls == [("operation.get", {"operation_id": "retained"})] * 2


def test_wait_rejects_nonfinite_timeout_before_polling():
    class Client:
        def call(self, *_):
            raise AssertionError("Invalid timeout must not enter the polling loop")

    with pytest.raises(CoordinationError):
        wait_operation(
            Client(), {"ok": True, "data": {"operation_id": "accepted"}}, timeout=float("nan")
        )


def test_binary_patch_artifact_keeps_hash_and_rejects_symlink(tmp_path):
    import hashlib

    data = b"diff --git a/x b/x\nGIT binary patch\n\xff\x00\n"
    patch = tmp_path / "selected.patch"
    patch.write_bytes(data)
    workspace = SimpleNamespace(root=tmp_path)
    assert prepare_patch_file(workspace, patch) == {
        "patch_file": "selected.patch",
        "patch_sha256": hashlib.sha256(data).hexdigest(),
    }
    (tmp_path / "link.patch").symlink_to(patch)
    with pytest.raises(CoordinationError):
        prepare_patch_file(workspace, "link.patch")


def test_uncorrelated_hook_never_copies_binding_generation(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "agentcoord.identity.native_context",
        lambda harness, payload, **options: {"harness": harness, "native_session_id": "actual"},
    )
    monkeypatch.setattr(
        "agentcoord.config.load_config", lambda workspace: SimpleNamespace(native_executables={})
    )

    class Client:
        def __init__(self, path, native, **options):
            assert native == {"harness": "codex", "native_session_id": "actual"}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def call(self, operation, arguments, key):
            assert operation == "identity.event"
            assert arguments == {"event": "stop", "state": "idle"}
            return {"ok": True, "data": {"applied": False}}

    from agentcoord.config import register_workspace

    workspace = register_workspace(tmp_path, state_root=tmp_path / "state")
    reply = hook(
        workspace, "codex", "stop", payload={"session_id": "actual", "cwd": str(tmp_path)},
        client_factory=Client,
    )
    assert reply["data"]["applied"] is False


@pytest.fixture
def lifecycle_workspace(tmp_path, monkeypatch):
    from agentcoord.config import daemon_environment, register_workspace

    root = tmp_path / "workspace"
    root.mkdir()
    workspace = register_workspace(root, state_root=tmp_path / "state")
    for key, value in daemon_environment(workspace).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        "agentcoord.identity.native_context",
        lambda harness, payload, **options: {"harness": harness, "native_session_id": "actual"},
    )
    return workspace


@pytest.mark.parametrize("harness", ["claude", "codex", "cursor", "grok"])
@pytest.mark.parametrize("event", ["start", "stop", "end", "failure"])
def test_lifecycle_cli_success_obeys_native_hook_output_contract(
    lifecycle_workspace, monkeypatch, capsys, harness, event
):
    from agentcoord import cli

    result = {"ok": True, "protocol": 1, "request_id": "lifecycle-event",
              "data": {"applied": True}, "action_digest": None}
    monkeypatch.setattr(cli, "_maintenance", lambda *_: result)
    assert cli.main(["--project", str(lifecycle_workspace.root), "hook", harness, event]) == 0
    captured = capsys.readouterr()
    assert captured.out == ("{}\n" if harness == "cursor" else "")
    assert captured.err == ""


@pytest.mark.parametrize("harness", ["claude", "codex", "cursor", "grok"])
@pytest.mark.parametrize("failure", ["returned", "raised", "missing-result"])
def test_lifecycle_cli_failures_stay_explicit_on_stderr(
    lifecycle_workspace, monkeypatch, capsys, harness, failure
):
    from agentcoord import cli
    from agentcoord.transport import error_envelope

    def failed_hook(*_):
        if failure == "raised":
            raise CoordinationError("UNBOUND_ACTOR", "Native session could not be bound")
        if failure == "returned":
            return error_envelope("UNBOUND_ACTOR", "Native session could not be bound")
        return None

    monkeypatch.setattr(cli, "_maintenance", failed_hook)
    assert cli.main(["--project", str(lifecycle_workspace.root), "hook", harness, "start"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    code = "INVALID_RESPONSE" if failure == "missing-result" else "UNBOUND_ACTOR"
    assert code in captured.err
    if failure != "missing-result":
        assert "Native session could not be bound" in captured.err


def test_lifecycle_cli_failure_diagnostic_is_bounded(lifecycle_workspace, monkeypatch, capsys):
    from agentcoord import cli
    from agentcoord.transport import error_envelope

    result = error_envelope("INVALID_ARGUMENT", "bad lifecycle input " + "x" * 10000)
    monkeypatch.setattr(cli, "_maintenance", lambda *_: result)
    assert cli.main(["--project", str(lifecycle_workspace.root), "hook", "codex", "stop"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and len(captured.err) == 4097
    assert "INVALID_ARGUMENT: bad lifecycle input" in captured.err


@pytest.mark.parametrize("ok", [True, False])
def test_ordinary_cli_retains_full_json_envelope(lifecycle_workspace, monkeypatch, capsys, ok):
    from agentcoord import cli
    from agentcoord.transport import error_envelope

    result = ({"ok": True, "protocol": 1, "data": {"valid": True}}
              if ok else error_envelope("OPERATION_FAILED", "Doctor failed"))
    monkeypatch.setattr(cli, "_maintenance", lambda *_: result)
    assert cli.main(["--project", str(lifecycle_workspace.root), "doctor"]) == (0 if ok else 1)
    captured = capsys.readouterr()
    assert json.loads(captured.out) == result and captured.err == ""


@pytest.mark.parametrize("root", ["missing", None, "", " ", 0, {}, [], "relative/path", "\x00"])
def test_hook_rejects_missing_or_invalid_workspace_before_connect(lifecycle_workspace, root):
    payload = {"session_id": "actual"}
    if root != "missing":
        payload["cwd"] = root

    def forbidden_client(*args, **options):
        pytest.fail("Rejected lifecycle event constructed a client")

    reply = hook(lifecycle_workspace, "codex", "start", payload=payload,
                 client_factory=forbidden_client)
    assert reply["ok"] and reply["data"]["applied"] is False


@pytest.mark.parametrize("location", ["foreign_sibling", "nested_registered", "conflicting_paths"])
def test_hook_rejects_other_workspace_before_connect(lifecycle_workspace, tmp_path, location):
    from agentcoord.config import register_workspace

    workspace = lifecycle_workspace
    other = workspace.root / "nested" if location == "nested_registered" else tmp_path / "other"
    other.mkdir()
    register_workspace(other, state_root=workspace.state_root)
    payload = {"session_id": "actual", "cwd": str(other / "subdirectory")}
    if location == "conflicting_paths":
        payload = {"session_id": "actual", "cwd": str(workspace.root), "workspace_root": str(other)}

    def forbidden_client(*args, **options):
        pytest.fail("Foreign lifecycle event constructed a client")

    reply = hook(workspace, "codex", "start", payload=payload, client_factory=forbidden_client)
    assert reply["ok"] and reply["data"]["applied"] is False


@pytest.mark.parametrize("field", ["cwd", "workspace_root", "project_dir"])
def test_hook_accepts_workspace_subdirectory_using_selected_registry(lifecycle_workspace, tmp_path,
                                                                    monkeypatch, field):
    from agentcoord.config import register_workspace

    workspace = lifecycle_workspace
    # Ambient routing is not evidence of the hook event's originating workspace.
    foreign_state = tmp_path / "foreign-state"
    register_workspace(workspace.root, state_root=foreign_state)
    monkeypatch.setenv("AGENTCOORD_STATE_HOME", str(foreign_state))
    constructed = []

    class Client:
        def __init__(self, path, native, **options):
            constructed.append(path)
            assert path == workspace.socket_path and options["workspace_id"] == workspace.id

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def call(self, operation, arguments, key):
            assert operation == "identity.event" and arguments == {"event": "start", "state": "working"}
            return {"ok": True, "data": {"applied": True}}

    payload = {"session_id": "actual", field: str(workspace.root / "src" / "domain")}
    reply = hook(workspace, "codex", "start", payload=payload, client_factory=Client)
    assert reply["data"]["applied"] is True and constructed == [workspace.socket_path]


def test_final_workflow_fields_and_structured_evidence_are_usable():
    args = parser().parse_args(
        [
            "ready",
            "--artifact",
            "build",
            "--paths",
            "src/x.py",
            "--evidence-json",
            '{"checks":[true,3]}',
            "--status",
            "verified",
        ]
    )
    assert args.evidence_json == {"checks": [True, 3]}
    validate_arguments(
        args.spec,
        {
            "artifact": args.artifact,
            "paths": args.paths,
            "evidence": args.evidence_json,
            "status": args.status,
        },
    )
    args = parser().parse_args(
        [
            "request-resolve",
            MESSAGE_ID,
            "--state",
            "answered",
            "--response",
            "done",
            "--external-settlement",
            '{"answered_by":"' + MESSAGE_ID + '","evidence":"external record"}',
        ]
    )
    validate_arguments(
        args.spec,
        {
            "id": args.id,
            "state": args.state,
            "response": args.response,
            "external_settlement": args.external_settlement,
        },
    )
    with pytest.raises(CoordinationError):
        validate_arguments(
            args.spec,
            {
                "id": MESSAGE_ID,
                "state": "answered",
                "response": "done",
                "external_settlement": {"answered_by": MESSAGE_ID},
            },
        )
    with pytest.raises(CoordinationError):
        validate_arguments(
            BY_TOOL["ready"], {"artifact": "build", "paths": ["a", "a"], "evidence": {}}
        )
    with pytest.raises(CoordinationError):
        validate_arguments(
            BY_TOOL["ready"], {"artifact": "build", "paths": ["a"], "evidence": float("nan")}
        )


@pytest.fixture
def originating_service(tmp_path, monkeypatch):
    import os

    from agentcoord import identity
    from agentcoord.application import build_service
    from agentcoord.config import daemon_environment, register_workspace
    from agentcoord.core import Call

    service = build_service(register_workspace(tmp_path, state_root=tmp_path / "state"))
    for key, value in daemon_environment(service.workspace).items():
        monkeypatch.setenv(key, value)
    process = identity._process_info(os.getpid())
    proof = {key: process[key] for key in ("pid", "pgid", "started", "source")}
    native = {"harness": "codex", "native_session_id": "origin-native", "process_identity": proof}
    binding = identity.bind_native(service.store, native, transport="hook")
    started = service.execute(binding["context"], Call("identity.event", {
        "event": "start", "state": "working", "native_run_id": "origin-run",
    }, "start-origin"))
    assert started["ok"], started
    context = identity.context_from_token(service.store, binding["token"])
    actor = service.execute(context, Call("identity.get", {}))["data"]["actor"]
    origin = {"workspace_id": service.workspace.id, "actor_id": actor["id"],
              "task_generation": actor["current_task_generation"],
              "execution_generation": actor["current_execution_generation"]}
    # Independent native evidence is fixed before any origin snapshot is passed.
    monkeypatch.setattr("agentcoord.cli.native_context", lambda *args, **kwargs: dict(native))
    return service, context, origin


def test_identity_snapshot_includes_workspace_id(originating_service):
    from agentcoord.core import Call

    service, context, origin = originating_service
    assert service.execute(context, Call("identity.get", {}))["data"]["workspace_id"] == origin["workspace_id"]


@pytest.mark.parametrize("change", ["same", "task", "execution", "workspace", "actor", "task_after_identity"])
def test_cli_origin_context_guards_real_git_admission(originating_service, capsys, change):
    import json
    import uuid

    from test_application import running

    from agentcoord import identity
    from agentcoord.cli import main
    from agentcoord.transport import Client

    service, context, original = originating_service
    origin = dict(original)
    if change == "task":
        with service.store.write() as tx:
            identity.assign_task(tx, context, "later task")
    elif change == "execution":
        with service.store.write() as tx:
            identity.start_execution(tx, context, native_run_id="later-run")
    elif change in {"workspace", "actor"}:
        origin[change + "_id"] = str(uuid.uuid4())

    class RacingClient(Client):
        def call(self, operation, *args, **kwargs):
            reply = super().call(operation, *args, **kwargs)
            if operation == "identity.get" and change == "task_after_identity":
                live_context = identity.bind_native(service.store, {
                    "harness": "codex", "native_session_id": "origin-native",
                })["context"]
                with service.store.write() as tx:
                    identity.assign_task(tx, live_context, "later task after lookup")
            return reply

    with running(service):
        status = main(["--project", str(service.workspace.root), "--origin-context", json.dumps(origin),
                       "commit", "acquire", "--key", "origin-acquire"], client_factory=RacingClient)
        reply = json.loads(capsys.readouterr().out)
        with service.store.read() as tx:
            count = tx.connection.execute("SELECT COUNT(*) FROM commit_grants").fetchone()[0]
        if change == "same":
            assert status == 0 and reply["ok"] and reply["data"]["held_by_you"], reply
            assert count == 1
            with Client(service.workspace.socket_path, {"harness": "codex", "native_session_id": "origin-native"},
                        workspace_id=service.workspace.id) as client:
                released = client.call("commit.release", {"grant_id": reply["data"]["grant_id"]}, key="cleanup")
                assert released["ok"], released
        else:
            assert status == 1 and not reply["ok"], reply
            expected = "WRONG_WORKSPACE" if change == "workspace" else "NOT_AUTHORIZED" if change == "actor" else "STALE_GENERATION"
            assert reply["error"]["code"] == expected, reply
            assert count == 0


@pytest.mark.parametrize("change", ["extra", "missing", "null_execution", "invalid_uuid"])
def test_origin_context_invalid_snapshot_fails_before_binding(originating_service, capsys, change):
    import json

    from agentcoord.cli import main

    service, _, original = originating_service
    origin = dict(original)
    if change == "extra":
        origin["native_session_id"] = "never-authentication"
    elif change == "missing":
        del origin["actor_id"]
    elif change == "null_execution":
        origin["execution_generation"] = None
    else:
        origin["task_generation"] = "not-a-uuid"

    def forbidden_client(*args, **kwargs):
        pytest.fail("Invalid origin snapshot attempted native binding")

    status = main(["--project", str(service.workspace.root), "--origin-context", json.dumps(origin),
                   "commit", "acquire"], client_factory=forbidden_client)
    reply = json.loads(capsys.readouterr().out)
    assert status == 1 and reply["error"]["code"] == "INVALID_ARGUMENT", reply
