"""Real SQLite receipts and native child-process scheduler regressions."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agentcoord import identity, jobs, messages, pending
from agentcoord.config import Config
from agentcoord.core import Call, CoordinationError, Service
from agentcoord.store import Store


def uid():
    return str(uuid.uuid4())


@pytest.fixture
def system(tmp_path):
    workspace_id = uid()
    store = Store(tmp_path / "state" / "coord.sqlite3", workspace_id)
    store.initialize((identity.SCHEMA, messages.SCHEMA, jobs.SCHEMA, pending.SCHEMA))
    service = Service(
        store,
        SimpleNamespace(id=workspace_id, root=tmp_path),
        Config(),
        (*jobs.operations(), *messages.operations()),
        {
            "slow_handlers": {"job.execute": jobs.execute_operation},
            "observe_native": lambda target: "offline",
        },
    )
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
    try:
        process_identity = identity.process_identity(child.pid)
        assert process_identity is not None
        binding = identity.bind_native(
            store,
            {
                "harness": "codex",
                "native_session_id": uid(),
                "task": "jobs test",
                "process_identity": process_identity,
            },
        )
        with store.write() as tx:
            identity.start_execution(tx, binding["context"], native_run_id=uid())
        context = identity.context_from_token(store, binding["token"])
    finally:
        child.terminate()
        child.wait(timeout=5)
    with store.write() as tx:
        tx.connection.execute("UPDATE actors SET resume_enabled=1 WHERE id=?", (context.actor_id,))
        tx.connection.execute(
            "UPDATE executions SET state='ended',ended_us=? WHERE generation=?",
            (tx.now_us, context.execution_generation),
        )
        # Domain tests drive worker calls after the originating CLI disconnects.
        # Real public binding and observation are covered by composition tests.
        tx.connection.execute("DELETE FROM bindings WHERE id=?", (context.connection_id,))
    return service, replace(context, transport="worker")


def invoke(system, name, arguments, key=None):
    service, context = system
    return service.execute(context, Call(name, arguments, key))


def schedule(system, kind="reminder", **extra):
    response = invoke(
        system,
        "job.schedule",
        {
            "kind": kind,
            "due_us": time.time_ns() // 1000 - 1000_000,
            "note": "Review the current task",
            **extra,
        },
        uid(),
    )
    assert response["ok"], response
    return response["data"]


def record(system, job_id):
    response = invoke(system, "job.get", {"id": job_id})
    assert response["ok"], response
    return response["data"]


def launch(system, job_id, *, fault=None):
    service, _ = system
    operation_id = next(x["operation_id"] for x in jobs.claim_due(service) if x["job_id"] == job_id)
    if fault is not None:
        service.adapters["slow_handlers"]["job.execute"] = lambda service, op: (
            jobs.execute_operation(service, op, fault=fault)
        )
    return service.run_operation(operation_id)


def test_reminder_commits_one_message_and_receipt_atomically(system):
    service, context = system
    job = schedule(system)
    receipt = launch(system, job["id"])
    assert receipt["state"] == "succeeded"
    assert record(system, job["id"])["state"] == "succeeded"
    assert service.run_operation(receipt["id"])["state"] == "succeeded"
    with service.store.read() as tx:
        assert tx.connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
        row = tx.connection.execute("SELECT * FROM recipients").fetchone()
        assert row["actor_id"] == context.actor_id and row["handled_us"] is None
        assert tx.connection.execute("SELECT COUNT(*) FROM job_notifications").fetchone()[0] == 1
    assert jobs.claim_due(service) == []


def test_public_mutation_retry_is_stable_and_conflicting_payload_rejected(system):
    key = uid()
    args = {"kind": "reminder", "due_us": 0, "note": "A stable note"}
    first = invoke(system, "job.schedule", args, key)
    second = invoke(system, "job.schedule", args, key)
    assert first["data"] == second["data"]
    conflict = invoke(system, "job.schedule", {**args, "note": "Changed"}, key)
    assert conflict["error"]["code"] == "IDEMPOTENCY_CONFLICT"


@pytest.mark.parametrize(
    "extra",
    [
        {"command": "echo unsafe"},
        {"actor_id": uid()},
        {"due_us": True},
        {"note": "\ud800"},
        {"note": "\x00"},
        {"timeout_seconds": 5},
    ],
)
def test_invalid_or_arbitrary_job_inputs_fail_explicitly(system, extra):
    result = invoke(
        system, "job.schedule", {"kind": "reminder", "due_us": 0, "note": "note", **extra}, uid()
    )
    assert not result["ok"] and result["error"]["code"] == "INVALID_ARGUMENT"


def test_actor_job_reads_do_not_leak_another_actors_payload(system):
    service, context = system
    job = schedule(system)
    alien = replace(context, actor_id=uid())
    result = service.execute(alien, Call("job.get", {"id": job["id"]}))
    assert not result["ok"]
    assert "Review the current task" not in json.dumps(result)


def test_pending_cancel_is_sticky_and_cannot_be_resolved_to_retry(system):
    job = schedule(system)
    cancelled = invoke(system, "job.cancel", {"id": job["id"]}, uid())["data"]
    assert cancelled["state"] == "cancelled" and cancelled["cancel_requested"]
    assert jobs.claim_due(system[0]) == []
    result = invoke(
        system,
        "job.resolve",
        {"id": job["id"], "version": cancelled["version"], "resolution": "retry", "note": "Retry"},
        uid(),
    )
    assert not result["ok"]
    assert record(system, job["id"])["state"] == "cancelled"


@pytest.mark.parametrize("presence", ["live", "unknown"])
def test_live_or_unknown_native_target_is_never_launched(system, monkeypatch, presence):
    service, _ = system
    job = schedule(system, "resume")
    service.adapters["observe_native"] = lambda target: presence

    def forbidden(*args, **kwargs):
        pytest.fail("Attempted unsafe native launch")

    monkeypatch.setattr(jobs.subprocess, "Popen", forbidden)
    receipt = launch(system, job["id"])
    assert receipt["state"] == "failed"
    assert record(system, job["id"])["state"] == "failed"


def test_resume_consent_revocation_and_generation_changes_are_rechecked(system, monkeypatch):
    service, context = system
    schedule(system, "resume")
    operation_id = jobs.claim_due(service)[0]["operation_id"]
    with service.store.write() as tx:
        tx.connection.execute(
            "UPDATE actors SET resume_inhibited=1 WHERE id=?", (context.actor_id,)
        )
    monkeypatch.setattr(
        jobs.subprocess, "Popen", lambda *a, **kw: pytest.fail("Launched without consent")
    )
    assert service.run_operation(operation_id)["state"] == "failed"


def test_due_sweep_retires_stale_jobs_without_blocking_healthy_reminders(system):
    service, context = system
    stale = schedule(system, "resume")
    with service.store.write() as tx:
        tx.connection.execute(
            "UPDATE actors SET resume_inhibited=1 WHERE id=?", (context.actor_id,)
        )
    healthy = schedule(system)
    receipts = jobs.claim_due(service)
    assert [r["job_id"] for r in receipts] == [healthy["id"]]
    assert record(system, stale["id"])["state"] == "failed"


def test_failed_job_action_keeps_exact_notification_and_version_presentation(system):
    service, context = system
    job = schedule(system, "resume")
    with service.store.write() as tx:
        tx.connection.execute(
            "UPDATE actors SET resume_inhibited=1 WHERE id=?", (context.actor_id,)
        )
    assert jobs.claim_due(service) == []
    with service.store.write() as tx:
        actions = jobs.select_actions(tx, context, new_only=True, filters={"task": "jobs test"})
        assert [a["id"] for a in actions] == [job["id"]]
        assert len(actions[0]["message_ids"]) == 1
        assert jobs.count_pending(tx, context, new_only=True) == {"jobs": 1}
        tx.connection.execute(
            "INSERT INTO action_presentations VALUES (?,?,?,?,?)",
            (context.actor_id, "job", job["id"], actions[0]["version"], tx.now_us),
        )
        assert jobs.select_actions(tx, context, new_only=True) == []
        assert jobs.count_pending(tx, context, new_only=True) == {"jobs": 0}
        assert jobs.count_pending(tx, context) == {"jobs": 1}
        assert jobs.select_actions(tx, context, filters={"path": "unrelated/path"}) == []


def test_proven_prelaunch_failure_has_bounded_backoff_and_no_secret_receipt(system, monkeypatch):
    service, _ = system
    job = schedule(system, "resume")
    monkeypatch.setattr(
        jobs, "resume_argv", lambda target, note, **kwargs: [sys.executable, "-c", "pass"]
    )

    def refused(*args, **kwargs):
        raise OSError("A_PRIVATE_TOKEN_AND_PROMPT")

    monkeypatch.setattr(jobs.subprocess, "Popen", refused)
    for attempt in range(3):
        receipt = launch(system, job["id"])
        assert receipt["state"] == "failed"
        current = record(system, job["id"])
        assert current["state"] == ("pending" if attempt < 2 else "failed")
        assert "A_PRIVATE_TOKEN_AND_PROMPT" not in json.dumps(current["last_result"])
        if attempt < 2:
            with service.store.write() as tx:
                tx.connection.execute(
                    "UPDATE jobs SET due_us=? WHERE id=?", (tx.now_us - 1, job["id"])
                )
    assert jobs.claim_due(service) == []


def test_native_reminder_crash_rolls_back_both_delivery_and_receipt(system):
    service, _ = system
    job = schedule(system)

    def fault(boundary):
        if boundary == "before_reminder_commit":
            raise RuntimeError("fixture interruption")

    receipt = launch(system, job["id"], fault=fault)
    assert receipt["state"] == "failed"
    jobs.recover(service)
    with service.store.read() as tx:
        assert (
            tx.connection.execute("SELECT COUNT(*) FROM messages WHERE kind='reminder'").fetchone()[
                0
            ]
            == 0
        )
        assert tx.connection.execute("SELECT COUNT(*) FROM job_notifications").fetchone()[0] <= 1
    current = record(system, job["id"])
    assert current["state"] == "failed"
    resolved = invoke(
        system,
        "job.resolve",
        {
            "id": job["id"],
            "version": current["version"],
            "resolution": "retry",
            "note": "Verified delivery transaction rolled back",
        },
        uid(),
    )
    assert resolved["ok"], resolved
    service.adapters["slow_handlers"]["job.execute"] = jobs.execute_operation
    assert launch(system, job["id"])["state"] == "succeeded"
    with service.store.read() as tx:
        assert (
            tx.connection.execute("SELECT COUNT(*) FROM messages WHERE kind='reminder'").fetchone()[
                0
            ]
            == 1
        )


def test_native_child_cannot_schedule_parent_conversation_resume(system):
    service, _ = system
    child = identity.bind_native(
        service.store, {"harness": "codex", "native_session_id": uid(), "child_id": uid()}
    )["context"]
    result = service.execute(
        child, Call("job.schedule", {"kind": "resume", "due_us": 0, "note": "Resume"}, uid())
    )
    assert not result["ok"] and result["error"]["code"] == "NOT_AUTHORIZED"


def test_safe_native_argv_preserves_metacharacters_without_permission_mutation(monkeypatch):
    native = uid()
    monkeypatch.setattr(jobs.shutil, "which", lambda name: "/usr/local/bin/" + name)
    prompt = 'Review $(echo token); keep "literal" arguments'
    for harness in ("codex", "claude", "grok"):
        argv = jobs.resume_argv({"harness": harness, "native_session_id": native}, prompt)
        assert native in argv and prompt in argv
        assert "--dangerously-skip-permissions" not in argv and "--yolo" not in argv
    with pytest.raises(CoordinationError):
        jobs.resume_argv({"harness": "codex", "native_session_id": native}, "--yolo")
    with pytest.raises(CoordinationError):
        jobs.resume_argv({"harness": "other", "native_session_id": native}, "prompt")


def test_native_success_uses_owned_process_identity_and_no_write_lock_during_spawn(
    system, monkeypatch
):
    service, _ = system
    job = schedule(system, "resume")
    monkeypatch.setattr(
        jobs,
        "resume_argv",
        lambda target, note, **kwargs: [sys.executable, "-c", "import time; time.sleep(.1)"],
    )
    real_popen = jobs.subprocess.Popen

    def popen(*args, **kwargs):
        # A concurrent writer must be admitted before external launch begins.
        with service.store.write() as tx:
            tx.event("test", "launch_probe", job["id"], None, {})
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(jobs.subprocess, "Popen", popen)
    assert launch(system, job["id"])["state"] == "succeeded"
    with service.store.read() as tx:
        attempt = tx.connection.execute("SELECT * FROM job_attempts").fetchone()
        owned = json.loads(attempt["process_identity_json"])
        assert owned["pid"] == owned["pgid"] and attempt["launch_intent_us"] is not None
        assert attempt["ended_us"] is not None


def test_running_cancel_stops_only_the_worker_owned_process_and_never_resurrects(
    system, monkeypatch
):
    service, _ = system
    job = schedule(system, "resume")
    monkeypatch.setattr(
        jobs,
        "resume_argv",
        lambda target, note, **kwargs: [sys.executable, "-c", "import time; time.sleep(10)"],
    )
    operation_id = jobs.claim_due(service)[0]["operation_id"]
    reached = threading.Event()

    def fault(boundary):
        if boundary == "after_identity":
            reached.set()
            assert invoke(system, "job.cancel", {"id": job["id"]}, uid())["ok"]

    service.adapters["slow_handlers"]["job.execute"] = lambda service, op: jobs.execute_operation(
        service, op, fault=fault
    )
    receipt = service.run_operation(operation_id)
    assert reached.is_set() and receipt["state"] == "cancelled"
    final = record(system, job["id"])
    assert final["cancel_requested"] and final["state"] == "cancelled"
    assert not invoke(
        system,
        "job.resolve",
        {"id": job["id"], "version": final["version"], "resolution": "retry", "note": "Reviewed"},
        uid(),
    )["ok"]


def test_timeout_never_auto_retries_a_launched_process(system, monkeypatch):
    job = schedule(system, "resume", timeout_seconds=1)
    monkeypatch.setattr(
        jobs,
        "resume_argv",
        lambda target, note, **kwargs: [sys.executable, "-c", "import time; time.sleep(10)"],
    )
    assert launch(system, job["id"])["state"] == "uncertain"
    assert record(system, job["id"])["last_result"]["phase"] == "timeout"
    assert jobs.claim_due(system[0]) == []


def test_native_output_is_drained_into_a_private_bounded_log(system, monkeypatch):
    service, _ = system
    service.config = replace(service.config, jobs_log_max_bytes=4096)
    job = schedule(system, "resume", timeout_seconds=3)
    monkeypatch.setattr(
        jobs,
        "resume_argv",
        lambda target, note, **kwargs: [sys.executable, "-c", "print('private-output-' * 10000)"],
    )
    receipt = launch(system, job["id"])
    assert receipt["state"] == "succeeded", receipt["result"]
    path = service.store.path.parent / "jobs" / f"{job['id']}.log"
    assert path.stat().st_size == 4096
    assert path.stat().st_mode & 0o777 == 0o600
    assert "private-output" not in json.dumps(receipt)


def test_spawn_crash_is_durable_uncertainty_and_duplicate_launch_is_blocked(system, monkeypatch):
    service, _ = system
    job = schedule(system, "resume")
    launched = []
    real_popen = jobs.subprocess.Popen
    monkeypatch.setattr(
        jobs,
        "resume_argv",
        lambda target, note, **kwargs: [sys.executable, "-c", "import time; time.sleep(10)"],
    )

    def popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        launched.append(process)
        return process

    monkeypatch.setattr(jobs.subprocess, "Popen", popen)

    class Crash(BaseException):
        pass

    def fault(boundary):
        if boundary == "after_spawn":
            raise Crash()

    try:
        with pytest.raises(Crash):
            launch(system, job["id"], fault=fault)
        with service.store.write() as tx:
            op = tx.connection.execute("SELECT * FROM operations").fetchone()
            assert op["effect_started_us"] is not None
            assert (
                tx.connection.execute("SELECT process_identity_json FROM job_attempts").fetchone()[
                    0
                ]
                is None
            )
            service.finish_operation(
                tx,
                op["id"],
                "uncertain",
                error={"code": "WORKER_GONE"},
                claim_token=op["claim_token"],
            )
        jobs.recover(service)
        current = record(system, job["id"])
        assert current["state"] == "uncertain"
        assert service.run_operation(op["id"])["state"] == "uncertain"
        assert jobs.claim_due(service) == [] and len(launched) == 1
        retry = invoke(
            system,
            "job.resolve",
            {
                "id": job["id"],
                "version": current["version"],
                "resolution": "retry",
                "note": "Unverified outcome",
            },
            uid(),
        )
        assert not retry["ok"] and retry["error"]["code"] == "RECONCILIATION_REQUIRED"
    finally:
        for process in launched:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
    cancelled = invoke(system, "job.cancel", {"id": job["id"]}, uid())["data"]
    completed = invoke(
        system,
        "job.resolve",
        {
            "id": job["id"],
            "version": cancelled["version"],
            "resolution": "complete",
            "note": "Reviewed the recorded spawn and stopped the fixture-owned process",
        },
        uid(),
    )
    assert completed["ok"], completed
    assert completed["data"]["state"] == "cancelled"
    assert service.run_operation(op["id"])["state"] == "cancelled"


def test_pid_reuse_and_unknown_provenance_never_authorize_signalling(monkeypatch):
    process = SimpleNamespace(pid=1234567)
    identity_record = {
        "pid": process.pid,
        "pgid": process.pid,
        "started": "1.000000",
        "source": "darwin-libproc",
    }
    monkeypatch.setattr(jobs, "process_status", lambda identity: "gone")
    monkeypatch.setattr(jobs, "group_status", lambda identity: "unknown")
    monkeypatch.setattr(
        jobs.os, "killpg", lambda *args: pytest.fail("Signalled an unverified group")
    )
    assert not jobs.terminate_owned(process, identity_record)
