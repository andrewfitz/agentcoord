"""Disposable real Git repositories verify selection, publication and recovery."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentcoord import commits


def git(root, *arguments, data=None):
    executable = shutil.which("git")
    assert executable, "Disposable repository checks require Git"
    return subprocess.run(
        [executable, "-C", str(root), *arguments],
        input=data,
        capture_output=True,
        check=True,
    ).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for name in ("GIT_INDEX_FILE", "GIT_DIR", "GIT_WORK_TREE"):
        monkeypatch.delenv(name, raising=False)
    git(tmp_path, "init", "-q")
    for key, value in [
        ("user.name", "Fixture"),
        ("user.email", "fixture@example.invalid"),
        ("core.hooksPath", "/dev/null"),
        ("commit.gpgsign", "false"),
    ]:
        git(tmp_path, "config", key, value)
    (tmp_path / "owned.txt").write_text("before\n")
    (tmp_path / "peer.txt").write_text("preserve\n")
    (tmp_path / "version.txt").write_text("version = 1.2.3\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "initial")
    (tmp_path / "owned.txt").write_text("after\n")
    return tmp_path


def version_rule(**changes):
    values = {
        "path": "version.txt",
        "match": r"^version = (?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)$",
        "replacement": "version = {major}.{minor}.{patch}",
        "increment": "patch",
        "validate": r"version = \d+\.\d+\.\d+",
    }
    return SimpleNamespace(**dict(values, **changes))


class Control:
    def __init__(self, *, granted=True, on_status=None, release_error=False):
        self.granted, self.on_status, self.release_error = (
            granted,
            on_status,
            release_error,
        )
        self.grant_id = str(uuid.uuid4())
        self.released = []

    def reserve(self):
        return {"granted": self.granted, "grant_id": self.grant_id}

    def status(self):
        if self.on_status:
            self.on_status()
        return {"held_by_you": True, "grant_id": self.grant_id}

    def check_wait(self, grant_id):
        assert self.granted and grant_id == self.grant_id

    def release(self, grant_id):
        self.released.append(grant_id)
        if self.release_error:
            raise ValueError("release transport unavailable")
        return {"released": True, "grant_id": grant_id}


def execute(
    repo, *, paths=None, control=None, progress=None, bump=False, rule=None, **options
):
    return commits._execute_git(
        repo,
        paths or ["owned.txt"],
        "reviewed change",
        bump,
        control or Control(),
        progress or (lambda phase, values: None),
        rule if rule is not None else version_rule(),
        **options,
    )


def test_exact_selection_preserves_peer_staging_and_synthesizes_version_once(repo):
    (repo / "peer.txt").write_text("peer staged\n")
    git(repo, "add", "peer.txt")
    control = Control()
    result = execute(repo, control=control, bump=True)
    assert result["committed"] and result["reviewed_tree_matches"]
    assert git(repo, "show", "HEAD:owned.txt") == b"after\n"
    assert git(repo, "show", "HEAD:peer.txt") == b"preserve\n"
    assert git(repo, "show", ":peer.txt") == b"peer staged\n"
    assert git(repo, "diff", "--cached", "--name-only") == b"peer.txt\n"
    assert (repo / "version.txt").read_text() == "version = 1.2.4\n"
    assert control.released == [control.grant_id]


def test_already_committed_selected_work_cannot_repeat_as_version_only_commit(repo):
    execute(repo, bump=True)
    head = git(repo, "rev-parse", "HEAD")
    control = Control()
    with pytest.raises(ValueError, match="No selected changes"):
        execute(repo, bump=True, control=control)
    assert git(repo, "rev-parse", "HEAD") == head
    assert (repo / "version.txt").read_text() == "version = 1.2.4\n"
    assert control.released == [control.grant_id]


def test_optional_version_configuration_absent_and_dirty_target_fail_closed(repo):
    result = commits._execute_git(
        repo,
        ["owned.txt"],
        "no version configuration",
        False,
        Control(),
        lambda phase, values: None,
        None,
    )
    assert result["committed"]
    (repo / "owned.txt").write_text("another\n")
    with pytest.raises(ValueError, match="not configured"):
        commits._execute_git(
            repo,
            ["owned.txt"],
            "missing configuration",
            True,
            Control(),
            lambda p, v: None,
            None,
        )
    (repo / "version.txt").write_text("version = 9.9.9\n")
    with pytest.raises(ValueError, match="already has edits"):
        execute(repo, bump=True)
    assert (repo / "version.txt").read_text() == "version = 9.9.9\n"


@pytest.mark.parametrize(
    "rule",
    [
        version_rule(match="version = (?P<patch>\\d+)"),
        version_rule(match="^no such record$", replacement="0"),
        version_rule(validate="never matches"),
    ],
)
def test_bad_version_rules_refuse_publication(repo, rule):
    head = git(repo, "rev-parse", "HEAD")
    with pytest.raises(ValueError):
        execute(repo, bump=True, rule=rule)
    assert git(repo, "rev-parse", "HEAD") == head


def reviewed_patch(repo):
    content = (
        "owned before\n"
        + "".join(f"context {index}\n" for index in range(30))
        + "peer before\n"
    )
    (repo / "owned.txt").write_text(content)
    git(repo, "add", "owned.txt")
    git(repo, "commit", "-qm", "patch base")
    base = git(repo, "rev-parse", "HEAD").decode().strip()
    owned = content.replace("owned before", "owned after")
    (repo / "owned.txt").write_text(owned)
    patch = git(repo, "diff", "--binary", "--", "owned.txt")
    (repo / "owned.txt").write_text(content.replace("peer before", "peer after"))
    git(repo, "add", "owned.txt")
    combined = owned.replace("peer before", "peer after")
    (repo / "owned.txt").write_text(combined)
    return base, patch, owned, combined


def test_reviewed_patch_preserves_peer_hunks_in_same_working_file_and_index(repo):
    base, patch, owned, combined = reviewed_patch(repo)
    result = execute(repo, patch=patch, base_commit=base)
    assert result["selection"] == "patch"
    assert git(repo, "show", "HEAD:owned.txt") == owned.encode()
    assert git(repo, "show", ":owned.txt") == combined.encode()
    assert (repo / "owned.txt").read_text() == combined
    assert b"peer after" in git(repo, "diff", "--cached", "--", "owned.txt")
    assert b"owned after" not in git(
        repo, "diff", "--cached", "--unified=0", "--", "owned.txt"
    )


@pytest.mark.parametrize(
    "failure", ["wrong_scope", "overlap", "working_race", "changed_base"]
)
def test_patch_refusal_preserves_every_owned_and_peer_surface(repo, failure):
    base, patch, _, combined = reviewed_patch(repo)
    control = Control()
    if failure == "wrong_scope":
        patch = patch.replace(b"owned.txt", b"peer.txt")
    if failure == "overlap":
        (repo / "owned.txt").write_text(
            combined.replace("owned after", "peer overlapping edit")
        )
        git(repo, "add", "owned.txt")
        (repo / "owned.txt").write_text(combined)
    if failure == "working_race":
        control.on_status = lambda: (repo / "owned.txt").write_text(
            combined + "racing write\n"
        )
    if failure == "changed_base":
        git(repo, "commit", "-qm", "peer selected committed change")
    index = (repo / ".git/index").read_bytes()
    head = git(repo, "rev-parse", "HEAD")
    with pytest.raises(ValueError):
        execute(repo, control=control, patch=patch, base_commit=base)
    assert git(repo, "rev-parse", "HEAD") == head
    assert (repo / ".git/index").read_bytes() == index


@pytest.mark.parametrize("hook_name,code", [("pre-commit", 23), ("post-commit", 23)])
def test_hook_output_and_failure_preserve_machine_receipt(repo, capfd, hook_name, code):
    hooks = repo / ".git/hooks"
    hooks.mkdir(exist_ok=True)
    git(repo, "config", "core.hooksPath", str(hooks))
    hook = hooks / hook_name
    hook.write_text(f"#!/bin/sh\necho hook stdout\necho hook stderr >&2\nexit {code}\n")
    hook.chmod(0o700)
    result = execute(repo)
    print(json.dumps(result))
    captured = capfd.readouterr()
    assert json.loads(captured.out) == result
    assert "hook stdout" in captured.err and "hook stderr" in captured.err
    assert result["committed"] == (hook_name == "post-commit")
    assert result["post_commit_code" if hook_name == "post-commit" else "code"] == code


def test_hook_receives_only_exact_generic_grant_and_owned_descriptor(repo):
    hooks = repo / ".git/hooks"
    hooks.mkdir(exist_ok=True)
    git(repo, "config", "core.hooksPath", str(hooks))
    hook = hooks / "pre-commit"
    output = repo / "hook.json"
    hook.write_text(
        "#!" + sys.executable + "\nimport json,os\nfrom pathlib import Path\n"
        'fd=int(os.environ["AGENTCOORD_COMMIT_LOCK_FD"])\n'
        'actual=os.fstat(fd); expected=os.stat(".git/agentcoord-commit.lock")\n'
        f'Path({str(output)!r}).write_text(json.dumps([os.environ["AGENTCOORD_COMMIT_GRANT_ID"],actual.st_ino==expected.st_ino]))\n'
    )
    hook.chmod(0o700)
    control = Control()
    result = execute(repo, control=control)
    assert result["committed"]
    assert json.loads(output.read_text()) == [control.grant_id, True]


def test_selected_staging_requires_reviewed_adoption_and_matching_content(repo):
    git(repo, "add", "owned.txt")
    with pytest.raises(ValueError, match="staged edits"):
        execute(repo)
    result = execute(repo, adopt_staged=True)
    assert result["committed"]
    (repo / "owned.txt").write_text("staged\n")
    git(repo, "add", "owned.txt")
    (repo / "owned.txt").write_text("different working edit\n")
    with pytest.raises(ValueError, match="does not match"):
        execute(repo, adopt_staged=True)


def test_disjoint_head_advance_rebases_prepared_index_preserving_worktree(repo):
    def race():
        (repo / "peer.txt").write_text("concurrent committed\n")
        git(repo, "add", "peer.txt")
        git(repo, "commit", "-qm", "disjoint peer")

    result = execute(repo, control=Control(on_status=race))
    assert result["committed"]
    assert git(repo, "show", "HEAD:peer.txt") == b"concurrent committed\n"
    assert git(repo, "show", "HEAD:owned.txt") == b"after\n"


def test_release_error_retains_exact_published_commit_receipt(repo):
    result = execute(repo, control=Control(release_error=True))
    assert result["committed"] and result["release_pending"]
    assert result["commit"] == git(repo, "rev-parse", "HEAD").decode().strip()
    assert "do not retry" in result["next_action"]


def test_post_ref_fault_reconciles_without_another_commit_or_lost_peer_index(repo):
    (repo / "peer.txt").write_text("peer staged\n")
    git(repo, "add", "peer.txt")
    prepared = {}

    def progress(phase, values):
        prepared.update(values, phase=phase)
        if phase == "published":
            raise ValueError("interrupted after Git ref publication")

    result = execute(repo, progress=progress, bump=True)
    assert result["committed"] and result["receipt_error"]
    count = git(repo, "rev-list", "--count", "HEAD")
    receipt = commits._reconcile_git(repo, prepared)
    assert receipt["committed"] and receipt["commit"] == result["commit"]
    assert git(repo, "rev-list", "--count", "HEAD") == count
    assert git(repo, "diff", "--cached", "--name-only") == b"peer.txt\n"
    assert git(repo, "show", ":peer.txt") == b"peer staged\n"
    assert (repo / "version.txt").read_text() == "version = 1.2.4\n"
    assert commits._reconcile_git(repo, prepared)["committed"]


def test_recovery_refuses_changed_peer_staging_and_unrelated_index_lock(repo):
    prepared = {}

    def progress(phase, values):
        prepared.update(values, phase=phase)
        if phase == "published":
            raise ValueError("publication interruption")

    execute(repo, progress=progress)
    (repo / "owned.txt").write_text("peer new staged content\n")
    git(repo, "add", "owned.txt")
    with pytest.raises(commits.CoordinationError, match="peer staging"):
        commits._reconcile_git(repo, prepared)
    lock = repo / ".git/index.lock"
    lock.write_bytes(b"other Git writer")
    with pytest.raises(commits.CoordinationError, match="unrelated"):
        commits._reconcile_git(repo, prepared)
    assert lock.read_bytes() == b"other Git writer"


def test_no_effect_after_denied_grant_and_unsafe_paths(repo):
    head = git(repo, "rev-parse", "HEAD")
    before = (repo / ".git/index").read_bytes()
    assert not execute(repo, control=Control(granted=False), bump=True)["committed"]
    for path in ("../escape", ".git/index", "."):
        with pytest.raises(ValueError):
            execute(repo, paths=[path])
    assert git(repo, "rev-parse", "HEAD") == head
    assert (repo / ".git/index").read_bytes() == before


@pytest.mark.parametrize(
    "selected", [["owned.txt"], ["renamed.txt"], ["owned.txt", "renamed.txt"]]
)
def test_staged_rename_adoption_requires_both_sides(repo, selected):
    (repo / "owned.txt").write_text("before\n")
    (repo / "owned.txt").rename(repo / "renamed.txt")
    git(repo, "add", "-A")
    if len(selected) == 1:
        before = (repo / ".git/index").read_bytes()
        with pytest.raises(ValueError, match="both sides"):
            execute(repo, paths=selected, adopt_staged=True)
        assert (repo / ".git/index").read_bytes() == before
        assert git(repo, "show", "HEAD:owned.txt") == b"before\n"
    else:
        assert execute(repo, paths=selected, adopt_staged=True)["committed"]
        assert git(repo, "show", "HEAD:renamed.txt") == b"before\n"
        assert git(repo, "diff", "--cached", "--name-only") == b""


@pytest.mark.parametrize("deleted", [False, True])
def test_tracked_file_beneath_ignored_directory_retains_peer_index(repo, deleted):
    directory = repo / "generated"
    directory.mkdir()
    target = directory / "record.txt"
    target.write_text("tracked\n")
    git(repo, "add", "generated/record.txt")
    (repo / ".gitignore").write_text("/generated/\n")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-qm", "tracked ignored path")
    if deleted:
        target.unlink()
    else:
        target.write_text("updated\n")
    (repo / "peer.txt").write_text("peer staged\n")
    git(repo, "add", "peer.txt")
    assert execute(repo, paths=["generated/record.txt"])["committed"]
    assert git(repo, "show", ":peer.txt") == b"peer staged\n"
    if deleted:
        assert (
            git(repo, "ls-tree", "--name-only", "HEAD", "--", "generated/record.txt")
            == b""
        )
    else:
        assert git(repo, "show", "HEAD:generated/record.txt") == b"updated\n"


def test_new_ignored_file_is_refused_and_configuration_signing_failure_is_preserved(
    repo,
):
    (repo / ".gitignore").write_text("/ignored.txt\n")
    (repo / "ignored.txt").write_text("not selected\n")
    before = (repo / ".git/index").read_bytes()
    with pytest.raises(ValueError, match="ignored"):
        execute(repo, paths=["ignored.txt"])
    assert (repo / ".git/index").read_bytes() == before
    git(repo, "config", "commit.gpgsign", "true")
    git(repo, "config", "gpg.program", str(repo / "missing-signing-program"))
    head = git(repo, "rev-parse", "HEAD")
    with pytest.raises(ValueError, match="commit-tree failed"):
        execute(repo)
    assert git(repo, "rev-parse", "HEAD") == head


def test_hook_private_index_mutation_is_rejected_before_publication(repo):
    hooks = repo / ".git/hooks"
    hooks.mkdir(exist_ok=True)
    git(repo, "config", "core.hooksPath", str(hooks))
    hook = hooks / "pre-commit"
    hook.write_text(
        '#!/bin/sh\nprintf "hook change\\n" > peer.txt\ngit add -- peer.txt\n'
    )
    hook.chmod(0o700)
    head = git(repo, "rev-parse", "HEAD")
    before = (repo / ".git/index").read_bytes()
    with pytest.raises(ValueError, match="private index"):
        execute(repo)
    assert git(repo, "rev-parse", "HEAD") == head
    assert (repo / ".git/index").read_bytes() == before
    assert (repo / "peer.txt").read_text() == "hook change\n"


def test_ref_compare_and_swap_preserves_competing_commit(repo):
    competing = {}

    def progress(phase, values):
        if phase == "publishing":
            oid = (
                git(
                    repo,
                    "commit-tree",
                    git(repo, "rev-parse", "HEAD^{tree}").decode().strip(),
                    "-p",
                    git(repo, "rev-parse", "HEAD").decode().strip(),
                    data=b"competing\n",
                )
                .decode()
                .strip()
            )
            git(repo, "update-ref", "HEAD", oid)
            competing["oid"] = oid

    with pytest.raises(ValueError, match="update-ref failed"):
        execute(repo, progress=progress)
    assert git(repo, "rev-parse", "HEAD").decode().strip() == competing["oid"]
    assert git(repo, "show", "HEAD:owned.txt") == b"before\n"


@pytest.fixture
def admission_store(tmp_path):
    from agentcoord.store import Store

    store = Store(tmp_path / "state" / "test.sqlite3", str(uuid.uuid4()))
    # The admission domain needs actor references; native binding is tested by identity.
    store.initialize((("CREATE TABLE actors(id TEXT PRIMARY KEY)",), commits.SCHEMA))
    actors = [str(uuid.uuid4()) for _ in range(3)]
    with store.write() as tx:
        tx.connection.executemany(
            "INSERT INTO actors VALUES (?)", ((actor,) for actor in actors)
        )
    return store, [
        SimpleNamespace(actor_id=actor, task_generation=str(uuid.uuid4()))
        for actor in actors
    ]


def test_admission_disjoint_parallel_progress_and_manual_scope(admission_store):
    store, (one, two, three) = admission_store
    with store.write() as tx:
        first = commits.admit(
            tx, one, mode="native", paths=["one.txt"], owner_identity={}
        )
        second = commits.admit(
            tx, two, mode="native", paths=["two.txt"], owner_identity={}
        )
        manual = commits.admit(
            tx, three, mode="manual", paths=[], owner_identity={"started": "verified"}
        )
        assert first["granted"] and second["granted"] and not manual["granted"]
        commits.release_exact(tx, one.actor_id, first["grant_id"])
        assert not commits.admit(
            tx, three, mode="manual", paths=[], owner_identity={"started": "verified"}
        )["granted"]
        commits.release_exact(tx, two.actor_id, second["grant_id"])
        assert commits.admit(
            tx, three, mode="manual", paths=[], owner_identity={"started": "verified"}
        )["granted"]


def test_exact_grant_never_expires_an_active_writer_or_releases_another_actor(
    admission_store,
):
    store, (one, two, _) = admission_store
    with store.write() as tx:
        first = commits.admit(
            tx, one, mode="native", paths=["shared.txt"], owner_identity={}
        )
        tx.connection.execute(
            "UPDATE commit_admissions SET expires_us=0 WHERE id=?",
            (first["admission_id"],),
        )
        assert not commits.admit(
            tx, two, mode="native", paths=["shared.txt"], owner_identity={}
        )["granted"]
        with pytest.raises(commits.CoordinationError, match="another actor"):
            commits.release_exact(tx, two.actor_id, first["grant_id"])
        commits.release_exact(tx, one.actor_id, first["grant_id"])
        second = commits.admit(
            tx, two, mode="native", paths=["shared.txt"], owner_identity={}
        )
        assert second["granted"]
        assert commits.release_exact(tx, one.actor_id, first["grant_id"])[
            "already_released"
        ]
        assert commits.status(tx, two.actor_id)["held_by_you"]


def test_status_read_does_not_join_or_renew_queue(admission_store):
    store, (one, _, _) = admission_store
    with store.read() as tx:
        assert commits.status(tx, one.actor_id)["admission_id"] is None
        assert (
            tx.connection.execute("SELECT COUNT(*) FROM commit_admissions").fetchone()[
                0
            ]
            == 0
        )


@pytest.fixture
def native_service(repo):
    from agentcoord import identity, pending
    from agentcoord.config import Config
    from agentcoord.core import Service
    from agentcoord.store import Store

    store = Store(repo / "state" / "coord.sqlite3", str(uuid.uuid4()))
    store.initialize((identity.SCHEMA, commits.SCHEMA, pending.SCHEMA))
    binding = identity.bind_native(
        store,
        {
            "harness": "codex",
            "native_session_id": str(uuid.uuid4()),
            "task": "Git effect fixture",
            "process_identity": identity.process_identity(os.getpid()),
        },
    )
    with store.write() as tx:
        identity.start_execution(
            tx, binding["context"], native_run_id=str(uuid.uuid4())
        )
    context = identity.context_from_token(store, binding["token"])
    service = Service(
        store,
        SimpleNamespace(id=store.workspace_id, root=repo),
        Config(version=version_rule()),
        commits.operations(),
        {
            "slow_handlers": {
                "commit.execute": commits.execute_operation,
                "commit.reconcile": commits.execute_operation,
            }
        },
    )
    return service, context


def enqueue_commit(native_service, *, key=None, **arguments):
    from agentcoord.core import Call

    service, context = native_service
    response = service.execute(
        context,
        Call(
            "commit.execute",
            {
                "paths": ["owned.txt"],
                "message": "native durable selection",
                **arguments,
            },
            key or str(uuid.uuid4()),
        ),
    )
    assert response["ok"], response
    return response["data"]


def test_native_service_receipt_retry_and_grant_release_do_not_publish_twice(
    native_service,
):
    service, context = native_service
    key = str(uuid.uuid4())
    accepted = enqueue_commit(native_service, key=key, bump_version=True)
    assert enqueue_commit(native_service, key=key, bump_version=True) == accepted
    from agentcoord.core import Call

    second = service.execute(
        context,
        Call(
            "commit.execute",
            {
                "paths": ["owned.txt"],
                "message": "another request under the same grant",
            },
            str(uuid.uuid4()),
        ),
    )
    assert second["error"]["code"] == "RECONCILIATION_REQUIRED"
    assert second["error"]["details"]["operation_id"] == accepted["operation_id"]
    result = service.run_operation(accepted["operation_id"])
    assert result["state"] == "succeeded", result
    assert (
        result["result"]["commit"]
        == git(service.workspace.root, "rev-parse", "HEAD").decode().strip()
    )
    count = git(service.workspace.root, "rev-list", "--count", "HEAD")
    assert service.run_operation(accepted["operation_id"])["result"] == result["result"]
    assert git(service.workspace.root, "rev-list", "--count", "HEAD") == count
    with service.store.read() as tx:
        assert not commits.status(tx, context.actor_id)["held_by_you"]
        assert (
            tx.connection.execute("SELECT COUNT(*) FROM commit_execution").fetchone()[0]
            == 1
        )
    changed = service.execute(
        context,
        Call(
            "commit.execute",
            {
                "paths": ["owned.txt"],
                "message": "different content",
                "bump_version": True,
            },
            key,
        ),
    )
    assert changed["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_manual_native_grant_uses_kernel_start_identity_and_exact_release(
    native_service,
):
    from agentcoord.core import Call

    service, context = native_service
    acquired = service.execute(context, Call("commit.acquire", {}, str(uuid.uuid4())))
    assert acquired["ok"] and acquired["data"]["granted"], acquired
    grant = acquired["data"]["grant_id"]
    with service.store.read() as tx:
        owner = json.loads(
            tx.connection.execute(
                "SELECT owner_identity_json FROM commit_grants WHERE id=?", (grant,)
            ).fetchone()[0]
        )
        assert owner["pid"] == os.getpid() and owner["started"]
    released = service.execute(
        context, Call("commit.release", {"grant_id": grant}, str(uuid.uuid4()))
    )
    assert released["ok"] and released["data"]["released"]


@pytest.mark.parametrize(
    "change", ["task", "authority", "draining", "paused", "completed"]
)
def test_effect_guard_rechecks_generation_authority_and_lifecycle_outside_prepare(
    native_service, change
):
    service, context = native_service
    accepted = enqueue_commit(native_service)
    head = git(service.workspace.root, "rev-parse", "HEAD")

    def fence(phase):
        if phase != "preparing":
            return
        # The callback runs after the phase transaction: an unrelated fast write
        # can complete while the slow worker owns Git's lock.
        with service.store.write() as tx:
            if change == "task":
                tx.connection.execute(
                    "UPDATE actors SET current_task_generation=? WHERE id=?",
                    (str(uuid.uuid4()), context.actor_id),
                )
            elif change in {"paused", "completed"}:
                tx.connection.execute(
                    "UPDATE actors SET reported_state=? WHERE id=?",
                    (change, context.actor_id),
                )
            else:
                name, value = (
                    ("authority_generation", str(uuid.uuid4()))
                    if change == "authority"
                    else ("service_state", "draining")
                )
                tx.connection.execute(
                    "UPDATE meta SET value_json=? WHERE key=?",
                    (json.dumps(value), name),
                )

    service.adapters["slow_handlers"]["commit.execute"] = lambda srv, op: (
        commits.execute_operation(srv, op, fault=fence)
    )
    result = service.run_operation(accepted["operation_id"])
    assert result["state"] == "failed", result
    assert result["error"]["code"] == (
        "STALE_GENERATION"
        if change == "task"
        else "NOT_AUTHORIZED"
        if change in {"paused", "completed"}
        else "AUTHORITY_FENCED"
    )
    assert git(service.workspace.root, "rev-parse", "HEAD") == head
    with service.store.read() as tx:
        assert not commits.status(tx, context.actor_id)["held_by_you"]


def test_postpublication_worker_fault_requires_and_reconciles_exact_receipt(
    native_service,
):
    from agentcoord.core import Call

    service, context = native_service
    accepted = enqueue_commit(native_service, bump_version=True)

    def fault(phase):
        if phase == "published":
            raise ValueError("lost outcome transport")

    service.adapters["slow_handlers"]["commit.execute"] = lambda srv, op: (
        commits.execute_operation(srv, op, fault=fault)
    )
    result = service.run_operation(accepted["operation_id"])
    assert result["state"] == "uncertain" and result["result"]["committed"]
    with service.store.read() as tx:
        assert (
            commits.status(tx, context.actor_id)["next_action"]
            == "reconcile_publication"
        )
        assert commits.status(tx, context.actor_id)["grant_id"] == accepted["grant_id"]
    denied = service.execute(
        context,
        Call("commit.release", {"grant_id": accepted["grant_id"]}, str(uuid.uuid4())),
    )
    assert denied["error"]["code"] == "RECONCILIATION_REQUIRED"
    recovery = service.execute(
        context,
        Call(
            "commit.reconcile",
            {"operation_id": accepted["operation_id"]},
            str(uuid.uuid4()),
        ),
    )
    assert recovery["ok"], recovery
    recovered = service.run_operation(recovery["data"]["operation_id"])
    assert recovered["state"] == "succeeded", recovered
    assert recovered["result"]["commit"] == result["result"]["commit"]
    assert git(service.workspace.root, "rev-list", "--count", "HEAD") == b"2\n"
    assert (service.workspace.root / "version.txt").read_text() == "version = 1.2.4\n"
    with service.store.read() as tx:
        assert not commits.status(tx, context.actor_id)["held_by_you"]


@pytest.mark.parametrize(
    "phase", ["preparing", "publishing", "published", "reconciled"]
)
def test_real_worker_crash_recovers_durable_phase_without_repeating_effect(
    native_service, phase
):
    service, context = native_service
    root = service.workspace.root
    (root / "peer.txt").write_text("peer staged\n")
    git(root, "add", "peer.txt")
    accepted = enqueue_commit(native_service, bump_version=True)
    script = """
import os, sys
from pathlib import Path
from types import SimpleNamespace
from agentcoord import commits, identity
from agentcoord.config import Config, VersionRule
from agentcoord.core import Service
from agentcoord.store import Store
root, database, workspace, operation, phase = sys.argv[1:]
store = Store(Path(database), workspace)
rule = VersionRule('version.txt', r'^version = (?P<major>\\d+)\\.(?P<minor>\\d+)\\.(?P<patch>\\d+)$', 'version = {major}.{minor}.{patch}')
def stop(actual):
    if actual == phase:
        os._exit(77)
service = Service(store, SimpleNamespace(id=workspace, root=Path(root)), Config(version=rule), commits.operations(),
    {'slow_handlers': {'commit.execute': lambda srv, op: commits.execute_operation(srv, op, fault=stop)}})
service.run_operation(operation, owner_identity=identity.process_identity(os.getpid()))
raise RuntimeError('crash boundary not reached')
"""
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(root),
            str(service.store.path),
            service.store.workspace_id,
            accepted["operation_id"],
            phase,
        ],
        env=dict(os.environ, PYTHONPATH=str(Path(commits.__file__).parents[1])),
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert process.returncode == 77, process.stderr.decode()
    with service.store.read() as tx:
        row = tx.connection.execute(
            "SELECT state FROM operations WHERE id=?", (accepted["operation_id"],)
        ).fetchone()
        assert row["state"] == "running"
        prepared = json.loads(
            tx.connection.execute(
                "SELECT prepared_json FROM commit_execution WHERE operation_id=?",
                (accepted["operation_id"],),
            ).fetchone()[0]
        )
        assert prepared["phase"] == phase and not prepared.get("worker_finished")
    committed = phase in ("published", "reconciled")
    count = git(root, "rev-list", "--count", "HEAD")
    recovered = commits.reconcile(
        service, accepted["operation_id"], actor_id=context.actor_id
    )
    assert recovered["committed"] == committed
    assert (
        git(root, "rev-list", "--count", "HEAD")
        == count
        == (b"2\n" if committed else b"1\n")
    )
    assert git(root, "show", ":peer.txt") == b"peer staged\n"
    assert git(root, "diff", "--cached", "--name-only") == b"peer.txt\n"
    assert (root / "version.txt").read_text() == (
        "version = 1.2.4\n" if committed else "version = 1.2.3\n"
    )
    assert not (root / ".git" / "index.lock").exists()
    with service.store.read() as tx:
        assert not commits.status(tx, context.actor_id)["held_by_you"]


def test_recovery_rejects_active_and_unknown_worker_identity(native_service):
    from agentcoord import identity

    service, context = native_service
    accepted = enqueue_commit(native_service)
    for owner in (identity.process_identity(os.getpid()), {}):
        with service.store.write() as tx:
            tx.connection.execute(
                "UPDATE operations SET state='uncertain',owner_identity_json=? WHERE id=?",
                (json.dumps(owner), accepted["operation_id"]),
            )
        with pytest.raises(
            commits.CoordinationError, match="active or cannot be proven gone"
        ):
            commits.reconcile(
                service, accepted["operation_id"], actor_id=context.actor_id
            )


def test_reviewed_file_artifact_preserves_non_utf8_patch_bytes(native_service):
    service, _ = native_service
    root = service.workspace.root
    (root / "owned.txt").write_bytes(b"before \xff\n")
    git(root, "add", "owned.txt")
    git(root, "commit", "-qm", "non-UTF8 base")
    base = git(root, "rev-parse", "HEAD").decode().strip()
    (root / "owned.txt").write_bytes(b"after \xfe\n")
    patch = git(root, "diff", "--binary", "--", "owned.txt")
    with pytest.raises(UnicodeDecodeError):
        patch.decode()
    artifact = root / "reviewed.patch"
    artifact.write_bytes(patch)
    accepted = enqueue_commit(
        native_service,
        patch_file="reviewed.patch",
        patch_sha256=hashlib.sha256(patch).hexdigest(),
        base_commit=base,
    )
    result = service.run_operation(accepted["operation_id"])
    assert result["state"] == "succeeded", result
    assert result["result"]["patch_sha256"] == hashlib.sha256(patch).hexdigest()
    assert git(root, "show", "HEAD:owned.txt") == b"after \xfe\n"


@pytest.mark.parametrize("failure", ["changed", "symlink", "directory", "oversized"])
def test_reviewed_artifact_failures_do_not_publish_or_retain_grant(
    native_service, failure
):
    service, context = native_service
    root = service.workspace.root
    patch = git(root, "diff", "--binary", "--", "owned.txt")
    artifact = root / "reviewed.patch"
    artifact.write_bytes(patch)
    accepted = enqueue_commit(
        native_service,
        patch_file="reviewed.patch",
        patch_sha256=hashlib.sha256(patch).hexdigest(),
        base_commit=git(root, "rev-parse", "HEAD").decode().strip(),
    )
    artifact.unlink()
    if failure == "changed":
        artifact.write_bytes(patch + b"changed")
    elif failure == "symlink":
        artifact.symlink_to(root / "owned.txt")
    elif failure == "directory":
        artifact.mkdir()
    else:
        with artifact.open("wb") as stream:
            stream.truncate(16 * 1024 * 1024 + 1)
    before = git(root, "rev-parse", "HEAD")
    result = service.run_operation(accepted["operation_id"])
    assert result["state"] == "failed", result
    assert git(root, "rev-parse", "HEAD") == before
    with service.store.read() as tx:
        assert not commits.status(tx, context.actor_id)["held_by_you"]


def test_admission_actions_filter_before_limit_and_reappear_when_blocker_releases(
    native_service,
):
    from dataclasses import replace

    from agentcoord import identity

    service, one = native_service
    two = identity.bind_native(
        service.store,
        {
            "harness": "codex",
            "native_session_id": str(uuid.uuid4()),
            "task": "peer task",
        },
    )["context"]
    with service.store.write() as tx:
        first = commits.admit(
            tx, one, mode="native", paths=["folder/owned.txt"], owner_identity={}
        )
        second = commits.admit(
            tx, two, mode="native", paths=["folder/owned.txt"], owner_identity={}
        )
        assert first["granted"] and not second["granted"]
        preview = commits.select_actions(tx, two, new_only=True)[0]
        tx.connection.execute(
            "INSERT INTO action_presentations VALUES (?,?,?,?,?)",
            (
                two.actor_id,
                preview["kind"],
                preview["id"],
                preview["version"],
                tx.now_us,
            ),
        )
        assert commits.count_pending(tx, two, new_only=True) == {"commit_admissions": 0}
        commits.release_exact(tx, one.actor_id, first["grant_id"])
        updated = commits.select_actions(tx, two, new_only=True)[0]
        assert (
            updated["version"] > preview["version"] and "eligible" in updated["summary"]
        )
        assert commits.select_actions(tx, two, filters={"actor_id": one.actor_id}) == []
        assert commits.select_actions(tx, two, filters={"path": "other"}) == []
        assert commits.count_pending(tx, two, filters={"path": "folder"}) == {
            "commit_admissions": 1
        }
        operator = replace(
            one,
            operator=True,
            actor_id=None,
            identity_mode="operator",
            transport="operator",
        )
        assert (
            commits.select_actions(
                tx, operator, limit=1, filters={"task": "peer task"}
            )[0]["id"]
            == second["admission_id"]
        )
        assert commits.count_pending(tx, operator) == {"commit_admissions": 1}


def test_version_parent_symlink_escape_preserves_external_authored_file(repo, tmp_path):
    directory = repo / "version-owner"
    directory.mkdir()
    (directory / "record.txt").write_text("version = 1.2.3\n")
    git(repo, "add", "version-owner/record.txt")
    git(repo, "commit", "-qm", "nested version")
    (directory / "record.txt").unlink()
    directory.rmdir()
    external = tmp_path.parent / ("external-version-" + uuid.uuid4().hex)
    external.mkdir()
    (external / "record.txt").write_text("version = 1.2.3\n")
    directory.symlink_to(external, target_is_directory=True)
    head = git(repo, "rev-parse", "HEAD")
    with pytest.raises(ValueError, match="symlink escape"):
        execute(repo, bump=True, rule=version_rule(path="version-owner/record.txt"))
    assert git(repo, "rev-parse", "HEAD") == head
    assert (external / "record.txt").read_text() == "version = 1.2.3\n"


def test_obsolete_pending_admission_can_change_scope_without_expiring_granted_owner(
    admission_store,
):
    store, (one, two, _) = admission_store
    with store.write() as tx:
        held = commits.admit(
            tx, one, mode="native", paths=["shared.txt"], owner_identity={}
        )
        waiting = commits.admit(
            tx, two, mode="native", paths=["shared.txt"], owner_identity={}
        )
        assert not waiting["granted"]
        tx.connection.execute(
            "UPDATE commit_admissions SET expires_us=0 WHERE id=?",
            (waiting["admission_id"],),
        )
        changed = commits.admit(
            tx, two, mode="native", paths=["independent.txt"], owner_identity={}
        )
        assert changed["granted"] and changed["admission_id"] != waiting["admission_id"]
        assert (
            tx.connection.execute(
                "SELECT state FROM commit_admissions WHERE id=?",
                (waiting["admission_id"],),
            ).fetchone()[0]
            == "cancelled"
        )
        assert commits.status(tx, one.actor_id)["grant_id"] == held["grant_id"]


def test_manual_reacquire_cannot_adopt_another_native_process_grant(admission_store):
    store, (one, _, _) = admission_store
    with store.write() as tx:
        commits.admit(
            tx,
            one,
            mode="manual",
            paths=[],
            owner_identity={"pid": 10, "started": "first"},
        )
        with pytest.raises(commits.CoordinationError, match="Previous native process"):
            commits.admit(
                tx,
                one,
                mode="manual",
                paths=[],
                owner_identity={"pid": 20, "started": "second"},
            )


def test_published_outcome_finishes_when_service_drains(native_service):
    service, _ = native_service
    accepted = enqueue_commit(native_service)

    def drain(phase):
        if phase == "published":
            with service.store.write() as tx:
                tx.connection.execute(
                    "UPDATE meta SET value_json='\"draining\"' WHERE key='service_state'"
                )

    service.adapters["slow_handlers"]["commit.execute"] = lambda srv, op: (
        commits.execute_operation(srv, op, fault=drain)
    )
    result = service.run_operation(accepted["operation_id"])
    assert result["state"] == "succeeded" and result["result"]["committed"], result
    assert git(service.workspace.root, "rev-list", "--count", "HEAD") == b"2\n"


def test_native_pending_cancel_preserves_live_owner_and_allows_new_scope(
    native_service,
):
    from agentcoord import identity
    from agentcoord.core import Call

    service, waiting = native_service
    owner = identity.bind_native(
        service.store,
        {
            "harness": "claude",
            "native_session_id": str(uuid.uuid4()),
            "task": "owner fixture",
        },
    )["context"]
    with service.store.write() as tx:
        held = commits.admit(
            tx, owner, mode="native", paths=["owned.txt"], owner_identity={}
        )
    accepted = enqueue_commit(native_service)
    assert not accepted["granted"]
    key = str(uuid.uuid4())
    call = Call("commit.cancel", {"admission_id": accepted["admission_id"]}, key)
    response = service.execute(waiting, call)
    assert response["ok"] and response["data"]["cancelled"], response
    assert service.execute(waiting, call)["data"] == response["data"]
    with service.store.read() as tx:
        assert commits.status(tx, owner.actor_id)["grant_id"] == held["grant_id"]
        assert commits.status(tx, owner.actor_id)["held_by_you"]
    alternate = enqueue_commit(native_service, paths=["peer.txt"])
    assert alternate["operation_id"]


def test_pending_cancel_cannot_cancel_a_granted_or_peer_admission(native_service):
    from agentcoord import identity
    from agentcoord.core import Call

    service, context = native_service
    accepted = enqueue_commit(native_service)
    denied = service.execute(
        context,
        Call(
            "commit.cancel",
            {"admission_id": accepted["admission_id"]},
            str(uuid.uuid4()),
        ),
    )
    assert denied["error"]["code"] == "RECONCILIATION_REQUIRED"
    peer = identity.bind_native(
        service.store,
        {
            "harness": "claude",
            "native_session_id": str(uuid.uuid4()),
            "task": "unrelated actor",
        },
    )["context"]
    denied = service.execute(
        peer,
        Call(
            "commit.cancel",
            {"admission_id": accepted["admission_id"]},
            str(uuid.uuid4()),
        ),
    )
    assert denied["error"]["code"] == "NOT_AUTHORIZED"


def test_four_disjoint_workers_queue_handoff_and_rebase_preserving_peer_staging(
    native_service, monkeypatch
):
    from agentcoord import identity
    from agentcoord.core import Call

    service, original = native_service
    root = service.workspace.root
    paths = [f"concurrent-{number}.txt" for number in range(4)]
    for path in paths:
        (root / path).write_text("before\n")
    git(root, "add", "--", *paths)
    git(root, "commit", "-qm", "concurrent selection base")
    for path in paths:
        (root / path).write_text("after\n")
    (root / "peer.txt").write_text("peer staged\n")
    git(root, "add", "peer.txt")
    contexts = [original]
    for number in range(3):
        binding = identity.bind_native(
            service.store,
            {
                "harness": "codex",
                "native_session_id": str(uuid.uuid4()),
                "task": f"concurrent worker {number}",
                "process_identity": identity.process_identity(os.getpid()),
            },
        )
        with service.store.write() as tx:
            identity.start_execution(
                tx, binding["context"], native_run_id=str(uuid.uuid4())
            )
        contexts.append(identity.context_from_token(service.store, binding["token"]))
    accepted = [
        enqueue_commit((service, context), paths=[path], bump_version=True)
        for context, path in zip(contexts, paths, strict=True)
    ]

    # All four real private indexes finish preparation before any worker can
    # enter Git's handoff. The wrapper only synchronizes real lock-file opens;
    # flock, Git commands and durable domain transitions remain unmodified.
    barrier = threading.Barrier(4)
    original_open = os.open

    def synchronized_open(path, flags, mode=0o777, **options):
        descriptor = original_open(path, flags, mode, **options)
        if Path(path) == root / ".git" / "agentcoord-commit.lock":
            barrier.wait(timeout=10)
        return descriptor

    monkeypatch.setattr(commits.os, "open", synchronized_open)
    first_handoff = threading.Event()
    allow_publication = threading.Event()

    def hold_first_handoff(phase):
        if phase == "preparing" and not first_handoff.is_set():
            first_handoff.set()
            assert allow_publication.wait(timeout=10), (
                "Fast status did not finish outside the Git handoff"
            )

    service.adapters["slow_handlers"]["commit.execute"] = lambda srv, op: (
        commits.execute_operation(srv, op, fault=hold_first_handoff)
    )
    owner = identity.process_identity(os.getpid())
    with ThreadPoolExecutor(max_workers=4) as workers:
        futures = [
            workers.submit(
                service.run_operation, item["operation_id"], owner_identity=owner
            )
            for item in accepted
        ]
        try:
            assert first_handoff.wait(timeout=10), (
                "Concurrent workers never reached publication preparation"
            )
            for context in contexts:
                status = service.execute(context, Call("commit.status", {}, None))
                assert status["ok"] and status["data"]["held_by_you"], status
        finally:
            allow_publication.set()
        results = [future.result(timeout=20) for future in futures]
    assert all(result["state"] == "succeeded" for result in results), results
    assert len({result["result"]["commit"] for result in results}) == 4
    for result, path in zip(results, paths, strict=True):
        committed_paths = (
            git(
                root,
                "diff-tree",
                "--no-commit-id",
                "--name-only",
                "-r",
                result["result"]["commit"],
            )
            .decode()
            .splitlines()
        )
        assert sorted(committed_paths) == sorted([path, "version.txt"])
        assert git(root, "show", "HEAD:" + path) == b"after\n"
    assert git(root, "rev-list", "--count", "HEAD") == b"6\n"
    assert (root / "version.txt").read_text() == "version = 1.2.7\n"
    assert git(root, "diff", "--cached", "--name-only") == b"peer.txt\n"
    assert git(root, "show", ":peer.txt") == b"peer staged\n"
    assert (root / "owned.txt").read_text() == "after\n"
    with service.store.read() as tx:
        assert (
            tx.connection.execute(
                "SELECT COUNT(*) FROM commit_grants WHERE state='active'"
            ).fetchone()[0]
            == 0
        )


def test_stopped_handoff_holder_times_out_before_effect_without_unlocking_owner(
    native_service, monkeypatch
):
    import fcntl

    service, context = native_service
    root = service.workspace.root
    accepted = enqueue_commit(native_service, bump_version=True)
    head, index = git(root, "rev-parse", "HEAD"), (root / ".git" / "index").read_bytes()
    monkeypatch.setattr(commits, "HANDOFF_WAIT_SECONDS", 0.15)
    with (root / ".git" / "agentcoord-commit.lock").open("a") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        result = service.run_operation(accepted["operation_id"])
        assert result["state"] == "failed", result
        assert (
            result["error"]["code"] == "SERVICE_BUSY" and result["error"]["retryable"]
        )
        details = result["error"]["details"]
        assert details["phase"] == "handoff_wait" and details["effect_started"] is False
        assert details["publication"] == "not_published"
        with (
            (root / ".git" / "agentcoord-commit.lock").open("a") as probe,
            pytest.raises(BlockingIOError),
        ):
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert git(root, "rev-parse", "HEAD") == head
    assert (root / ".git" / "index").read_bytes() == index
    assert (root / "version.txt").read_text() == "version = 1.2.3\n"
    with service.store.read() as tx:
        assert not commits.status(tx, context.actor_id)["held_by_you"]
        assert (
            tx.connection.execute(
                "SELECT effect_started_us FROM operations WHERE id=?",
                (accepted["operation_id"],),
            ).fetchone()[0]
            is None
        )


def test_handoff_deadline_expiring_during_authority_probe_cannot_start_effect(
    native_service, monkeypatch
):
    import time

    service, _ = native_service
    root = service.workspace.root
    accepted = enqueue_commit(native_service)
    head = git(root, "rev-parse", "HEAD")
    original_check = commits._Control.check_wait

    def delayed_probe(control, grant_id):
        original_check(control, grant_id)
        time.sleep(0.03)

    monkeypatch.setattr(commits._Control, "check_wait", delayed_probe)
    monkeypatch.setattr(commits, "HANDOFF_WAIT_SECONDS", 0.01)
    result = service.run_operation(accepted["operation_id"])
    assert result["state"] == "failed" and result["error"]["code"] == "SERVICE_BUSY", (
        result
    )
    assert result["error"]["retryable"]
    assert git(root, "rev-parse", "HEAD") == head


@pytest.mark.parametrize("fence", ["draining", "cancelled", "paused", "generation"])
def test_waiting_handoff_exits_on_exact_authority_or_cancellation_change(
    native_service, monkeypatch, fence
):
    import fcntl

    service, context = native_service
    root = service.workspace.root
    accepted = enqueue_commit(native_service)
    waiting = threading.Event()
    original_check = commits._Control.check_wait

    def observe_wait(control, grant_id):
        original_check(control, grant_id)
        waiting.set()

    monkeypatch.setattr(commits._Control, "check_wait", observe_wait)
    head = git(root, "rev-parse", "HEAD")
    with (root / ".git" / "agentcoord-commit.lock").open("a") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        with ThreadPoolExecutor(max_workers=1) as workers:
            future = workers.submit(service.run_operation, accepted["operation_id"])
            assert waiting.wait(timeout=10), "Worker did not enter handoff admission"
            # This write finishes while the lock remains held, proving the
            # waiting worker retains no SQLite transaction.
            with service.store.write() as tx:
                if fence == "draining":
                    tx.connection.execute(
                        "UPDATE meta SET value_json='\"draining\"' WHERE key='service_state'"
                    )
                elif fence == "paused":
                    tx.connection.execute(
                        "UPDATE actors SET reported_state='paused' WHERE id=?",
                        (context.actor_id,),
                    )
                elif fence == "generation":
                    tx.connection.execute(
                        "UPDATE actors SET current_task_generation=? WHERE id=?",
                        (str(uuid.uuid4()), context.actor_id),
                    )
                else:
                    service.finish_operation(
                        tx,
                        accepted["operation_id"],
                        "cancelled",
                        result={"cancelled": True, "committed": False},
                    )
            result = future.result(timeout=5)
            assert result["state"] == (
                "cancelled" if fence == "cancelled" else "failed"
            ), result
            if fence != "cancelled":
                assert (
                    result["error"]["code"]
                    == {
                        "draining": "AUTHORITY_FENCED",
                        "paused": "NOT_AUTHORIZED",
                        "generation": "STALE_GENERATION",
                    }[fence]
                )
    assert git(root, "rev-parse", "HEAD") == head
    with service.store.read() as tx:
        assert not commits.status(tx, context.actor_id)["held_by_you"]
        assert (
            tx.connection.execute(
                "SELECT effect_started_us FROM operations WHERE id=?",
                (accepted["operation_id"],),
            ).fetchone()[0]
            is None
        )


def test_commit_failure_receipt_has_safe_phase_and_private_detail(native_service, monkeypatch):
    service, _ = native_service
    accepted = enqueue_commit(native_service)
    secret = 'sensitive-hook-argument-should-not-appear-in-receipt'
    def fail(*args, **kwargs):
        raise ValueError(secret)
    monkeypatch.setattr(commits, '_execute_git', fail)
    result = service.run_operation(accepted['operation_id'])
    assert result['state'] == 'failed' and not result['result']['committed']
    assert secret not in json.dumps(result)
    detail = result['error']['details']
    assert detail['publication'] == 'not_published' and detail['phase']
    assert detail['category'] == 'validation' and detail['operation_id'] == accepted['operation_id']
    log = Path(detail['diagnostic_log'])
    assert secret in log.read_text() and log.stat().st_mode & 0o777 == 0o600
    assert log.parent.stat().st_mode & 0o777 == 0o700


def test_hook_rejection_is_distinct_from_uncertain_publication(native_service):
    service, _ = native_service
    root = service.workspace.root
    hooks = root / '.git/hooks'
    hooks.mkdir(exist_ok=True)
    git(root, 'config', 'core.hooksPath', str(hooks))
    hook = hooks / 'pre-commit'
    hook.write_text('#!/bin/sh\nexit 7\n')
    hook.chmod(0o700)
    head = git(root, 'rev-parse', 'HEAD')
    result = service.run_operation(enqueue_commit(native_service)['operation_id'])
    assert result['state'] == 'failed' and result['error']['code'] == 'GIT_HOOK_FAILED'
    assert result['error']['details']['hook'] == 'pre-commit'
    assert result['error']['details']['exit_code'] == 7
    assert result['error']['details']['publication'] == 'not_published'
    assert git(root, 'rev-parse', 'HEAD') == head


def test_path_selection_has_no_count_cap_and_retains_byte_defense():
    paths = [f"owned/{index}.txt" for index in range(10001)]
    assert set(commits._paths(paths)) == set(paths)
    from agentcoord.core import CoordinationError

    with pytest.raises(CoordinationError, match="4096-byte"):
        commits._paths(["é" * 2049])


def test_native_large_exact_selection_preserves_peer_staging(native_service):
    service, context = native_service
    root = service.workspace.root
    paths = [f"owned-{index}.txt" for index in range(151)]
    for path in paths:
        (root / path).write_text(f"reviewed {path}\n")
    (root / "peer.txt").write_text("peer staging survives\n")
    git(root, "add", "peer.txt")
    from agentcoord.cli import BY_TOOL, validate_arguments

    arguments = {"paths": paths, "message": "large native selection"}
    validate_arguments(BY_TOOL["commit_execute"], arguments)
    accepted = enqueue_commit(native_service, **arguments)
    result = service.run_operation(accepted["operation_id"])
    assert result["state"] == "succeeded", result
    assert set(git(root, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").decode().splitlines()) == set(paths)
    assert git(root, "show", "HEAD:peer.txt") == b"preserve\n"
    assert git(root, "show", ":peer.txt") == b"peer staging survives\n"
    assert git(root, "diff", "--cached", "--name-only") == b"peer.txt\n"
    with service.store.read() as tx:
        assert not commits.status(tx, context.actor_id)["held_by_you"]
