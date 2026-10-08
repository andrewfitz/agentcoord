"""Bounded maintenance of dispensable diagnostics, never coordination authority."""
from __future__ import annotations

import json

from . import storage_codec
from .core import canonical_json


def maintain(store) -> dict:
    """Scan a bounded event page and discard only old archived-actor diagnostics.

    Every domain record, retry receipt, import source and external-effect record
    remains durable. The cursor is maintenance progress, not an event consumer's
    high-water mark. It wraps so later archival is eventually reconsidered.
    """
    with store.write() as tx:
        previous = tx.connection.execute(
            "SELECT value_json FROM meta WHERE key='storage_event_cursor'"
        ).fetchone()
        after = json.loads(previous[0]) if previous else 0
        rows = tx.connection.execute(
            "SELECT sequence FROM events WHERE sequence>? ORDER BY sequence LIMIT ?",
            (after, store.config.storage_maintenance_batch),
        ).fetchall()
        cutoff = tx.now_us - store.config.storage_diagnostic_retention_days * 86400 * 1_000_000
        deleted = 0
        if rows:
            last = rows[-1][0]
            # Pagination derives its high-water mark from extant events. Preserve
            # the newest event even if every remaining diagnostic is expendable.
            highwater = tx.connection.execute("SELECT MAX(sequence) FROM events").fetchone()[0]
            # Only ignored generation diagnostics are expendable. Current actor
            # diagnostics and any actor with an unresolved operation are retained.
            deleted = tx.connection.execute(
                """DELETE FROM events WHERE sequence>? AND sequence<=? AND at_us<? AND sequence<?
                AND domain='identity' AND kind IN ('ambiguous_event','stale_event')
                AND EXISTS (SELECT 1 FROM actors a WHERE a.id=events.actor_id AND a.archived=1)
                AND NOT EXISTS (SELECT 1 FROM operations o WHERE o.actor_id=events.actor_id
                    AND o.state IN ('queued','running','uncertain'))""",
                (after, last, cutoff, highwater),
            ).rowcount
        else:
            last = 0
        archive = compact_archives(tx, store.config.storage_maintenance_batch)
        if last != after:
            tx.connection.execute(
                "INSERT OR REPLACE INTO meta(key,value_json) VALUES ('storage_event_cursor',?)",
                (canonical_json(last),),
            )
    reclamation = store.reclaim_storage()
    return {"scanned_events": len(rows), "pruned_diagnostics": deleted,
            **archive, **reclamation, **store.storage_status()}


def compact_archives(tx, limit: int) -> dict:
    """Compress complete import archives with bounded rows and 4 MiB of input/pass.

    SQLite's TEXT affinity permits the codec's tagged BLOB. Plain TEXT and tagged
    BLOB are explicit storage representations of the same UTF-8 document; domain
    records, provenance and canonical hashes are unchanged.
    """
    previous = tx.connection.execute(
        "SELECT value_json FROM meta WHERE key='storage_archive_cursor'"
    ).fetchone()
    after = json.loads(previous[0]) if previous else 0
    rows = tx.connection.execute(
        """SELECT r.rowid,length(CAST(r.source_json AS BLOB)) +
        length(CAST(r.destinations_json AS BLOB)) AS size FROM import_records r
        JOIN import_runs i ON i.id=r.import_run_id WHERE r.rowid>? AND i.state='complete'
        ORDER BY r.rowid LIMIT ?""", (after, limit),
    ).fetchall()
    remaining, saved, scanned, oversized, last = 4 * 1024 * 1024, 0, 0, 0, after
    for row in rows:
        if row["size"] > 4 * 1024 * 1024:
            oversized += 1
            scanned += 1
            last = row["rowid"]
            continue
        if row["size"] > remaining:
            break
        remaining -= row["size"]
        value = tx.connection.execute(
            "SELECT source_json,destinations_json FROM import_records WHERE rowid=?", (row["rowid"],)
        ).fetchone()
        encoded = []
        for original in value:
            if isinstance(original, bytes):
                encoded.append(original)
            else:
                encoded.append(storage_codec.encode(original))
        if any(isinstance(old, str) and isinstance(new, bytes) for old, new in zip(value, encoded)):
            tx.connection.execute(
                "UPDATE import_records SET source_json=?,destinations_json=? WHERE rowid=?",
                (*encoded, row["rowid"]),
            )
            saved += row["size"] - sum(len(v.encode("utf-8") if isinstance(v, str) else v) for v in encoded)
        scanned += 1
        last = row["rowid"]
    if not rows:
        last = 0
    if last != after:
        tx.connection.execute(
            "INSERT OR REPLACE INTO meta(key,value_json) VALUES ('storage_archive_cursor',?)",
            (canonical_json(last),),
        )
    return {"scanned_archives": scanned, "archive_bytes_saved": saved,
            "oversized_archives_skipped": oversized}
