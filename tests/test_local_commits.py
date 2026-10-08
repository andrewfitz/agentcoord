"""Local retry safety uses real Git effects and preserves unrelated staging."""
import hashlib
import json
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest
from test_commits import (  # noqa: F401 -- reusable real native fixtures
    native_service,
    repo,
)

from agentcoord import local_commits


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True).stdout


@pytest.fixture
def workspace(tmp_path):
    git(tmp_path, "init", "-q")
    for name, value in [("user.name", "Local test"), ("user.email", "local@example.invalid"), ("core.hooksPath", "/dev/null"), ("commit.gpgsign", "false")]:
        git(tmp_path, "config", name, value)
    (tmp_path / "owned").write_text("before\n")
    (tmp_path / "peer").write_text("peer before\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "initial")
    (tmp_path / "owned").write_text("after\n")
    (tmp_path / "peer").write_text("peer staged\n")
    git(tmp_path, "add", "peer")
    return SimpleNamespace(root=tmp_path, database_path=tmp_path / "absent.sqlite")


def arguments(**changes):
    return {"paths": ["owned"], "message": "local reviewed commit", **changes}


def test_local_commit_preserves_staging_and_replays_receipt(workspace):
    messages = []
    reply = local_commits.execute(workspace, arguments(), "stable", report=messages.append)
    assert reply["ok"], reply
    assert reply["data"]["state"] == "succeeded"
    assert reply["data"]["receipt"] == reply["data"]["result"]["receipt"]
    assert reply["data"]["receipt"].endswith(".json")
    assert git(workspace.root, "show", "HEAD:owned") == b"after\n"
    assert git(workspace.root, "show", "HEAD:peer") == b"peer before\n"
    assert git(workspace.root, "show", ":peer") == b"peer staged\n"
    count = git(workspace.root, "rev-list", "--count", "HEAD")
    assert local_commits.execute(workspace, arguments(), "stable") == reply
    assert git(workspace.root, "rev-list", "--count", "HEAD") == count
    assert any("publishing" in message for message in messages)
    changed = local_commits.execute(workspace, arguments(message="changed"), "stable")
    assert changed["error"]["code"] == "IDEMPOTENCY_CONFLICT"


@pytest.mark.parametrize("stop_phase", ["publishing", "published"])
def test_interrupted_receipt_never_repeats_effect(workspace, monkeypatch, stop_phase):
    original = local_commits._write

    def crash(path, state):
        original(path, state)
        if state["phase"] == stop_phase:
            raise KeyboardInterrupt("simulated process stop after durable intent")

    monkeypatch.setattr(local_commits, "_write", crash)
    with pytest.raises(KeyboardInterrupt):
        local_commits.execute(workspace, arguments(), "crash-key")
    monkeypatch.setattr(local_commits, "_write", original)
    before = git(workspace.root, "rev-parse", "HEAD")
    reply = local_commits.execute(workspace, arguments(), "crash-key")
    assert reply["error"]["code"] == "RECONCILIATION_REQUIRED"
    assert git(workspace.root, "rev-parse", "HEAD") == before


def native_db(workspace, *, state="queued", key="native-key", result=None, overlap=None):
    with sqlite3.connect(workspace.database_path) as db:
        db.executescript("CREATE TABLE operations(id TEXT,state TEXT,result_json TEXT,kind TEXT,retry_key TEXT,arguments_json TEXT); CREATE TABLE commit_admissions(id TEXT,state TEXT); CREATE TABLE commit_paths(admission_id TEXT,path TEXT);")
        db.execute("INSERT INTO operations VALUES ('native',?,?, 'commit.execute',?,?)", (state, json.dumps(result) if result else None, key, json.dumps(arguments())))
        if overlap:
            db.execute("INSERT INTO commit_admissions VALUES ('owner','pending')")
            db.execute("INSERT INTO commit_paths VALUES ('owner',?)", (overlap,))


def test_native_same_key_and_exact_overlap_are_real_git_risks(workspace):
    native_db(workspace, overlap="owned")
    before = git(workspace.root, "rev-parse", "HEAD")
    assert local_commits.execute(workspace, arguments(), "native-key")["error"]["code"] == "RECONCILIATION_REQUIRED"
    assert local_commits.execute(workspace, arguments(), "different-key")["error"]["code"] == "RECONCILIATION_REQUIRED"
    assert git(workspace.root, "rev-parse", "HEAD") == before


def test_native_succeeded_receipt_is_reused(workspace):
    head = git(workspace.root, "rev-parse", "HEAD").decode().strip()
    native_db(workspace, state="succeeded", result={"committed": True, "commit": head})
    reply = local_commits.execute(workspace, arguments(), "native-key")
    assert reply["ok"] and reply["data"]["mode"] == "native-recovered"
    assert reply["data"]["result"]["commit"] == head
    assert git(workspace.root, "rev-parse", "HEAD").decode().strip() == head


def test_unreadable_coordination_state_warns_once_and_preserves_git_checks(workspace):
    workspace.database_path.write_bytes(b"not sqlite")
    messages = []
    reply = local_commits.execute(workspace, arguments(), "new-key", report=messages.append)
    assert reply["ok"], reply
    assert sum("could not be read" in message for message in messages) == 1


def test_symlink_local_receipt_is_rejected_without_git_effect(workspace):
    directory = workspace.root / ".git" / "agentcoord-local-commits"
    directory.mkdir(mode=0o700)
    receipt = directory / (hashlib.sha256(b"unsafe").hexdigest() + ".json")
    receipt.symlink_to(workspace.root / "owned")
    before = git(workspace.root, "rev-parse", "HEAD")
    assert not local_commits.execute(workspace, arguments(), "unsafe")["ok"]
    assert git(workspace.root, "rev-parse", "HEAD") == before


def test_local_patch_preserves_peer_staging(workspace):
    head = git(workspace.root, "rev-parse", "HEAD").decode().strip()
    patch = git(workspace.root, "diff", "--", "owned").decode()
    reply = local_commits.execute(workspace, arguments(patch=patch, base_commit=head), "patch-key")
    assert reply["ok"], reply
    assert reply["data"]["result"]["selection"] == "patch"
    assert git(workspace.root, "show", "HEAD:owned") == b"after\n"
    assert git(workspace.root, "show", ":peer") == b"peer staged\n"


def test_local_version_synthesis_replays_without_second_bump(workspace, monkeypatch):
    version = workspace.root / "version.txt"
    version.write_text("version = 1.2.3\n")
    git(workspace.root, "add", "version.txt")
    git(workspace.root, "commit", "-qm", "version baseline")
    # The fixture's peer staging was included above; stage a new peer edit.
    (workspace.root / "peer").write_text("new peer staging\n")
    git(workspace.root, "add", "peer")
    rule = SimpleNamespace(path="version.txt", match=r"^version = (?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)$", replacement="version = {major}.{minor}.{patch}", increment="patch", validate=r"version = \d+\.\d+\.\d+")
    monkeypatch.setattr(local_commits, "load_config", lambda workspace: SimpleNamespace(version=rule))
    args = arguments(bump_version=True)
    reply = local_commits.execute(workspace, args, "bump-key")
    assert reply["ok"], reply
    assert version.read_text() == "version = 1.2.4\n"
    assert local_commits.execute(workspace, args, "bump-key") == reply
    assert version.read_text() == "version = 1.2.4\n"
    assert git(workspace.root, "show", ":peer") == b"new peer staging\n"


def test_native_completed_key_cannot_adopt_different_reviewed_content(workspace):
    head = git(workspace.root, "rev-parse", "HEAD").decode().strip()
    native_db(workspace, state="succeeded", result={"committed": True, "commit": head})
    reply = local_commits.execute(workspace, arguments(message="different"), "native-key")
    assert reply["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_git_override_fails_before_local_receipt_creation(workspace, monkeypatch):
    monkeypatch.setenv("GIT_INDEX_FILE", str(workspace.root / "alternate-index"))
    reply = local_commits.execute(workspace, arguments(), "override")
    assert not reply["ok"]
    assert not (workspace.root / ".git" / "agentcoord-local-commits").exists()


def test_failure_saving_final_receipt_never_reclassifies_published_commit(workspace, monkeypatch):
    original = local_commits._write

    def fail_final(path, state):
        if state["phase"] == "finished":
            raise OSError("receipt publication unavailable")
        original(path, state)

    monkeypatch.setattr(local_commits, "_write", fail_final)
    reply = local_commits.execute(workspace, arguments(), "late-failure")
    assert reply["error"]["code"] == "RECONCILIATION_REQUIRED"
    head = git(workspace.root, "rev-parse", "HEAD")
    assert git(workspace.root, "show", "HEAD:owned") == b"after\n"
    monkeypatch.setattr(local_commits, "_write", original)
    retry = local_commits.execute(workspace, arguments(), "late-failure")
    assert retry["error"]["code"] == "RECONCILIATION_REQUIRED"
    assert git(workspace.root, "rev-parse", "HEAD") == head


def test_proven_rejected_native_attempt_does_not_block_local_publication(workspace):
    native_db(workspace, state="failed")
    with sqlite3.connect(workspace.database_path) as db:
        db.execute("CREATE TABLE commit_execution(operation_id TEXT,published_commit TEXT,prepared_json TEXT)")
        db.execute("INSERT INTO commit_execution VALUES ('native',NULL,?)", (json.dumps({"worker_finished": True, "phase": "selected"}),))
    reply = local_commits.execute(workspace, arguments(), "native-key")
    assert reply["ok"], reply
    assert git(workspace.root, "show", "HEAD:owned") == b"after\n"


def test_unregistered_local_workspace_needs_no_coordination_database(workspace):
    workspace.database_path = None
    reply = local_commits.execute(workspace, arguments(), "unregistered")
    assert reply["ok"], reply
    assert git(workspace.root, "show", "HEAD:owned") == b"after\n"


def test_native_receipt_boolean_defaults_match_omitted_arguments(workspace):
    head = git(workspace.root, "rev-parse", "HEAD").decode().strip()
    native_db(workspace, state="succeeded", result={"committed": True, "commit": head})
    reply = local_commits.execute(workspace, arguments(bump_version=False, adopt_staged=False), "native-key")
    assert reply["ok"], reply
    assert reply["data"]["mode"] == "native-recovered"


def test_synthetic_version_does_not_admit_overlap_for_disjoint_owned_files(workspace, monkeypatch):
    version = workspace.root / "version.txt"
    version.write_text("version = 1.2.3\n")
    git(workspace.root, "add", "version.txt")
    git(workspace.root, "commit", "-qm", "version baseline")
    rule = SimpleNamespace(path="version.txt", match=r"^version = (?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)$", replacement="version = {major}.{minor}.{patch}", increment="patch", validate=r"version = \d+\.\d+\.\d+")
    monkeypatch.setattr(local_commits, "load_config", lambda workspace: SimpleNamespace(version=rule))
    native_db(workspace, overlap="version.txt")
    reply = local_commits.execute(workspace, arguments(bump_version=True), "disjoint")
    assert reply["ok"], reply
    assert version.read_text() == "version = 1.2.4\n"
    # Explicit authored selection retains the exact-path overlap check.
    version.write_text("version = 2.0.0\n")
    authored = local_commits.execute(workspace, arguments(paths=["version.txt"]), "authored-version")
    assert authored["error"]["code"] == "RECONCILIATION_REQUIRED"


def test_real_native_user_key_follows_idempotency_to_internal_operation(native_service):  # noqa: F811 -- pytest fixture
    from agentcoord.core import Call

    service, context = native_service
    workspace = SimpleNamespace(root=service.workspace.root, database_path=service.store.path)
    args = {"paths": ["owned.txt"], "message": "real native reviewed selection"}
    accepted = service.execute(context, Call("commit.execute", args, "external-user-key"))
    assert accepted["ok"], accepted
    operation_id = accepted["data"]["operation_id"]
    with service.store.read() as tx:
        internal_key = tx.connection.execute("SELECT retry_key FROM operations WHERE id=?", (operation_id,)).fetchone()[0]
        assert internal_key != "external-user-key"
    pending = local_commits.execute(workspace, args, "external-user-key")
    assert pending["error"]["code"] == "RECONCILIATION_REQUIRED"
    completed = service.run_operation(operation_id)
    assert completed["state"] == "succeeded", completed
    before = git(workspace.root, "rev-parse", "HEAD")
    recovered = local_commits.execute(workspace, args, "external-user-key")
    assert recovered["ok"], recovered
    assert recovered["data"]["mode"] == "native-recovered"
    assert recovered["data"]["result"]["commit"] == before.decode().strip()
    assert git(workspace.root, "rev-parse", "HEAD") == before


def test_pending_native_user_key_cannot_switch_to_disjoint_local_selection(workspace):
    native_db(workspace, key="internal")
    with sqlite3.connect(workspace.database_path) as db:
        db.execute("CREATE TABLE idempotency(operation TEXT,retry_key TEXT,result_json TEXT)")
        db.execute("INSERT INTO idempotency VALUES ('commit.execute','external',?)", (json.dumps({"admission_id": "pending-original", "granted": False}),))
    reply = local_commits.execute(workspace, arguments(), "external")
    assert reply["error"]["code"] == "RECONCILIATION_REQUIRED"
    assert git(workspace.root, "show", "HEAD:owned") == b"before\n"


def test_real_precommit_hook_rejection_is_failed_and_preserves_peer_staging(workspace):
    hooks = workspace.root / ".git" / "hooks"
    hook = hooks / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 7\n")
    hook.chmod(0o700)
    git(workspace.root, "config", "core.hooksPath", str(hooks))
    before = git(workspace.root, "rev-parse", "HEAD")
    reply = local_commits.execute(workspace, arguments(), "hook-key")
    assert reply["ok"] is False
    assert reply["error"]["code"] == "GIT_HOOK_FAILED"
    assert reply["error"]["details"]["exit_code"] == 7
    assert reply["data"]["state"] == "failed"
    assert reply["data"]["result"]["committed"] is False
    assert git(workspace.root, "rev-parse", "HEAD") == before
    assert git(workspace.root, "show", ":peer") == b"peer staged\n"
    assert local_commits.execute(workspace, arguments(), "hook-key") == reply


def test_postpublication_error_retains_commit_and_never_repeats(workspace, monkeypatch):
    original = local_commits._write
    failed = False

    def fail_published_once(path, state):
        nonlocal failed
        if state["phase"] == "published" and not failed:
            failed = True
            raise OSError("publication receipt storage failed")
        original(path, state)

    monkeypatch.setattr(local_commits, "_write", fail_published_once)
    reply = local_commits.execute(workspace, arguments(), "postpublication-key")
    assert reply["ok"] is False
    assert reply["error"]["code"] == "RECONCILIATION_REQUIRED"
    assert reply["error"]["details"]["publication"] == "published"
    result = reply["data"]["result"]
    assert result["committed"] and result["code"] == 2
    assert result["commit"] == git(workspace.root, "rev-parse", "HEAD").decode().strip()
    assert result["receipt"] == reply["data"]["receipt"]
    assert local_commits.execute(workspace, arguments(), "postpublication-key") == reply
    assert git(workspace.root, "rev-list", "--count", "HEAD") == b"2\n"
