"""Migration proofs use real source SQLite and the actual destination schema."""
from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

import pytest

from agentcoord import migrate


def native_source(path: Path, *, state="completed", grant=False, job_state="completed"):
    db = sqlite3.connect(path)
    db.executescript("""
        PRAGMA user_version=11;
        CREATE TABLE sessions(session_key TEXT PRIMARY KEY,agent_name TEXT,registration_token TEXT,
            task TEXT,harness TEXT,pane_id TEXT,process_id INTEGER,state TEXT,last_seen REAL,checkpoint TEXT,metadata TEXT);
        CREATE TABLE commits(id INTEGER PRIMARY KEY,session_key TEXT,created_at REAL,finished_at REAL,mode TEXT,paths TEXT);
        CREATE TABLE jobs(id INTEGER PRIMARY KEY,kind TEXT,session_key TEXT,due_at REAL,payload TEXT,state TEXT,
            attempts INTEGER,claim_token TEXT,worker TEXT,lease_until REAL,error TEXT);
        CREATE TABLE actionable_requests(id TEXT PRIMARY KEY,sender_key TEXT,recipient_key TEXT,parent_key TEXT,
            recipient_generation TEXT,parent_generation TEXT,task TEXT,subject TEXT,body TEXT,request_key TEXT,state TEXT,
            routing_state TEXT,deadline REAL,created_at REAL,resolved_at REAL,resolved_by TEXT,response TEXT);
        CREATE TABLE readiness_receipts(id INTEGER PRIMARY KEY,producer_key TEXT,artifact_key TEXT,observed TEXT,
            evidence TEXT,status TEXT,created_at REAL,producer_generation TEXT);
        CREATE TABLE message_presentations(session_key TEXT,message_id INTEGER,first_presented_at REAL,
            last_presented_at REAL,presentation_count INTEGER,PRIMARY KEY(session_key,message_id));
        CREATE TABLE delivery_outbox(id INTEGER PRIMARY KEY,sender_key TEXT,recipient_key TEXT,recipient_generation TEXT,
            kind TEXT,request_id TEXT,receipt_id INTEGER,payload TEXT,idempotency_key TEXT,state TEXT,attempts INTEGER,
            due_at REAL,created_at REAL,delivered_at REAL,last_error TEXT,result TEXT);
    """)
    metadata = {"lifecycle_generation": "legacy-generation", "native_session_id": "native-one",
        "activity": {"task": "task", "paths": ["owned.py"], "note": "Preserve the parser change",
            "evidence": "verification:one", "state": "completed", "at": 10}}
    if grant:
        metadata.update(commit_until=100, commit_request_until=100, process_identity={"pid": 123, "start": "old"})
    db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
        "codex:native-one:", "SameLabel", "obsolete-proof-secret", "task", "codex", None, None,
        state, 10, '{"note":"retain checkpoint","at":10}', json.dumps(metadata)))
    db.execute("INSERT INTO commits VALUES (1,?,10,?,'native','[\"owned.py\"]')",
               ("codex:native-one:", None if grant else 20))
    db.execute("INSERT INTO jobs VALUES (1,'reminder',?,10,'{\"body\":\"remember\",\"delivery_deadline\":100}',?,1,NULL,NULL,NULL,NULL)",
               ("codex:native-one:", job_state))
    db.execute("INSERT INTO actionable_requests VALUES ('request',?,?,NULL,?,NULL,'task','Need decision','Body','key','open','waiting',100,10,NULL,NULL,NULL)",
               ("codex:native-one:", "codex:native-one:", "legacy-generation"))
    db.execute("INSERT INTO readiness_receipts VALUES (1,?,'build','{\"owned.py\":\"hash\"}','check:one','ready',10,?)",
               ("codex:native-one:", "legacy-generation"))
    db.execute("INSERT INTO message_presentations VALUES (?,1,10,10,1)", ("codex:native-one:",))
    db.execute("INSERT INTO delivery_outbox VALUES (1,?,?,?,'request','request',NULL,'{}','delivery-key','delivered',1,10,10,10,NULL,?)",
               ("codex:native-one:", "codex:native-one:", "legacy-generation", '{"message_ids":[1]}'))
    db.commit()
    db.close()


def conversation_source(path: Path, *, body="A useful message", project="/selected", attachment=None, missing_sender=False):
    db = sqlite3.connect(path)
    db.executescript("""
        PRAGMA user_version=1;
        CREATE TABLE projects(id INTEGER PRIMARY KEY,human_key TEXT,slug TEXT);
        CREATE TABLE agents(id INTEGER PRIMARY KEY,project_id INTEGER,name TEXT,program TEXT,model TEXT,
            task_description TEXT,inception_ts,last_active_ts,registration_token TEXT,contact_policy TEXT);
        CREATE TABLE messages(id INTEGER PRIMARY KEY,project_id INTEGER,sender_id INTEGER,thread_id TEXT,
            topic TEXT,subject TEXT,body_md TEXT,importance TEXT,ack_required INTEGER,created_ts,
            recipients_json TEXT,attachments TEXT);
        CREATE TABLE message_recipients(message_id INTEGER,agent_id INTEGER,kind TEXT,read_ts,ack_ts,
            PRIMARY KEY(message_id,agent_id));
    """)
    db.execute("INSERT INTO projects VALUES (1,?,'selected')", (project,))
    db.execute("INSERT INTO agents VALUES (1,1,'SameLabel','client','model','task','2026-01-01T00:00:00Z',NULL,'mail-secret','open')")
    db.execute("INSERT INTO messages VALUES (1,1,?,'thread',NULL,'Subject',?,'normal',1,'2026-01-01T00:00:00Z','[]',?)",
               (99 if missing_sender else 1, body, json.dumps([attachment] if attachment else [])))
    db.execute("INSERT INTO message_recipients VALUES (1,1,'to','2026-01-02T00:00:00Z','2026-01-03T00:00:00Z')")
    db.commit()
    db.close()


@pytest.fixture
def sources(tmp_path):
    native, conversations = tmp_path / "native.sqlite3", tmp_path / "conversations.sqlite3"
    native_source(native)
    conversation_source(conversations)
    return [migrate.SourceSpec(native, "/selected"), migrate.SourceSpec(conversations, "/selected")]


@pytest.fixture
def destination(tmp_path):
    from agentcoord import commits, decisions, identity, jobs, messages, readiness, work
    from agentcoord.store import Store
    store = Store(tmp_path / "state" / "coordinator.sqlite3", str(uuid.uuid4()))
    store.initialize((identity.SCHEMA, work.SCHEMA, messages.SCHEMA, decisions.SCHEMA,
                      readiness.SCHEMA, commits.SCHEMA, jobs.SCHEMA, migrate.SCHEMA))
    return store


def test_source_snapshot_is_private_consistent_and_does_not_modify_source(sources, tmp_path):
    before = [spec.path.read_bytes() for spec in sources]
    snapshots = migrate.snapshot_sources(sources, tmp_path / "snapshots")
    manifest = migrate.inspect_sources(snapshots)
    assert len(manifest.records) > 10
    assert all(spec.path.stat().st_mode & 0o777 == 0o600 for spec in snapshots)
    assert [spec.path.read_bytes() for spec in sources] == before


def test_manifest_roundtrip_validates_complete_private_content(sources, tmp_path):
    manifest = migrate.inspect_sources(sources)
    path = migrate.save_manifest(manifest, tmp_path/'manifest.json')
    restored = migrate.load_manifest(path)
    assert restored.sha256 == manifest.sha256 and restored.records == manifest.records
    assert path.stat().st_mode & 0o777 == 0o600
    document = json.loads(path.read_text())
    document['records'][0]['data']['tampered'] = True
    path.write_text(json.dumps(document))
    with pytest.raises(migrate.MigrationError) as caught:
        migrate.load_manifest(path)
    assert caught.value.code == 'INVALID_MANIFEST'


def test_known_source_fingerprint_and_exact_project_selection(sources):
    manifest = migrate.inspect_sources(sources)
    assert [row["format"] for row in manifest.sources] == ["native-sqlite-v11", "conversation-sqlite-v1"]
    with pytest.raises(migrate.MigrationError, match="project selection") as caught:
        migrate.inspect_sources([migrate.SourceSpec(sources[1].path, "/other")])
    assert caught.value.code == "WRONG_SOURCE_PROJECT"
    with sqlite3.connect(sources[0].path) as db:
        db.execute("PRAGMA user_version=12")
    with pytest.raises(migrate.MigrationError) as caught:
        migrate.inspect_sources(sources)
    assert caught.value.code == "UNSUPPORTED_SOURCE_SCHEMA"


def test_conversation_integer_microseconds_do_not_scale_again(sources):
    timestamp = 1788933451884037
    with sqlite3.connect(sources[1].path) as db:
        db.execute('UPDATE agents SET inception_ts=?', (timestamp,))
        db.execute('UPDATE messages SET created_ts=?', (timestamp,))
        db.execute('UPDATE message_recipients SET ack_ts=?', (timestamp,))
    prepared = migrate.prepare_import(migrate.inspect_sources(sources), 'workspace')
    historical_actor = next(target for record in prepared.records for target in record.destinations
                            if target.table == 'actors' and target.values['harness'] == 'historical')
    message = next(target for record in prepared.records for target in record.destinations if target.table == 'messages')
    assert historical_actor.values['created_us'] == timestamp
    assert message.values['created_us'] == timestamp


def test_repeated_readiness_artifact_preserves_all_versions(sources, destination):
    with sqlite3.connect(sources[0].path) as db:
        db.execute("INSERT INTO readiness_receipts SELECT 2,producer_key,artifact_key,observed,'check:two',status,20,producer_generation FROM readiness_receipts WHERE id=1")
    manifest = migrate.inspect_sources(sources)
    result = migrate.apply_import(destination, migrate.prepare_import(manifest, destination.workspace_id), run_id='revisions')
    assert result['state'] == 'complete'
    with destination.read() as tx:
        receipts = list(tx.connection.execute('SELECT version,evidence_json FROM receipts ORDER BY version'))
        assert [(row['version'], json.loads(row['evidence_json'])['reference']) for row in receipts] == [(1, 'check:one'), (2, 'check:two')]


def test_failed_diagnostic_preserves_open_decision_without_blocking_import(sources, destination):
    with sqlite3.connect(sources[0].path) as db:
        db.execute("UPDATE delivery_outbox SET kind='diagnostic',state='failed',result=NULL,last_error=?",
                   (json.dumps({'reason': 'BACKEND_REJECTED', 'stage': 'tool', 'retryable': True}),))
        db.execute("UPDATE actionable_requests SET routing_state='no_parent'")
    manifest = migrate.inspect_sources(sources)
    result = migrate.apply_import(destination, migrate.prepare_import(manifest, destination.workspace_id), run_id='diagnostic')
    assert result['state'] == 'complete'
    with destination.read() as tx:
        assert tuple(tx.connection.execute('SELECT state,routing_state FROM decisions').fetchone()) == ('open', 'diagnostic')
        issue = tx.connection.execute("SELECT required,detail_json FROM import_issues WHERE code='FAILED_SOURCE_SEND'").fetchone()
        assert issue['required'] == 0
        assert json.loads(issue['detail_json'])['last_error']['reason'] == 'BACKEND_REJECTED'


@pytest.mark.parametrize(('kind', 'state', 'code'), [
    ('answer', 'failed', 'FAILED_SOURCE_SEND'),
    ('diagnostic', 'sending', 'AMBIGUOUS_SOURCE_SEND'),
])
def test_authoritative_or_uncertain_send_remains_blocked(sources, destination, kind, state, code):
    with sqlite3.connect(sources[0].path) as db:
        db.execute('UPDATE delivery_outbox SET kind=?,state=?,result=NULL', (kind, state))
    manifest = migrate.inspect_sources(sources)
    result = migrate.apply_import(destination, migrate.prepare_import(manifest, destination.workspace_id), run_id='blocked-send')
    assert result['state'] == 'blocked'
    assert any(issue['code'] == code and issue['required'] for issue in result['issues'])
    with destination.read() as tx:
        assert tx.connection.execute('SELECT state FROM decisions').fetchone()[0] == 'open'
        assert tx.connection.execute('SELECT COUNT(*) FROM operations').fetchone()[0] == 0


def test_imported_jobs_have_verified_native_history_events(sources, destination):
    manifest = migrate.inspect_sources(sources)
    result = migrate.apply_import(destination, migrate.prepare_import(manifest, destination.workspace_id), run_id='jobs-events')
    assert result['state'] == 'complete'
    with destination.write(maintenance=True) as tx:
        assert tx.connection.execute("SELECT COUNT(*) FROM events WHERE domain='jobs' AND kind='imported'").fetchone()[0] == 1
        tx.connection.execute("DELETE FROM events WHERE domain='jobs'")
    verification = migrate.verify_import(destination, manifest)
    assert not verification['valid']
    assert 'MISSING_JOB_EVENT' in {failure['code'] for failure in verification['failures']}


def test_import_fences_normal_writers_until_explicit_activation(sources, destination):
    from agentcoord.core import CoordinationError
    manifest = migrate.inspect_sources(sources)
    migrate.apply_import(destination, migrate.prepare_import(manifest, destination.workspace_id), run_id='fence')
    with pytest.raises(CoordinationError) as caught, destination.write():
        pass
    assert caught.value.code == 'AUTHORITY_FENCED'
    with destination.read() as tx:
        assert json.loads(tx.connection.execute("SELECT value_json FROM meta WHERE key='service_state'").fetchone()[0]) == 'importing'


def test_preparation_preserves_native_identity_and_never_infers_handling(sources):
    manifest = migrate.inspect_sources(sources)
    prepared = migrate.prepare_import(manifest, "workspace")
    assert len(prepared.records) == len(manifest.records)
    actors = [target for record in prepared.records for target in record.destinations if target.table == "actors"]
    assert len(actors) == 2 and len({actor.values['id'] for actor in actors}) == 2
    assert all(actor.values['archived'] == 1 for actor in actors)
    recipient = next(target for record in prepared.records for target in record.destinations if target.table == "recipients")
    native = next(actor for actor in actors if actor.values['harness'] == 'codex')
    assert recipient.values['actor_id'] == native.values['id']  # Exact delivery receipt supplies this relation.
    assert recipient.values['handled_us'] is None
    assert recipient.values['acknowledged_us'] is not None and recipient.values['presented_us'] is not None
    encoded = migrate.canonical_json([record.source.data for record in prepared.records])
    assert 'obsolete-proof-secret' not in encoded and 'mail-secret' not in encoded


def test_import_repeat_restores_all_records_hashes_and_large_body(sources, destination):
    with sqlite3.connect(sources[1].path) as db:
        db.execute("UPDATE messages SET body_md=?", ("é" * 40000,))
    manifest = migrate.inspect_sources(sources)
    prepared = migrate.prepare_import(manifest, destination.workspace_id)
    first = migrate.apply_import(destination, prepared, run_id="first", batch_limit=2)
    assert first['state'] == 'complete' and first['verification']['valid']
    second = migrate.apply_import(destination, prepared, run_id="repeat", batch_limit=3)
    assert second['state'] == 'complete'
    with destination.read() as tx:
        assert tx.connection.execute('SELECT count(*) FROM provenance').fetchone()[0] == len(manifest.records)
        message = tx.connection.execute('SELECT body_utf8,body_bytes FROM messages').fetchone()
        assert message['body_utf8'].decode() == 'é' * 40000 and message['body_bytes'] == 80000
        assert tx.connection.execute('SELECT count(*) FROM messages').fetchone()[0] == 1
        assert tx.connection.execute('SELECT handled_us FROM recipients').fetchone()[0] is None


def test_interrupted_committed_batch_resumes_without_duplicate_identities(sources, destination, monkeypatch):
    manifest = migrate.inspect_sources(sources)
    prepared = migrate.prepare_import(manifest, destination.workspace_id)
    original = migrate._write_batch
    calls = 0
    def lose_response(*args):
        nonlocal calls
        original(*args)
        calls += 1
        if calls == 1:
            raise ConnectionError('response lost after durable batch')
    monkeypatch.setattr(migrate, '_write_batch', lose_response)
    with pytest.raises(ConnectionError):
        migrate.apply_import(destination, prepared, run_id='interrupted', batch_limit=2)
    with destination.read() as tx:
        assert tx.connection.execute('SELECT checkpoint FROM import_runs').fetchone()[0] == 2
    monkeypatch.setattr(migrate, '_write_batch', original)
    result = migrate.apply_import(destination, prepared, run_id='interrupted', batch_limit=2)
    assert result['state'] == 'complete'
    with destination.read() as tx:
        assert tx.connection.execute('SELECT count(*) FROM actors').fetchone()[0] == 2


def test_storage_failure_rolls_back_entire_batch_and_checkpoint(sources, destination, monkeypatch):
    prepared = migrate.prepare_import(migrate.inspect_sources(sources), destination.workspace_id)
    original = migrate._insert_destination
    calls = 0
    def fail_during_batch(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError('disk unavailable')
        original(*args)
    monkeypatch.setattr(migrate, '_insert_destination', fail_during_batch)
    with pytest.raises(OSError):
        migrate.apply_import(destination, prepared, run_id='storage', batch_limit=2)
    with destination.read() as tx:
        assert tx.connection.execute('SELECT count(*) FROM actors').fetchone()[0] == 0
        assert tx.connection.execute('SELECT count(*) FROM provenance').fetchone()[0] == 0
        assert tx.connection.execute('SELECT checkpoint FROM import_runs').fetchone()[0] == 0


def test_native_backup_restores_logical_import(sources, destination, tmp_path):
    manifest = migrate.inspect_sources(sources)
    migrate.apply_import(destination, migrate.prepare_import(manifest, destination.workspace_id), run_id='backup')
    backup = tmp_path/'restored'/'runtime.sqlite3'
    backup.parent.mkdir()
    destination.backup(backup)
    from agentcoord.store import Store
    restored = Store(backup, destination.workspace_id)
    verification = migrate.verify_import(restored, manifest)
    assert verification['valid'] and verification['checked_records'] == len(manifest.records)


def test_missing_required_link_retains_fact_but_blocks_cutover(sources, destination):
    with sqlite3.connect(sources[1].path) as db:
        db.execute('UPDATE messages SET sender_id=99')
    manifest = migrate.inspect_sources(sources)
    result = migrate.apply_import(destination, migrate.prepare_import(manifest, destination.workspace_id), run_id='missing')
    assert result['state'] == 'blocked' and result['required_issues'] > 0
    assert any(issue['code'] == 'MISSING_ACTOR' for issue in result['issues'])
    with destination.read() as tx:
        assert tx.connection.execute('SELECT count(*) FROM import_records').fetchone()[0] == len(manifest.records)


def test_unknown_external_grant_and_job_never_become_live_authority(sources, destination):
    sources[0].path.unlink()
    native_source(sources[0].path, grant=True, job_state='running')
    manifest = migrate.inspect_sources(sources)
    result = migrate.apply_import(destination, migrate.prepare_import(manifest, destination.workspace_id), run_id='uncertain')
    assert result['state'] == 'blocked'
    assert {issue['code'] for issue in result['issues']} >= {'UNRECONCILED_SOURCE_JOB', 'UNRECONCILED_SOURCE_GRANT'}
    with destination.read() as tx:
        assert tx.connection.execute('SELECT state FROM commit_grants').fetchone()[0] == 'uncertain'
        assert tx.connection.execute('SELECT state FROM jobs').fetchone()[0] == 'uncertain'


def test_changed_source_identity_cannot_rewrite_durable_import(sources, destination):
    manifest = migrate.inspect_sources(sources)
    migrate.apply_import(destination, migrate.prepare_import(manifest, destination.workspace_id), run_id='before')
    with sqlite3.connect(sources[1].path) as db:
        db.execute("UPDATE messages SET body_md='Changed content'")
    changed = migrate.inspect_sources(sources)
    with pytest.raises(migrate.MigrationError) as caught:
        migrate.apply_import(destination, migrate.prepare_import(changed, destination.workspace_id), run_id='after')
    assert caught.value.code == 'IMPORT_CONFLICT'
    with destination.read() as tx:
        assert tx.connection.execute('SELECT body_utf8 FROM messages').fetchone()[0] == b'A useful message'


def test_verification_detects_destination_corruption_and_missing_mapping(sources, destination):
    manifest = migrate.inspect_sources(sources)
    migrate.apply_import(destination, migrate.prepare_import(manifest, destination.workspace_id), run_id='verify')
    with destination.write(maintenance=True) as tx:
        tx.connection.execute("UPDATE import_records SET source_json='{}' WHERE source_kind='messages'")
        tx.connection.execute("DELETE FROM provenance WHERE source_kind='jobs'")
    result = migrate.verify_import(destination, manifest)
    assert not result['valid']
    assert {failure['code'] for failure in result['failures']} >= {'CANONICAL_HASH_MISMATCH', 'MISSING_PROVENANCE'}


def test_attachments_verified_and_changed_reference_blocks(sources, destination, tmp_path):
    attachment = tmp_path / 'retained.txt'
    attachment.write_text('immutable attachment')
    with sqlite3.connect(sources[1].path) as db:
        db.execute('UPDATE messages SET attachments=?', (json.dumps([{'path':'retained.txt'}]),))
    sources[1] = migrate.SourceSpec(sources[1].path, '/selected', attachments_root=tmp_path)
    manifest = migrate.inspect_sources(sources)
    migrate.apply_import(destination, migrate.prepare_import(manifest, destination.workspace_id), run_id='attachments')
    attachment.write_text('changed')
    assert not migrate.verify_import(destination, manifest)['valid']
