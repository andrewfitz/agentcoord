"""Scheduled native resumes must survive their own originating lifecycle hook."""

import json
import subprocess
import sys
import time
import uuid

import pytest

from agentcoord import identity, jobs
from agentcoord.application import build_service
from agentcoord.config import register_workspace
from agentcoord.core import Call


@pytest.mark.parametrize("change", ["owned_start", "unrelated_start", "consent_revoked", "reconnected"])
def test_resumed_process_start_is_owned_progress_not_revocation(tmp_path, monkeypatch, change):
    root = tmp_path / "repo"
    root.mkdir()
    service = build_service(register_workspace(root, state_root=tmp_path / "state"))
    native_session = str(uuid.uuid4())
    previous = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
    try:
        proof = identity.process_identity(previous.pid)
        binding = identity.bind_native(service.store, {
            "harness": "codex", "native_session_id": native_session,
            "task": "resume composition", "process_identity": proof,
        })
        with service.store.write() as tx:
            identity.start_execution(tx, binding["context"], native_run_id="previous-native-run")
    finally:
        previous.terminate()
        previous.wait(timeout=5)
    context = identity.context_from_token(service.store, binding["token"])
    checkpoint = service.execute(context, Call("identity.checkpoint", {
        "state": "idle", "note": "Resume this task later", "resume_enabled": True,
    }, str(uuid.uuid4())))
    assert checkpoint["ok"], checkpoint
    scheduled = service.execute(context, Call("job.schedule", {
        "kind": "resume", "due_us": time.time_ns() // 1000 - 1000,
        "note": "Continue the current task", "timeout_seconds": 5,
    }, str(uuid.uuid4())))
    assert scheduled["ok"], scheduled
    # The real observer refuses retained bindings; close the originating CLI.
    with service.store.write() as tx:
        tx.connection.execute("DELETE FROM bindings WHERE id=?", (context.connection_id,))
    monkeypatch.setattr(jobs, "resume_argv", lambda target, note, **kwargs: [
        sys.executable, "-c", "import time; time.sleep(.3)",
    ])
    spawned = []
    popen = jobs.subprocess.Popen

    def tracked_spawn(*args, **kwargs):
        process = popen(*args, **kwargs)
        spawned.append(process.pid)
        return process

    monkeypatch.setattr(jobs.subprocess, "Popen", tracked_spawn)
    if change == "reconnected":
        observer = service.adapters["observe_native"]

        def reconnect_after_observation(target):
            observed = observer(target)
            assert observed == "offline"
            identity.bind_native(service.store, {
                "harness": "codex", "native_session_id": native_session,
                "process_identity": proof,
            })
            return observed

        service.adapters["observe_native"] = reconnect_after_observation

    def native_start(boundary):
        if boundary != "after_identity":
            return
        if change == "reconnected":
            return
        with service.store.read() as tx:
            attempt = tx.connection.execute("SELECT process_identity_json FROM job_attempts").fetchone()
            launched_proof = json.loads(attempt[0])
        hook = identity.bind_native(service.store, {
            "harness": "codex", "native_session_id": native_session,
            "process_identity": proof if change == "unrelated_start" else launched_proof,
        }, transport="hook")
        event = service.execute(hook["context"], Call("identity.event", {
            "state": "working", "event": "start", "native_run_id": "resumed-native-run",
        }, str(uuid.uuid4())))
        assert event["ok"] and event["data"]["applied"], event
        if change == "consent_revoked":
            refreshed = identity.context_from_token(service.store, hook["token"])
            paused = service.execute(refreshed, Call("identity.checkpoint", {
                "state": "paused", "note": "Do not continue this task",
            }, str(uuid.uuid4())))
            assert paused["ok"], paused
        with service.store.write() as tx:
            tx.connection.execute("DELETE FROM bindings WHERE id=?", (hook["context"].connection_id,))

    service.adapters["slow_handlers"]["job.execute"] = lambda active, operation: jobs.execute_operation(
        active, operation, fault=native_start,
    )
    operation_id = jobs.claim_due(service)[0]["operation_id"]
    result = service.run_operation(operation_id)
    expected = "succeeded" if change == "owned_start" else "failed" if change == "reconnected" else "uncertain"
    assert result["state"] == expected, result
    if change == "reconnected":
        assert spawned == [], "Presence invalidation must refuse before process launch"
    with service.store.read() as tx:
        job = tx.connection.execute("SELECT state,last_result_json FROM jobs").fetchone()
        assert job["state"] == expected, dict(job)
        if change == "owned_start":
            assert json.loads(job["last_result_json"])["returncode"] == 0
        elif change == "reconnected":
            assert json.loads(job["last_result_json"])["code"] == "NOT_AUTHORIZED"
        else:
            assert json.loads(job["last_result_json"])["phase"] == "cancelled_or_revoked"
