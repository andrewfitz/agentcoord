"""Imported legacy jobs satisfy the native schema and worker contract."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from types import SimpleNamespace

import pytest
from test_migrate import native_source

from agentcoord import (
    commits,
    decisions,
    identity,
    jobs,
    messages,
    migrate,
    readiness,
    work,
)
from agentcoord.config import Config
from agentcoord.core import Service
from agentcoord.store import Store


@pytest.fixture
def legacy_job(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    native_source(path, state="idle", job_state="pending")
    return path


@pytest.fixture
def destination(tmp_path):
    store = Store(tmp_path / "private" / "runtime.sqlite3", str(uuid.uuid4()))
    store.initialize((identity.SCHEMA, work.SCHEMA, messages.SCHEMA, decisions.SCHEMA,
                      readiness.SCHEMA, commits.SCHEMA, jobs.SCHEMA, migrate.SCHEMA))
    return store


def import_job(source, destination, payload, *, kind="reminder", state="pending"):
    with sqlite3.connect(source) as db:
        db.execute("UPDATE jobs SET kind=?,payload=?,state=?", (kind, json.dumps(payload), state))
    manifest = migrate.inspect_sources([migrate.SourceSpec(source, "/selected")])
    prepared = migrate.prepare_import(manifest, destination.workspace_id)
    result = migrate.apply_import(destination, prepared, run_id="jobs")
    assert result["verification"]["valid"], result
    return prepared


@pytest.mark.parametrize("state", ["completed", "pending", "running"])
def test_legacy_resume_without_reminder_deadline_imports_and_stays_fenced(
    legacy_job, destination, state,
):
    prepared = import_job(legacy_job, destination, {
        "note": "Resume safely", "prompt": "Resume safely",
        "resume_context": {"generation": "legacy-generation"},
        "timeout": 90,
    }, kind="resume", state=state)
    with destination.read() as tx:
        job = tx.connection.execute("SELECT * FROM jobs").fetchone()
        assert job["delivery_deadline_us"] >= job["due_us"]
        assert json.loads(job["payload_json"]) == {"note": "Resume safely", "timeout_seconds": 90}
        assert job["state"] == ("succeeded" if state == "completed" else "uncertain")
        assert tx.connection.execute("SELECT archived FROM actors").fetchone()[0] == 1
        assert tx.connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 0
    assert migrate.verify_import(destination, prepared.manifest)["valid"]


def test_pending_legacy_reminder_delivers_original_body_after_native_rebind(legacy_job, destination):
    deadline = time.time() + 60
    import_job(legacy_job, destination, {"body": "Remember the original note", "delivery_deadline": deadline})
    with destination.read() as tx:
        job = dict(tx.connection.execute("SELECT * FROM jobs").fetchone())
        assert job["delivery_deadline_us"] == round(deadline * 1_000_000)
        retained = tx.connection.execute("SELECT source_json FROM import_records WHERE source_kind='jobs'").fetchone()
        assert json.loads(json.loads(retained[0])["payload"]) == {
            "body": "Remember the original note", "delivery_deadline": deadline,
        }
    with destination.write(maintenance=True) as tx:
        tx.connection.execute("UPDATE meta SET value_json='\"active\"' WHERE key='service_state'")
    binding = identity.bind_native(destination, {"harness": "codex", "native_session_id": "native-one"})
    assert binding["context"].actor_id == job["actor_id"]
    service = Service(destination, SimpleNamespace(root=legacy_job.parent), Config(),
                      (*jobs.operations(), *messages.operations()),
                      {"slow_handlers": {"job.execute": jobs.execute_operation}})
    admitted = jobs.claim_due(service)
    assert len(admitted) == 1
    result = service.run_operation(admitted[0]["operation_id"])
    assert result["state"] == "succeeded", result
    with destination.read() as tx:
        reminder = tx.connection.execute("SELECT subject,body_utf8 FROM messages WHERE kind='reminder'").fetchone()
        assert (reminder["subject"], reminder["body_utf8"].decode()) == ("Scheduled reminder", "Remember the original note")
        assert tx.connection.execute("SELECT state FROM jobs").fetchone()[0] == "succeeded"
        assert tx.connection.execute("SELECT COUNT(*) FROM job_notifications").fetchone()[0] == 1
