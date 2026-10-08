"""Storage budgets and compaction never discard coordination authority."""
from __future__ import annotations

import json
import struct

import pytest

from agentcoord import identity, storage, storage_codec
from agentcoord.application import build_service
from agentcoord.config import Config, load_config, register_workspace
from agentcoord.core import CoordinationError, canonical_json


@pytest.fixture
def service(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    return build_service(register_workspace(root, state_root=tmp_path / "state"))


def test_codec_exact_unicode_and_bounded_corrupt_decode():
    text = canonical_json({"text": "é🙂\n" * 10000})
    packed = storage_codec.encode(text)
    assert isinstance(packed, bytes)
    assert len(packed) < len(text.encode()) // 10
    assert storage_codec.decode(packed) == text
    assert storage_codec.decode("{}") == "{}"
    assert storage_codec.encode("{}") == "{}"
    for invalid in (packed[:-1], packed + b"extra", packed[:-10] + b"corruption",
                    b"unknown", packed[:len(storage_codec._MAGIC)] + struct.pack(">Q", 2**63) + packed[len(storage_codec._MAGIC)+8:]):
        with pytest.raises(ValueError):
            storage_codec.decode(invalid)


def test_storage_configuration_and_pressure_preserves_authority(service):
    path = service.workspace.root / ".agentcoord.toml"
    path.write_text("[storage]\nbudget_bytes=1048576\ndiagnostic_retention_days=7\nmaintenance_batch=2\n")
    config = load_config(service.workspace)
    assert (config.storage_budget_bytes, config.storage_diagnostic_retention_days,
            config.storage_maintenance_batch) == (1048576, 7, 2)
    path.write_text("[storage]\nmaintenance_batch=0\n")
    with pytest.raises(CoordinationError):
        load_config(service.workspace)
    assert Config().storage_budget_bytes == 512 * 1024 * 1024
    service.store.config = config
    with service.store.write() as tx:
        tx.connection.execute("INSERT INTO meta VALUES ('large_protected_receipt',?)",
                              (canonical_json("evidence" * 200000),))
    status = service.store.storage_status()
    assert status["over_budget"] and status["budget_kind"] == "soft" and status["guidance"]
    storage.maintain(service.store)
    with service.store.read() as tx:
        assert len(json.loads(tx.connection.execute(
            "SELECT value_json FROM meta WHERE key='large_protected_receipt'"
        ).fetchone()[0])) == 1600000


def test_bounded_diagnostic_pruning_preserves_current_actor(service):
    binding = identity.bind_native(service.store, {"harness": "codex", "native_session_id": "storage"})
    actor = binding["context"].actor_id
    service.store.config = Config(storage_maintenance_batch=2)
    with service.store.write() as tx:
        tx.connection.execute("DELETE FROM events WHERE actor_id=?", (actor,))
        tx.connection.execute("UPDATE actors SET archived=1 WHERE id=?", (actor,))
        for kind in ("ambiguous_event", "stale_event", "lifecycle", "ambiguous_event"):
            tx.event("identity", kind, actor, actor)
        tx.connection.execute("UPDATE events SET at_us=1 WHERE actor_id=?", (actor,))
    first = storage.maintain(service.store)
    assert first["scanned_events"] == 2 and first["pruned_diagnostics"] == 2
    second = storage.maintain(service.store)
    assert second["scanned_events"] == 2 and second["pruned_diagnostics"] == 0
    with service.store.write() as tx:
        tx.connection.execute("UPDATE actors SET archived=0 WHERE id=?", (actor,))
        tx.event("identity", "ambiguous_event", actor, actor)
        tx.connection.execute("UPDATE events SET at_us=1 WHERE actor_id=?", (actor,))
    storage.maintain(service.store)
    with service.store.read() as tx:
        assert tx.connection.execute("SELECT count(*) FROM events").fetchone()[0] == 3


def test_archive_compaction_exact_hashes_bounded_and_incomplete_preserved(service):
    original = canonical_json({"note": "source🙂" * 3000})
    destination = canonical_json([{"table": "actors", "values": "data" * 3000}])
    with service.store.write() as tx:
        for run, state in (("complete", "complete"), ("pending", "prepared")):
            tx.connection.execute("INSERT INTO import_runs VALUES (?,? ,?,'{}',1,NULL,0)", (run, "manifest", state))
        for i, run in enumerate(("complete", "complete", "pending")):
            tx.connection.execute("INSERT INTO import_records VALUES (?,?,?,?,?,?,?,?,?,?)",
                                  (str(i), "source", "project", "kind", str(i), original,
                                   "source-hash", "canonical-hash", destination, run))
    service.store.config = Config(storage_maintenance_batch=1)
    result = storage.maintain(service.store)
    assert result["scanned_archives"] == 1 and result["archive_bytes_saved"] > 0
    with service.store.read() as tx:
        rows = tx.connection.execute("SELECT * FROM import_records ORDER BY id").fetchall()
        assert isinstance(rows[0]["source_json"], bytes)
        assert isinstance(rows[1]["source_json"], str)
        assert isinstance(rows[2]["source_json"], str)
        assert storage_codec.decode(rows[0]["source_json"]) == original
        assert storage_codec.decode(rows[0]["destinations_json"]) == destination
        assert rows[0]["source_sha256"] == "source-hash"
        assert rows[0]["canonical_sha256"] == "canonical-hash"
    storage.maintain(service.store)
    storage.maintain(service.store)  # Wrap the completed-archive cursor.
    result = storage.maintain(service.store)
    assert result["archive_bytes_saved"] == 0
    with service.store.read() as tx:
        assert tx.connection.execute("PRAGMA auto_vacuum").fetchone()[0] == 2


@pytest.mark.parametrize("state", ["queued", "running", "uncertain"])
def test_old_archived_diagnostics_and_retry_authority_survive_unresolved_operation(service, state):
    binding = identity.bind_native(service.store, {"harness": "codex", "native_session_id": "unresolved"})
    context = binding["context"]
    with service.store.write() as tx:
        operation = service.enqueue(tx, context, "message.wake", {"exact": "🙂"}, key="stable-key")
        operation_id = operation["operation_id"]
        tx.connection.execute("UPDATE operations SET state=? WHERE id=?", (state, operation_id))
        tx.connection.execute("UPDATE actors SET archived=1 WHERE id=?", (context.actor_id,))
        diagnostic = tx.event("identity", "ambiguous_event", context.actor_id, context.actor_id)
        tx.connection.execute("UPDATE events SET at_us=1 WHERE sequence=?", (diagnostic,))
        before = dict(tx.connection.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone())
    result = storage.maintain(service.store)
    assert result["pruned_diagnostics"] == 0
    with service.store.read() as tx:
        assert tx.connection.execute("SELECT 1 FROM events WHERE sequence=?", (diagnostic,)).fetchone()
        assert dict(tx.connection.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()) == before


def test_reclamation_restores_retained_writer_timeout(service):
    with service.store.writer_lifespan():
        with service.store.write() as tx:
            tx.connection.execute("PRAGMA busy_timeout=4321")
        service.store.reclaim_storage()
        assert service.store._writer_connection.execute("PRAGMA busy_timeout").fetchone()[0] == 4321


def test_one_kib_threshold_compresses_small_repetitive_archives():
    text = canonical_json({"source": "evidence" * 160})
    assert 1024 < len(text.encode()) < 4096
    encoded = storage_codec.encode(text)
    assert isinstance(encoded, bytes)
    assert storage_codec.decode(encoded) == text
    small = "x" * 1023
    assert storage_codec.encode(small) == small


def test_pruning_preserves_pagination_highwater_when_every_event_is_eligible(service):
    binding = identity.bind_native(service.store, {"harness": "codex", "native_session_id": "highwater"})
    actor = binding["context"].actor_id
    with service.store.write() as tx:
        tx.connection.execute("DELETE FROM events WHERE actor_id=?", (actor,))
        tx.connection.execute("UPDATE actors SET archived=1 WHERE id=?", (actor,))
        for _ in range(3):
            tx.event("identity", "ambiguous_event", actor, actor)
        tx.connection.execute("UPDATE events SET at_us=1 WHERE actor_id=?", (actor,))
        highwater = tx.connection.execute("SELECT MAX(sequence) FROM events").fetchone()[0]
    result = storage.maintain(service.store)
    assert result["pruned_diagnostics"] == 2
    storage.maintain(service.store)  # Cursor wrap cannot remove the retained newest event.
    storage.maintain(service.store)
    with service.store.read() as tx:
        assert tx.connection.execute("SELECT MAX(sequence) FROM events").fetchone()[0] == highwater
        assert tx.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def test_empty_maintenance_does_not_create_or_rewrite_zero_cursors(service):
    storage.maintain(service.store)
    with service.store.write() as tx:
        assert not tx.connection.execute(
            "SELECT key FROM meta WHERE key IN ('storage_event_cursor','storage_archive_cursor')"
        ).fetchall()
        for cursor in ("storage_event_cursor", "storage_archive_cursor"):
            tx.connection.execute("INSERT INTO meta VALUES (?,'0')", (cursor,))
        for operation in ("INSERT", "UPDATE"):
            tx.connection.execute(f"""CREATE TRIGGER forbid_cursor_{operation.lower()}
                BEFORE {operation} ON meta
                WHEN NEW.key IN ('storage_event_cursor','storage_archive_cursor')
                BEGIN SELECT RAISE(ABORT,'Unchanged cursor must not be written'); END""")
    storage.maintain(service.store)
    with service.store.read() as tx:
        assert dict(tx.connection.execute(
            "SELECT key,value_json FROM meta WHERE key IN ('storage_event_cursor','storage_archive_cursor')"
        ).fetchall()) == {"storage_event_cursor": "0", "storage_archive_cursor": "0"}
