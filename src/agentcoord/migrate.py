"""Offline, resumable import of versioned coordination SQLite sources.

Source facts are an immutable migration archive. Domain tables, not that archive,
own executable destination state. All paths and project selectors are supplied by
the operator; import never discovers or changes a live source service.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from . import storage_codec

SCHEMA = (
    """CREATE TABLE import_runs (
        id TEXT PRIMARY KEY, source_manifest_sha256 TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('prepared','applying','blocked','complete')),
        source_metadata_json TEXT NOT NULL, created_us INTEGER NOT NULL,
        completed_us INTEGER, checkpoint INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE import_records (
        id TEXT PRIMARY KEY, source_namespace TEXT NOT NULL, source_project TEXT NOT NULL,
        source_kind TEXT NOT NULL, source_id TEXT NOT NULL, source_json TEXT NOT NULL,
        source_sha256 TEXT NOT NULL, canonical_sha256 TEXT NOT NULL,
        destinations_json TEXT NOT NULL, import_run_id TEXT NOT NULL REFERENCES import_runs(id),
        UNIQUE(source_namespace,source_project,source_kind,source_id))""",
    """CREATE TABLE provenance (
        source_namespace TEXT NOT NULL, source_project TEXT NOT NULL,
        source_kind TEXT NOT NULL, source_id TEXT NOT NULL,
        destination_kind TEXT NOT NULL, destination_id TEXT NOT NULL,
        source_sha256 TEXT NOT NULL, canonical_sha256 TEXT NOT NULL,
        import_run_id TEXT NOT NULL REFERENCES import_runs(id),
        UNIQUE(source_namespace,source_project,source_kind,source_id))""",
    """CREATE TABLE import_issues (
        id TEXT PRIMARY KEY, import_run_id TEXT NOT NULL REFERENCES import_runs(id),
        source_ref_json TEXT NOT NULL, code TEXT NOT NULL, required INTEGER NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('open','resolved')),
        detail_json TEXT NOT NULL)""",
    "CREATE INDEX import_issues_pending ON import_issues(import_run_id,required,state)",
)

_NAMESPACE = uuid.UUID("c7c48979-8658-472b-9c19-e0c05f418c93")
_SECRET_KEYS = frozenset({"registration_token", "token", "token_hash", "contact_policy"})
_FORMATS = {
    "native-sqlite-v11": (11, {
        "sessions": {"session_key", "agent_name", "metadata", "checkpoint", "state", "harness"},
        "commits": {"id", "session_key", "created_at", "finished_at", "mode", "paths"},
        "jobs": {"id", "kind", "session_key", "due_at", "payload", "state", "claim_token"},
        "actionable_requests": {"id", "sender_key", "recipient_key", "state", "request_key"},
        "readiness_receipts": {"id", "producer_key", "artifact_key", "observed"},
    }),
    "conversation-sqlite-v1": (1, {
        "projects": {"id", "human_key"},
        "agents": {"id", "project_id", "name"},
        "messages": {"id", "project_id", "sender_id", "body_md", "attachments"},
        "message_recipients": {"message_id", "agent_id", "kind", "read_ts", "ack_ts"},
    }),
}


class MigrationError(ValueError):
    """An explicit offline failure; never a healthy empty import."""

    def __init__(self, code: str, message: str, **details: Any):
        super().__init__(message)
        self.code, self.details = code, details


def canonical_json(value: Any) -> str:
    def safe(item: Any) -> Any:
        if isinstance(item, bytes):
            return {"$bytes_hex": item.hex()}
        if isinstance(item, dict):
            return {key: safe(child) for key, child in item.items()}
        if isinstance(item, (tuple, list)):
            return [safe(child) for child in item]
        return item
    return json.dumps(safe(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _id(*parts: Any) -> str:
    return str(uuid.uuid5(_NAMESPACE, canonical_json(parts)))


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key in _SECRET_KEYS:
                continue
            normalized = item
            if isinstance(item, str) and (key.endswith("_json") or key in {
                "metadata", "checkpoint", "payload", "result", "owner_identity", "path_intents"}):
                with suppress(ValueError, TypeError):
                    normalized = canonical_json(_clean(json.loads(item)))
                # Typed consumers explicitly quarantine required malformed JSON.
            result[key] = _clean(normalized)
        return result
    if isinstance(value, list):
        return [_clean(item) for item in value]
    return value


def _json(value: Any, *, default: Any = None) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError) as error:
        raise MigrationError("INVALID_SOURCE_JSON", "A required source JSON value is invalid") from error


def _us(value: Any, *, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise MigrationError("INVALID_SOURCE_TIME", "Boolean source time is invalid")
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise MigrationError("INVALID_SOURCE_TIME", "Nonfinite source time is invalid")
        converted = round(value * 1_000_000)
        if not -(2**63) <= converted < 2**63:
            raise MigrationError("INVALID_SOURCE_TIME", "Source time exceeds SQLite integer range")
        return converted
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return round(parsed.timestamp() * 1_000_000)
        except ValueError as error:
            raise MigrationError("INVALID_SOURCE_TIME", "Source timestamp is invalid") from error
    raise MigrationError("INVALID_SOURCE_TIME", "Unsupported source timestamp type")


def _conversation_us(value: Any, *, default: int = 0) -> int:
    # This source format stores integer UTC microseconds; older rows may retain ISO text.
    if type(value) is int:
        if not -(2**63) <= value < 2**63:
            raise MigrationError("INVALID_SOURCE_TIME", "Conversation timestamp exceeds SQLite integer range")
        return value
    if isinstance(value, float):
        raise MigrationError("INVALID_SOURCE_TIME", "Conversation numeric timestamp must be integer microseconds")
    return _us(value, default=default)


def _quoted(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _readonly(path: Path) -> sqlite3.Connection:
    if not path.is_file() or path.is_symlink():
        raise MigrationError("SOURCE_UNAVAILABLE", "Source must be an existing regular SQLite file")
    connection = sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


@dataclass(frozen=True)
class SourceSpec:
    path: Path
    project: str
    format: str | None = None
    namespace: str | None = None
    project_id: int | None = None
    attachments_root: Path | None = None


@dataclass(frozen=True)
class SourceRecord:
    namespace: str
    project: str
    kind: str
    source_id: str
    source_sha256: str
    data: dict[str, Any]

    @property
    def reference(self) -> dict[str, str]:
        return {"namespace": self.namespace, "project": self.project,
                "kind": self.kind, "id": self.source_id}

    @property
    def destination_id(self) -> str:
        return _id(self.reference)


@dataclass(frozen=True)
class SourceManifest:
    sources: tuple[dict[str, Any], ...]
    records: tuple[SourceRecord, ...]
    sha256: str

    def summary(self) -> dict[str, Any]:
        return {"sha256": self.sha256, "sources": list(self.sources), "records": len(self.records)}


@dataclass(frozen=True)
class DestinationRow:
    table: str
    identity: dict[str, Any]
    values: dict[str, Any]


@dataclass(frozen=True)
class PreparedRecord:
    source: SourceRecord
    destinations: tuple[DestinationRow, ...] = ()

    @property
    def canonical_sha256(self) -> str:
        return _sha({"source": self.source.data, "destinations": [
            {"table": row.table, "identity": row.identity, "values": row.values}
            for row in self.destinations]})


@dataclass(frozen=True)
class PreparedImport:
    manifest: SourceManifest
    workspace_id: str
    records: tuple[PreparedRecord, ...]
    issues: tuple[dict[str, Any], ...] = ()


def snapshot_sources(source_specs: list[SourceSpec], directory: Path) -> list[SourceSpec]:
    """Create private, WAL-consistent snapshots without any source writes."""
    directory = Path(directory)
    if directory.is_symlink():
        raise MigrationError("UNSAFE_PATH", "Snapshot directory cannot be a symlink")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    snapshots = []
    for index, spec in enumerate(source_specs):
        destination = directory / f"source-{index}-{uuid.uuid4().hex}.sqlite3"
        descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        source, target = _readonly(Path(spec.path)), sqlite3.connect(destination)
        try:
            source.backup(target)
            if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise MigrationError("CORRUPT_SOURCE", "Source snapshot failed SQLite consistency check")
        finally:
            target.close()
            source.close()
        snapshots.append(SourceSpec(destination, spec.project, spec.format, spec.namespace,
                                    spec.project_id, spec.attachments_root))
    return snapshots


def _selected_rows(connection: sqlite3.Connection, table: str, columns: set[str],
                   source_format: str, project_id: int | None) -> list[sqlite3.Row]:
    selection, arguments = "", ()
    if source_format == "conversation-sqlite-v1":
        if "project_id" in columns:
            selection, arguments = " WHERE project_id=?", (project_id,)
        elif table == "projects":
            selection, arguments = " WHERE id=?", (project_id,)
        elif table in {"message_recipients", "message_delivery_signal_receipts"}:
            selection = " WHERE message_id IN (SELECT id FROM messages WHERE project_id=?)"
            arguments = (project_id,)
        elif "agent_id" in columns:
            selection = " WHERE agent_id IN (SELECT id FROM agents WHERE project_id=?)"
            arguments = (project_id,)
        elif table == "file_reservation_releases":
            selection = " WHERE reservation_id IN (SELECT id FROM file_reservations WHERE project_id=?)"
            arguments = (project_id,)
        elif table == "agent_links":
            selection, arguments = " WHERE a_project_id=? OR b_project_id=?", (project_id, project_id)
    # Identifiers originate in sqlite_master and are SQLite-quoted; all filter values are bound.
    return connection.execute(f"SELECT * FROM {_quoted(table)}{selection}", arguments).fetchall()


def inspect_sources(source_specs: list[SourceSpec] | tuple[SourceSpec, ...]) -> SourceManifest:
    if not source_specs:
        raise MigrationError("INVALID_ARGUMENT", "At least one source is required")
    all_records, sources, seen = [], [], set()
    for spec in source_specs:
        if not isinstance(spec.project, str) or not spec.project:
            raise MigrationError("INVALID_ARGUMENT", "Each source needs its exact project identity")
        connection = _readonly(Path(spec.path))
        try:
            connection.execute("BEGIN")
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise MigrationError("CORRUPT_SOURCE", "SQLite source consistency check failed")
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            schema = {table: [dict(row) for row in connection.execute(
                f"PRAGMA table_info({_quoted(table)})")] for table in sorted(tables)}
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            matches = [name for name, (required_version, required) in _FORMATS.items()
                       if version == required_version and all(table in schema and columns <= {
                           row["name"] for row in schema[table]} for table, columns in required.items())]
            source_format = spec.format or (matches[0] if len(matches) == 1 else None)
            if source_format not in matches:
                raise MigrationError("UNSUPPORTED_SOURCE_SCHEMA", "Source schema is not a supported versioned format",
                                        user_version=version, schema_sha256=_sha(schema))
            project_id = spec.project_id
            if source_format == "conversation-sqlite-v1":
                projects = connection.execute("SELECT id,human_key FROM projects WHERE human_key=?", (spec.project,)).fetchall()
                if len(projects) != 1 or (project_id is not None and project_id != projects[0]["id"]):
                    raise MigrationError("WRONG_SOURCE_PROJECT", "Conversation source project selection is not exact")
                project_id = projects[0]["id"]
            namespace = spec.namespace or source_format
            source_records = []
            for table in sorted(tables):
                infos = schema[table]
                primary = [row["name"] for row in sorted(infos, key=lambda row: row["pk"]) if row["pk"]]
                for row in _selected_rows(connection, table, {info["name"] for info in infos}, source_format, project_id):
                    original = dict(row)
                    if any(isinstance(value, bytes) for value in original.values()):
                        original = {key: {"encoding": "hex", "value": value.hex()} if isinstance(value, bytes)
                                    else value for key, value in original.items()}
                    source_id = str(original[primary[0]]) if len(primary) == 1 else canonical_json(
                        [original[name] for name in primary]) if primary else _sha(original)
                    reference = (namespace, spec.project, table, source_id)
                    if reference in seen:
                        raise MigrationError("DUPLICATE_SOURCE_ID", "Two source records share an import identity")
                    seen.add(reference)
                    source_records.append(SourceRecord(namespace, spec.project, table, source_id,
                                                       _sha(original), _clean(original)))
            source_records.sort(key=lambda row: (row.kind, row.source_id))
            source_summary = {"format": source_format, "namespace": namespace, "project": spec.project,
                              "project_id": project_id, "schema_sha256": _sha(schema), "user_version": version,
                              "records": len(source_records), "logical_sha256": _sha([
                                  {"reference": row.reference, "hash": row.source_sha256} for row in source_records]),
                              "attachments_root": str(spec.attachments_root) if spec.attachments_root else None}
            sources.append(source_summary)
            all_records.extend(source_records)
        finally:
            connection.close()
    return SourceManifest(tuple(sources), tuple(all_records), _manifest_hash(sources, all_records))


def _manifest_hash(sources: Any, records: Any) -> str:
    return _sha({"sources": sources, "records": [{"reference": row.reference,
        "source_sha256": row.source_sha256, "content_sha256": _sha(row.data)} for row in records]})


def manifest_document(manifest: SourceManifest) -> dict[str, Any]:
    return {"version": 1, "sha256": manifest.sha256, "sources": list(manifest.sources),
            "records": [{**row.reference, "source_sha256": row.source_sha256,
                         "data": row.data} for row in manifest.records]}


def save_manifest(manifest: SourceManifest, path: Path) -> Path:
    """Retain full private source facts; normal command output uses summary()."""
    path = Path(path)
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise MigrationError("UNSAFE_PATH", "Manifest path cannot contain symlinks")
    encoded = canonical_json(manifest_document(manifest)).encode("utf-8")
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def load_manifest(path: Path) -> SourceManifest:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(document, dict) or set(document) != {"version", "sha256", "sources", "records"} or document["version"] != 1:
            raise ValueError("Unsupported manifest document")
        sources = tuple(document["sources"])
        records = tuple(SourceRecord(row["namespace"], row["project"], row["kind"], row["id"],
                                     row["source_sha256"], row["data"]) for row in document["records"])
        expected = _manifest_hash(sources, records)
        if expected != document["sha256"]:
            raise ValueError("Manifest content hash differs")
        if len({canonical_json(row.reference) for row in records}) != len(records):
            raise ValueError("Duplicate manifest source identity")
        for record in records:
            if not isinstance(record.data, dict) or _clean(record.data) != record.data:
                raise ValueError("Manifest contains unsanitized source data")
            if not isinstance(record.source_sha256, str) or len(record.source_sha256) != 64:
                raise ValueError("Invalid source hash")
        return SourceManifest(sources, records, expected)
    except (ValueError, TypeError, KeyError) as error:
        raise MigrationError("INVALID_MANIFEST", "Import manifest is damaged or unsupported") from error


def load_source_specs(path: Path) -> list[SourceSpec]:
    """Read an explicit operator selection, without guessing source paths."""
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(document, dict) or set(document) != {"source_specs"} or not document["source_specs"]:
            raise ValueError("An explicit source_specs list is required")
        result = []
        for row in document["source_specs"]:
            if not isinstance(row, dict) or set(row) - {"path", "project", "format", "namespace", "project_id", "attachments_root"}:
                raise ValueError("Unknown source selection fields")
            result.append(SourceSpec(Path(row["path"]), row["project"], row.get("format"), row.get("namespace"),
                row.get("project_id"), Path(row["attachments_root"]) if row.get("attachments_root") else None))
        return result
    except (ValueError, TypeError, KeyError) as error:
        raise MigrationError("INVALID_ARGUMENT", "Source selection document is invalid") from error


def _paths(value: Any) -> list[str]:
    result = []
    for path in value or []:
        if not isinstance(path, str) or "\0" in path or "\\" in path:
            raise MigrationError("INVALID_SOURCE_PATH", "Invalid source scope path")
        candidate = PurePosixPath(path)
        if candidate.is_absolute() or ".." in candidate.parts or path == "":
            raise MigrationError("INVALID_SOURCE_PATH", "Source scope must be repository-relative")
        result.append(str(candidate))
    return sorted(set(result))


def _body_context(body: str) -> dict[str, Any]:
    try:
        header = json.loads(body.split("\n", 1)[0])
    except ValueError:
        return {}
    return header if isinstance(header, dict) and isinstance(header.get("paths"), list) else {}


def prepare_import(manifest: SourceManifest, destination_workspace: Any) -> PreparedImport:
    """Normalize source records without opening the destination or launching work."""
    workspace_id = (destination_workspace if isinstance(destination_workspace, str)
                    else getattr(destination_workspace, "id", None) or getattr(destination_workspace, "workspace_id", None))
    if not isinstance(workspace_id, str) or not workspace_id:
        raise MigrationError("INVALID_ARGUMENT", "An exact destination workspace ID is required")
    if len({source["project"] for source in manifest.sources}) != 1:
        raise MigrationError("WRONG_SOURCE_PROJECT", "A workspace import cannot combine different source projects")
    if len({source["format"] for source in manifest.sources}) != len(manifest.sources):
        raise MigrationError("DUPLICATE_SOURCE_FORMAT", "Supply one frozen source for each supported format")
    source_formats = {source["namespace"]: source["format"] for source in manifest.sources}
    rows = list(manifest.records)
    native = {row.source_id: row for row in rows if row.kind == "sessions"
              and source_formats[row.namespace] == "native-sqlite-v11"}
    mail_actors = {(row.namespace, str(row.data["id"])): row for row in rows
                   if row.kind == "agents" and source_formats[row.namespace] == "conversation-sqlite-v1"}
    actors = {key: _id(row.reference, "actor") for key, row in native.items()}
    historical = {key: _id(row.reference, "actor") for key, row in mail_actors.items()}
    issues: list[dict[str, Any]] = []
    prepared: list[PreparedRecord] = []
    native_by_table = {(row.namespace, row.kind, row.source_id): row for row in rows}
    actor_generations: dict[str, set[str]] = {key: {str(_json(row.data["metadata"], default={}).get(
        "lifecycle_generation", "initial"))} for key, row in native.items()}
    receipt_versions: dict[str, int] = {}
    revision_counts: dict[tuple[Any, Any], int] = {}
    for receipt in sorted((row for row in rows if row.kind == "readiness_receipts"),
                          key=lambda row: (_us(row.data["created_at"]), int(row.source_id))):
        key = (receipt.data["producer_key"], receipt.data["artifact_key"])
        revision_counts[key] = revision_counts.get(key, 0) + 1
        receipt_versions[receipt.source_id] = revision_counts[key]

    def issue(row: SourceRecord, code: str, required: bool, **detail: Any) -> None:
        issues.append({"id": _id(row.reference, code, detail), "source_ref": row.reference,
                       "code": code, "required": required, "detail": detail})

    def actor(key: Any) -> str:
        if key not in actors:
            raise MigrationError("MISSING_ACTOR", "A native relationship has no source actor", source_id=key)
        return actors[key]

    def generation(key: str, legacy: Any = None) -> str:
        owner = native.get(key)
        if owner is None:
            raise MigrationError("MISSING_ACTOR", "A generation has no source actor")
        if legacy is None:
            legacy = _json(owner.data["metadata"], default={}).get("lifecycle_generation", "initial")
        actor_generations[key].add(str(legacy))
        return _id(owner.reference, "assignment", str(legacy))

    def execution(key: str) -> str:
        owner = native[key]
        metadata = _json(owner.data["metadata"], default={})
        return _id(owner.reference, "execution", metadata.get("lifecycle_generation", "initial"),
                   metadata.get("lifecycle_started_at"), metadata.get("process_identity"))

    def source_id(row: SourceRecord, table: str, identifier: Any) -> str:
        counterpart = native_by_table.get((row.namespace, table, str(identifier)))
        if counterpart is None:
            raise MigrationError("MISSING_LINK", "Required native source counterpart is absent", table=table, source_id=identifier)
        return _id(counterpart.reference, table)

    def dest(table: str, identity: dict[str, Any], **values: Any) -> DestinationRow:
        return DestinationRow(table, identity, {**identity, **values})

    # Exact receipt IDs, never mailbox labels, establish native recipient ownership.
    delivered_owners: dict[int, set[str]] = {}
    presentations = {(row.data["message_id"], row.data["session_key"]): row.data["first_presented_at"]
                     for row in rows if row.kind == "message_presentations"}
    for row in rows:
        if row.kind == "delivery_outbox" and row.data.get("state") == "delivered":
            for message_id in _json(row.data.get("result"), default={}).get("message_ids", []):
                if type(message_id) is int and row.data.get("recipient_key") in actors:
                    delivered_owners.setdefault(message_id, set()).add(row.data["recipient_key"])

    # Precollect captured generations so actor inserts include all referenced assignments.
    for row in rows:
        data = row.data
        for key_field, generation_field in (("recipient_key", "recipient_generation"),
                ("parent_key", "parent_generation"), ("producer_key", "producer_generation"),
                ("consumer_key", "consumer_generation"), ("session_key", "generation"),
                ("actor_key", "actor_generation")):
            if data.get(key_field) in native and data.get(generation_field) is not None:
                generation(data[key_field], data[generation_field])
        if row.kind == "jobs" and data.get("session_key") in native:
            target = _json(data.get("payload"), default={}).get("resume_context", {})
            if target.get("generation") is not None:
                generation(data["session_key"], target["generation"])

    actor_rows: dict[str, list[DestinationRow]] = {}
    for key, row in native.items():
        data, metadata = row.data, _json(row.data["metadata"], default={})
        parts = key.split(":", 2)
        if len(parts) != 3 or not parts[0] or not parts[1]:
            issue(row, "INVALID_NATIVE_IDENTITY", True)
            continue
        task_generation = generation(key)
        parent_id = actors.get(metadata.get("parent_session_key"))
        values = [dest("actors", {"id": actors[key]}, harness=parts[0], native_session_id=parts[1],
                       child_id=parts[2], parent_id=parent_id, label=data["agent_name"],
                       current_task_generation=task_generation, current_execution_generation=execution(key),
                       reported_state=data["state"], archived=1,
                       created_us=_us(metadata.get("lifecycle_started_at", data.get("last_seen"))),
                       resume_enabled=int(metadata.get("resume_enabled") is True),
                       resume_inhibited=int(metadata.get("resume_inhibited") is True),
                       process_identity_json=canonical_json(metadata.get("process_identity")),
                       checkpoint_json=canonical_json(_json(data["checkpoint"], default={})),
                       metadata_json=canonical_json({"source": row.reference, "legacy": metadata})),
                  dest("executions", {"generation": execution(key)}, actor_id=actors[key],
                       native_run_id=canonical_json({"source": row.reference, "legacy_generation": metadata.get("lifecycle_generation")}),
                       process_identity_json=canonical_json(metadata.get("process_identity")), state="unknown",
                       created_us=_us(metadata.get("lifecycle_started_at", data.get("last_seen"))), ended_us=None)]
        values.extend(dest("assignments", {"generation": generation(key, tag)}, actor_id=actors[key],
                           task=data["task"], created_us=_us(data.get("last_seen"))) for tag in sorted(actor_generations[key]))
        values.append(dest("native_aliases", {"harness": parts[0], "native_id": parts[1], "child_id": parts[2]},
                           actor_id=actors[key], provenance_json=canonical_json(row.reference)))
        native_id = metadata.get("native_session_id")
        if native_id and native_id != parts[1]:
            values.append(dest("native_aliases", {"harness": parts[0], "native_id": native_id, "child_id": parts[2]},
                               actor_id=actors[key], provenance_json=canonical_json(row.reference)))
        if parent_id and metadata.get("parent_relationship_authorized") is True:
            parent_key = metadata["parent_session_key"]
            parent_data = native[parent_key].data
            parent_metadata = _json(parent_data["metadata"], default={})
            binding = metadata.get("parent_relationship_binding")
            expected = {"parent_task": parent_data["task"], "child_task": data["task"],
                        "parent_generation": parent_metadata.get("lifecycle_generation"),
                        "child_generation": metadata.get("lifecycle_generation")}
            verified = (metadata.get("parent_relationship_verified") is True
                        and metadata.get("parent_relationship_source") == "native_child_start")
            if verified or (metadata.get("parent_relationship_source") == "parent_declaration" and binding == expected):
                values.append(dest("parent_routes", {"child_id": actors[key]}, parent_id=parent_id,
                    child_task_generation=task_generation, parent_task_generation=generation(parent_key),
                    source=metadata["parent_relationship_source"], created_us=_us(data.get("last_seen"))))
            else:
                issue(row, "UNVERIFIED_PARENT_ROUTE", False)
        actor_rows[key] = values

        history = [*metadata.get("activity_history", []), *metadata.get("shared_activity_reports", [])]
        current = metadata.get("activity")
        if current:
            history.append(current)
        history.extend(metadata.get("shared_activity_current", []))
        seen_history = set()
        current_digest = _sha(current) if current else None
        for activity in history:
            if not isinstance(activity, dict):
                issue(row, "INVALID_SOURCE_ACTIVITY", True)
                continue
            digest = _sha(activity)
            if digest in seen_history:
                continue
            seen_history.add(digest)
            activity_id = _id(row.reference, "activity", digest)
            mode = "shared_report" if "scope" in activity else "canonical"
            values.append(dest("activities", {"id": activity_id}, actor_id=actors[key],
                task_generation=task_generation, task=activity.get("task", data["task"]),
                state=activity.get("state", data["state"]), note=activity.get("note", ""),
                evidence_json=canonical_json({"reference": activity.get("evidence", ""), "source": row.reference}),
                sequence="$event", created_us=_us(activity.get("at", data.get("last_seen"))), mode=mode))
            try:
                values.extend(dest("activity_paths", {"activity_id": activity_id, "path": path})
                              for path in _paths(activity.get("paths", [])))
            except MigrationError as error:
                issue(row, error.code, True)
            if digest == current_digest:
                values.append(dest("current_activity", {"actor_id": actors[key]}, activity_id=activity_id))
            if activity in metadata.get("shared_activity_current", []):
                values.append(dest("shared_activity_current", {"actor_id": actors[key],
                    "task": activity.get("task", data["task"]), "paths_json": canonical_json(_paths(activity.get("paths", [])))},
                    activity_id=activity_id))

    # Actors and assignments precede all relationship inserts, across batch boundaries.
    for row in rows:
        if row.kind == "sessions" and row.source_id in actor_rows:
            prepared.append(PreparedRecord(row, tuple(actor_rows[row.source_id])))
        elif (row.namespace, row.source_id) in historical and row.kind == "agents":
            identifier = historical[(row.namespace, row.source_id)]
            tag = _id(row.reference, "assignment")
            prepared.append(PreparedRecord(row, (
                dest("actors", {"id": identifier}, harness="historical", native_session_id=_id(row.reference), child_id="",
                     parent_id=None, label=row.data["name"], current_task_generation=tag,
                     current_execution_generation=None, reported_state="completed", archived=1,
                     created_us=_conversation_us(row.data.get("inception_ts"))),
                dest("assignments", {"generation": tag}, actor_id=identifier,
                     task=row.data.get("task_description", ""), created_us=_conversation_us(row.data.get("inception_ts"))),
            )))
        elif row.kind in {"sessions", "agents"}:
            prepared.append(PreparedRecord(row))

    priority = {"messages": 1, "message_recipients": 2, "actionable_requests": 3,
                "readiness_receipts": 4, "dependencies": 5, "dependency_outbox": 6,
                "request_deferrals": 7, "request_followers": 7, "request_answer_proposals": 7,
                "delivery_outbox": 8}
    remaining = [row for row in rows if row.kind not in {"sessions", "agents"}]
    remaining.sort(key=lambda row: (priority.get(row.kind, 9),
        str(row.data.get("created_ts", row.data.get("created_at", ""))), row.kind,
        int(row.source_id) if row.source_id.isdecimal() else 0, row.source_id))
    mail_messages = {(row.namespace, str(row.data["id"])): row for row in remaining
                     if row.kind == "messages" and source_formats[row.namespace] == "conversation-sqlite-v1"}
    for row in remaining:
        data, targets = row.data, []
        identifier = _id(row.reference, row.kind)
        try:
            if row.kind == "messages" and source_formats[row.namespace] == "conversation-sqlite-v1":
                sender = historical.get((row.namespace, str(data["sender_id"])))
                if sender is None:
                    raise MigrationError("MISSING_ACTOR", "Historical message sender is absent")
                body = data["body_md"].encode("utf-8")
                context = _body_context(data["body_md"])
                targets.append(dest("messages", {"id": identifier}, sender_id=sender,
                    sender_task_generation=_id(mail_actors[(row.namespace, str(data["sender_id"]))].reference, "assignment"),
                    thread=data.get("thread_id") or "", kind=context.get("kind", "HISTORY"), subject=data.get("subject", ""),
                    body_utf8=body, body_sha256=hashlib.sha256(body).hexdigest(), body_bytes=len(body),
                    context_json=canonical_json({"import": row.reference, "source_facts": {
                        "importance": data.get("importance"), "topic": data.get("topic"),
                        "ack_required": data.get("ack_required")}, "declared": context}),
                    sequence="$event", created_us=_conversation_us(data["created_ts"])))
                targets.extend(dest("message_paths", {"message_id": identifier, "path": path})
                               for path in _paths(context.get("paths", [])))
                for number, attachment in enumerate(_json(data.get("attachments"), default=[])):
                    source = next(item for item in manifest.sources if item["namespace"] == row.namespace)
                    targets.append(_prepare_attachment(row, identifier, number, attachment, source.get("attachments_root")))
            elif row.kind == "message_recipients":
                message = mail_messages.get((row.namespace, str(data["message_id"])))
                recipient = historical.get((row.namespace, str(data["agent_id"])))
                if message is None or recipient is None:
                    raise MigrationError("MISSING_LINK", "Message recipient has an absent message or actor")
                known = delivered_owners.get(data["message_id"], set())
                recipient_key = next(iter(known)) if len(known) == 1 else None
                if recipient_key:
                    recipient, tag = actor(recipient_key), generation(recipient_key)
                else:
                    tag = _id(mail_actors[(row.namespace, str(data["agent_id"]))].reference, "assignment")
                    if len(known) > 1:
                        issue(row, "HISTORICAL_RECIPIENT_AMBIGUITY", False, candidates=sorted(known))
                presented = presentations.get((data["message_id"], recipient_key))
                targets.append(dest("recipients", {"message_id": _id(message.reference, "messages"), "actor_id": recipient},
                    recipient_task_generation=tag, role=data.get("kind", "to"),
                    presented_us=_us(presented) if presented is not None else None, handled_us=None,
                    requested_ack_us=_conversation_us(message.data["created_ts"]) if message.data.get("ack_required") else None,
                    acknowledged_us=_conversation_us(data["ack_ts"]) if data.get("ack_ts") else None))
            elif row.kind == "actionable_requests":
                targets.append(dest("decisions", {"id": identifier}, sender_id=actor(data["sender_key"]),
                    recipient_id=actor(data["recipient_key"]), recipient_task_generation=generation(data["recipient_key"], data.get("recipient_generation")),
                    parent_id=actor(data["parent_key"]) if data.get("parent_key") else None,
                    parent_task_generation=generation(data["parent_key"], data.get("parent_generation")) if data.get("parent_key") else None,
                    task=data["task"], subject=data["subject"], body=data["body"], deadline_us=_us(data["deadline"]),
                    state=data["state"], routing_state=data["routing_state"] if data["routing_state"] in {"waiting", "escalated"} else "diagnostic",
                    response=data.get("response"), resolved_by=actor(data["resolved_by"]) if data.get("resolved_by") else None,
                    resolved_us=_us(data["resolved_at"]) if data.get("resolved_at") is not None else None, version=1,
                    sequence="$event"))
                targets.extend(dest("decision_paths", {"decision_id": identifier, "path": path})
                               for path in _paths(_body_context(data["body"]).get("paths", [])))
            elif row.kind == "request_deferrals":
                targets.append(dest("deferrals", {"decision_id": source_id(row, "actionable_requests", data["request_id"])},
                    actor_id=actor(data["actor_key"]), until_us=_us(data["until"]), note=data["note"], version=1))
            elif row.kind == "request_followers":
                targets.append(dest("followers", {"decision_id": source_id(row, "actionable_requests", data["request_id"]),
                    "actor_id": actor(data["session_key"])}, task_generation=generation(data["session_key"], data["generation"]),
                    active=1, seen_version=0, handled_version=0))
            elif row.kind == "request_answer_proposals":
                targets.append(dest("answer_proposals", {"id": identifier},
                    decision_id=source_id(row, "actionable_requests", data["request_id"]), actor_id=actor(data["actor_key"]),
                    actor_generation=generation(data["actor_key"], data["actor_generation"]), state=data["state"],
                    response=data["response"], proposed_state=data["proposed_state"], created_us=_us(data["created_at"]),
                    reconciled_by=actor(data["reconciled_by"]) if data.get("reconciled_by") else None,
                    reconciled_us=_us(data["reconciled_at"]) if data.get("reconciled_at") is not None else None))
            elif row.kind == "readiness_receipts":
                observed = _json(data["observed"])
                targets.append(dest("receipts", {"id": identifier}, producer_id=actor(data["producer_key"]),
                    producer_task_generation=generation(data["producer_key"], data.get("producer_generation")), artifact=data["artifact_key"],
                    status=data["status"], hashes_json=canonical_json(observed), evidence_json=canonical_json({"reference": data["evidence"]}),
                    version=receipt_versions[row.source_id], sequence="$event", created_us=_us(data["created_at"])))
                targets.extend(dest("receipt_paths", {"receipt_id": identifier, "path": path}) for path in _paths(observed))
            elif row.kind == "dependencies":
                targets.append(dest("subscriptions", {"id": identifier}, consumer_id=actor(data["consumer_key"]),
                    consumer_task_generation=generation(data["consumer_key"], data.get("consumer_generation")),
                    producer_id=actor(data["producer_key"]), producer_task_generation=generation(data["producer_key"], data.get("producer_generation")),
                    artifact=data["artifact_key"], paths_json=canonical_json(_paths(_json(data["paths"]))),
                    next_action=data["next_action"], task=data["task"], state=data["state"], version=1))
            elif row.kind == "dependency_outbox":
                targets.append(dest("dependency_updates", {"id": identifier}, subscription_id=source_id(row, "dependencies", data["dependency_id"]),
                    receipt_id=source_id(row, "readiness_receipts", data["receipt_id"]),
                    presented_us=_us(data["fetched_at"]) if data.get("fetched_at") is not None else None,
                    accepted_us=_us(data["accepted_at"]) if data.get("accepted_at") is not None else None, sequence="$event"))
            elif row.kind in {"file_changes", "write_operations", "file_intents", "snapshots", "operation_paths"}:
                owner = data.get("session_key")
                if owner is None and data.get("operation_id"):
                    operation_row = native_by_table.get((row.namespace, "write_operations", str(data["operation_id"])))
                    owner = operation_row.data.get("session_key") if operation_row else None
                targets.append(dest("retained_changes", {"id": identifier}, actor_id=actor(owner) if owner else None,
                    source_json=canonical_json({"reference": row.reference, "facts": data}), sequence="$event",
                    created_us=_us(data.get("at", data.get("created_at", data.get("updated_at"))))))
                if row.kind == "file_intents":
                    targets.append(dest("intents", {"id": _id(row.reference, "intent")}, actor_id=actor(owner),
                        task_generation=generation(owner), path=_paths([data["path"]])[0], purpose=data["why"],
                        invariants_json=canonical_json(_json(data["preserve"], default=[])), state="completed",
                        updated_us=_us(data["updated_at"])))
                if row.kind == "write_operations" and data.get("state") in {"running", "active", "uncertain", "critical"}:
                    issue(row, "UNRECONCILED_SOURCE_EFFECT", True, state=data["state"])
            elif row.kind == "jobs":
                targets.extend(_prepare_job(row, actor, generation, execution, issue, dest))
            elif row.kind == "commits":
                targets.extend(_prepare_commit(row, native, actor, generation, issue, dest))
            elif row.kind == "delivery_outbox":
                if data["state"] not in {"delivered", "cancelled", "failed"}:
                    issue(row, "AMBIGUOUS_SOURCE_SEND", True, state=data["state"])
                elif data["state"] == "failed":
                    request = native_by_table.get((row.namespace, "actionable_requests", str(data.get("request_id"))))
                    # Failed diagnostic notices transfer no decision or grant authority.
                    # The retained open decision supplies the canonical pending surface.
                    diagnostic = data["kind"] == "diagnostic" and not data.get("receipt_id")
                    issue(row, "FAILED_SOURCE_SEND", bool(request and request.data["state"] == "open" and not diagnostic),
                          state="failed", kind=data["kind"], diagnostic_only=diagnostic,
                          request_state=request.data["state"] if request else None,
                          last_error=_json(data.get("last_error"), default={}))
                if data["state"] == "delivered":
                    for message_id in _json(data.get("result"), default={}).get("message_ids", []):
                        candidates = [message for (_, sid), message in mail_messages.items() if sid == str(message_id)]
                        if len(candidates) != 1:
                            raise MigrationError("MISSING_LINK", "Delivered notification has no unique source message")
                        message = _id(candidates[0].reference, "messages")
                        if data.get("request_id"):
                            targets.append(dest("decision_notifications", {"decision_id": source_id(row, "actionable_requests", data["request_id"]),
                                "message_id": message}, kind=data["kind"]))
                        if data.get("receipt_id"):
                            targets.append(dest("readiness_notifications", {"receipt_id": source_id(row, "readiness_receipts", data["receipt_id"]),
                                "message_id": message}))
        except (MigrationError, KeyError, TypeError, ValueError, OSError) as error:
            issue(row, getattr(error, "code", "INVALID_SOURCE_RECORD"), True,
                  error_type=type(error).__name__, message=str(error))
            targets = []
        prepared.append(PreparedRecord(row, tuple(targets)))
    prepared = _quarantine_missing_destinations(prepared, issue)
    return PreparedImport(manifest, workspace_id, tuple(prepared), tuple(issues))


def _quarantine_missing_destinations(records: list[PreparedRecord], issue: Any) -> list[PreparedRecord]:
    """Retain invalid facts and suppress their dependent writes before applying."""
    relations = {
        "actor_id": ("actors", "id"), "sender_id": ("actors", "id"),
        "recipient_id": ("actors", "id"), "parent_id": ("actors", "id"),
        "producer_id": ("actors", "id"), "consumer_id": ("actors", "id"),
        "resolved_by": ("actors", "id"), "reconciled_by": ("actors", "id"),
        "task_generation": ("assignments", "generation"), "sender_task_generation": ("assignments", "generation"),
        "recipient_task_generation": ("assignments", "generation"), "parent_task_generation": ("assignments", "generation"),
        "producer_task_generation": ("assignments", "generation"), "consumer_task_generation": ("assignments", "generation"),
        "actor_generation": ("assignments", "generation"),
        "message_id": ("messages", "id"), "activity_id": ("activities", "id"),
        "decision_id": ("decisions", "id"), "receipt_id": ("receipts", "id"),
        "subscription_id": ("subscriptions", "id"), "admission_id": ("commit_admissions", "id"),
    }
    while True:
        available: dict[tuple[str, str], set[Any]] = {}
        for record in records:
            for target in record.destinations:
                for key, value in target.identity.items():
                    available.setdefault((target.table, key), set()).add(value)
        changed, next_records = False, []
        for record in records:
            missing = []
            for target in record.destinations:
                for key, relation in relations.items():
                    value = target.values.get(key)
                    if value is not None and value not in available.get(relation, set()):
                        missing.append({"table": target.table, "field": key})
                if target.table == "parent_routes" and target.values["child_id"] not in available.get(("actors", "id"), set()):
                    missing.append({"table": target.table, "field": "child_id"})
            if missing:
                issue(record.source, "MISSING_NORMALIZED_LINK", True, relationships=missing)
                next_records.append(PreparedRecord(record.source))
                changed = True
            else:
                next_records.append(record)
        records = next_records
        if not changed:
            return records


def _prepare_attachment(row: SourceRecord, message_id: str, number: int,
                        reference: Any, root_value: str | None) -> DestinationRow:
    if not isinstance(reference, dict) or not isinstance(reference.get("path"), str) or root_value is None:
        raise MigrationError("UNVERIFIED_ATTACHMENT", "Attachment needs an exact retained root and path")
    root = Path(root_value).resolve(strict=True)
    raw = Path(reference["path"])
    path = (root / raw).resolve(strict=True) if not raw.is_absolute() else raw.resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file():
        raise MigrationError("UNSAFE_ATTACHMENT", "Attachment must remain inside the retained source root")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    expected = reference.get("sha256") or reference.get("content_sha256")
    if expected is not None and expected != digest:
        raise MigrationError("ATTACHMENT_HASH_MISMATCH", "Attachment differs from its source hash")
    size = path.stat().st_size
    if reference.get("bytes") is not None and reference["bytes"] != size:
        raise MigrationError("ATTACHMENT_LENGTH_MISMATCH", "Attachment differs from its source length")
    identifier = _id(row.reference, "attachment", number)
    return DestinationRow("attachments", {"id": identifier}, {"id": identifier, "message_id": message_id,
        "reference_json": canonical_json({"path": str(path), "source": reference}),
        "content_sha256": digest, "bytes": size})


def _prepare_job(row: SourceRecord, actor: Any, generation: Any, execution: Any,
                 issue: Any, dest: Any) -> list[DestinationRow]:
    data, payload = row.data, _json(row.data["payload"])
    state = {"completed": "succeeded", "cancelled": "cancelled", "failed": "failed",
             "pending": "pending", "running": "uncertain", "uncertain": "uncertain", "cancelling": "uncertain"}.get(data["state"])
    if state is None:
        raise MigrationError("UNKNOWN_JOB_STATE", "Source job state is unsupported")
    identifier = _id(row.reference, "jobs")
    context = payload.get("resume_context", {})
    if state == "uncertain" or (data["kind"] == "resume" and state == "pending"):
        issue(row, "UNRECONCILED_SOURCE_JOB", True, state=data["state"], target=context)
        state = "uncertain"
    target = {"source": row.reference, "legacy_target": context,
              "legacy_claim_token": data.get("claim_token"), "legacy_worker": data.get("worker"),
              "legacy_lease_until_us": _us(data["lease_until"]) if data.get("lease_until") is not None else None,
              "correlation": "legacy-task-run-generation-not-independent"}
    # Translate the retained legacy format once at its producer boundary. The
    # exact original payload remains in import_records.source_json; native
    # workers consume only their current note/subject/timeout contract.
    try:
        timeout_seconds = min(86400, max(1, int(payload.get("timeout", 3600))))
    except (ValueError, TypeError, OverflowError) as error:
        raise MigrationError("INVALID_SOURCE_JOB", "Legacy job timeout is invalid") from error
    native_payload = {"timeout_seconds": timeout_seconds}
    if data["kind"] == "reminder":
        native_payload["note"] = payload.get("body", payload.get("text", payload.get("note", "")))
        native_payload["subject"] = payload.get("subject", "Scheduled reminder")
    else:
        native_payload["note"] = payload.get("prompt", payload.get("note", ""))
    due_us = _us(data["due_at"])
    deadline_us = (_us(payload["delivery_deadline"])
                   if payload.get("delivery_deadline") is not None else due_us + 86400_000000)
    return [dest("jobs", {"id": identifier}, actor_id=actor(data["session_key"]),
        task_generation=generation(data["session_key"], context.get("generation")),
        execution_generation=execution(data["session_key"]), kind=data["kind"], due_us=due_us,
        target_json=canonical_json(target), payload_json=canonical_json(native_payload), state=state,
        operation_id=None, attempts=data["attempts"], cancel_requested=int(bool(payload.get("cancel_requested"))),
        last_result_json=canonical_json({"legacy_error": data.get("error")}), created_us=_us(data["due_at"]),
        updated_us=_us(data["due_at"]), version=1,
        delivery_deadline_us=deadline_us)]


def _prepare_commit(row: SourceRecord, native: dict[str, SourceRecord], actor: Any,
                    generation: Any, issue: Any, dest: Any) -> list[DestinationRow]:
    data = row.data
    owner = native[data["session_key"]]
    metadata = _json(owner.data["metadata"])
    identifier = _id(row.reference, "commits")
    live = data.get("finished_at") is None
    grant_id = _id(row.reference, "grant") if live and metadata.get("commit_until") else None
    if live:
        issue(row, "UNRECONCILED_SOURCE_GRANT", True, original_grant_id=data["id"],
              owner_identity=metadata.get("process_identity"), last_commit_run=metadata.get("last_commit_run"))
    targets = [dest("commit_admissions", {"id": identifier}, actor_id=actor(data["session_key"]),
        task_generation=generation(data["session_key"]), mode=data["mode"], state="uncertain" if live else "released",
        created_us=_us(data["created_at"]), expires_us=_us(metadata.get("commit_request_until", data["created_at"])), grant_id=grant_id)]
    targets.extend(dest("commit_paths", {"admission_id": identifier, "path": path})
                   for path in _paths(_json(data["paths"], default=[])))
    if grant_id:
        targets.append(dest("commit_grants", {"id": grant_id}, admission_id=identifier,
            owner_identity_json=canonical_json({"source": row.reference, "process_identity": metadata.get("process_identity"),
                "index_at_grant": metadata.get("index_at_grant"), "last_commit_run": metadata.get("last_commit_run")}),
            state="uncertain", created_us=_us(data["created_at"]), released_us=None))
    return targets


def _table_row(connection: sqlite3.Connection, row: DestinationRow) -> dict[str, Any] | None:
    predicate = " AND ".join(f"{_quoted(key)}=?" for key in row.identity)
    # Prepared identifiers are individually SQLite-quoted, while identity values are bound.
    result = connection.execute(f"SELECT * FROM {_quoted(row.table)} WHERE {predicate}",
                                tuple(row.identity.values())).fetchone()
    return dict(result) if result is not None else None


def _destination_snapshot(connection: sqlite3.Connection, row: DestinationRow,
                          *, check_event: bool = True) -> dict[str, Any]:
    actual = _table_row(connection, row)
    if actual is None:
        raise MigrationError("MISSING_DESTINATION", "An imported destination record is absent", table=row.table)
    values = {}
    for key, expected in row.values.items():
        if key not in actual:
            raise MigrationError("DESTINATION_SCHEMA_MISMATCH", "Destination omits an agreed import field", table=row.table, field=key)
        value = actual[key]
        if expected == "$event":
            if check_event and connection.execute("SELECT 1 FROM events WHERE sequence=?", (value,)).fetchone() is None:
                raise MigrationError("MISSING_EVENT", "Imported timeline record lost its event")
            value = "$event"
        values[key] = value
    return {"table": row.table, "identity": row.identity, "values": values}


def _insert_destination(transaction: Any, row: DestinationRow, source: SourceRecord) -> None:
    connection = transaction.connection
    existing = _table_row(connection, row)
    if existing is not None:
        snapshot = _destination_snapshot(connection, row)
        if snapshot["values"] != row.values:
            raise MigrationError("DESTINATION_CONFLICT", "A destination identity already has different content", table=row.table)
        return
    values = dict(row.values)
    if values.get("sequence") == "$event":
        record_id = str(row.identity.get("id") or _id(row.table, row.identity))
        owner = values.get("actor_id") or values.get("sender_id") or values.get("producer_id")
        values["sequence"] = transaction.event("migration", row.table, record_id, owner, {"source": source.reference})
    columns = tuple(values)
    statement = f"INSERT INTO {_quoted(row.table)} ({','.join(_quoted(key) for key in columns)}) VALUES ({','.join('?' for _ in columns)})"
    try:
        connection.execute(statement, tuple(values[key] for key in columns))
    except sqlite3.IntegrityError as error:
        raise MigrationError("DESTINATION_CONSTRAINT", "Normalized source violates destination invariants",
                                table=row.table, source=source.reference, constraint=str(error)) from error
    if row.table == "jobs":
        transaction.event("jobs", "imported", values["id"], values["actor_id"], {"source": source.reference})


def _require_offline(transaction: Any) -> None:
    """Reject an active native authority; root owns service fencing before import."""
    connection = transaction.connection
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "meta" in tables:
        marker = connection.execute("SELECT value_json FROM meta WHERE key='service_state'").fetchone()
        if marker and _json(marker[0]) in {"quiescent", "importing"}:
            return  # Explicit exclusive operator maintenance; root has fenced clients.
    if "bindings" in tables and connection.execute("SELECT 1 FROM bindings WHERE revoked_us IS NULL LIMIT 1").fetchone():
        raise MigrationError("DESTINATION_ACTIVE", "Import requires a quiescent destination without active native bindings")
    if "idempotency" in tables and connection.execute("SELECT 1 FROM idempotency LIMIT 1").fetchone():
        raise MigrationError("DESTINATION_ACTIVE", "Import cannot overwrite accepted native writes")
    if "operations" in tables and connection.execute("SELECT 1 FROM operations LIMIT 1").fetchone():
        raise MigrationError("DESTINATION_ACTIVE", "Import requires explicit quiescence after native effects")
    for table in ("actors", "messages", "decisions", "activities", "receipts", "jobs", "commit_admissions"):
        if table in tables and connection.execute(f"SELECT 1 FROM {_quoted(table)} LIMIT 1").fetchone():
            raise MigrationError("DESTINATION_ACTIVE", "Nonempty destination requires explicit quiescence", table=table)


def _write_batch(store: Any, prepared: PreparedImport, run_id: str, start: int, stop: int) -> None:
    with store.write(maintenance=True) as transaction:
        _require_offline(transaction)
        connection = transaction.connection
        run = connection.execute("SELECT checkpoint FROM import_runs WHERE id=?", (run_id,)).fetchone()
        if run is None or run["checkpoint"] != start:
            raise MigrationError("STALE_IMPORT_CHECKPOINT", "Another importer advanced the retained checkpoint")
        for record in prepared.records[start:stop]:
            source = record.source
            reference = (source.namespace, source.project, source.kind, source.source_id)
            existing = connection.execute("""SELECT * FROM import_records WHERE
                source_namespace=? AND source_project=? AND source_kind=? AND source_id=?""", reference).fetchone()
            if existing is not None:
                if (existing["source_sha256"] != source.source_sha256
                        or existing["canonical_sha256"] != record.canonical_sha256):
                    raise MigrationError("IMPORT_CONFLICT", "A stable source identity changed its imported content", source=source.reference)
                for destination in record.destinations:
                    if _destination_snapshot(connection, destination)["values"] != destination.values:
                        raise MigrationError("DESTINATION_CONFLICT", "Repeated import would change a mapped record", source=source.reference)
                continue
            for destination in record.destinations:
                _insert_destination(transaction, destination, source)
            destinations_json = canonical_json([{"table": row.table, "identity": row.identity,
                                                  "values": row.values} for row in record.destinations])
            connection.execute("INSERT INTO import_records VALUES (?,?,?,?,?,?,?,?,?,?)", (
                source.destination_id, *reference, storage_codec.encode(canonical_json(source.data)), source.source_sha256,
                record.canonical_sha256, storage_codec.encode(destinations_json), run_id))
            # Every fact has one provenance receipt, even when a retired mechanism has no live destination.
            destination_kind = record.destinations[0].table if record.destinations else "import_records"
            destination_id = str(record.destinations[0].identity.get("id") or
                                 _id(record.destinations[0].identity)) if record.destinations else source.destination_id
            connection.execute("INSERT INTO provenance VALUES (?,?,?,?,?,?,?,?,?)", (
                *reference, destination_kind, destination_id, source.source_sha256, record.canonical_sha256, run_id))
        connection.execute("UPDATE import_runs SET checkpoint=?,state='applying' WHERE id=? AND checkpoint=?",
                           (stop, run_id, start))


def apply_import(store: Any, prepared: PreparedImport, *, run_id: str,
                 batch_limit: int = 500) -> dict[str, Any]:
    if type(batch_limit) is not int or not 1 <= batch_limit <= 5000:
        raise MigrationError("INVALID_ARGUMENT", "Import batch limit must be 1..5000")
    if not isinstance(run_id, str) or not run_id or len(run_id) > 256:
        raise MigrationError("INVALID_ARGUMENT", "A stable import run ID is required")
    if getattr(store, "workspace_id", None) != prepared.workspace_id:
        raise MigrationError("WRONG_WORKSPACE", "Prepared import belongs to another destination workspace")
    with store.write(maintenance=True) as transaction:
        _require_offline(transaction)
        connection = transaction.connection
        # Fence ordinary writers in this same transaction before any import batch.
        # Completion does not activate authority; explicit cutover owns activation.
        connection.execute("UPDATE meta SET value_json=? WHERE key='service_state'", (canonical_json("importing"),))
        existing = connection.execute("SELECT * FROM import_runs WHERE id=?", (run_id,)).fetchone()
        if existing is not None and existing["source_manifest_sha256"] != prepared.manifest.sha256:
            raise MigrationError("IMPORT_CONFLICT", "Import run ID was reused for another source manifest")
        if existing is None:
            connection.execute("INSERT INTO import_runs VALUES (?,?, 'prepared',?,?,NULL,0)", (
                run_id, prepared.manifest.sha256, canonical_json(prepared.manifest.summary()), transaction.now_us))
        checkpoint = existing["checkpoint"] if existing is not None else 0
        for issue in prepared.issues:
            old = connection.execute("SELECT detail_json FROM import_issues WHERE id=?", (issue["id"],)).fetchone()
            if old is None:
                connection.execute("INSERT INTO import_issues VALUES (?,?,?,?,?,'open',?)", (
                    issue["id"], run_id, canonical_json(issue["source_ref"]), issue["code"], int(issue["required"]),
                    canonical_json(issue["detail"])))
    for start in range(checkpoint, len(prepared.records), batch_limit):
        _write_batch(store, prepared, run_id, start, min(start + batch_limit, len(prepared.records)))
    verification = verify_import(store, prepared.manifest)
    required = sum(issue["required"] for issue in prepared.issues)
    complete = verification["valid"] and required == 0
    with store.write(maintenance=True) as transaction:
        transaction.connection.execute("UPDATE import_runs SET state=?,completed_us=? WHERE id=?", (
            "complete" if complete else "blocked", transaction.now_us if complete else None, run_id))
    return {"run_id": run_id, "state": "complete" if complete else "blocked",
            "records": len(prepared.records), "required_issues": required,
            "issues": list(prepared.issues), "verification": verification}


def verify_import(store: Any, manifest: SourceManifest) -> dict[str, Any]:
    prepared = prepare_import(manifest, store.workspace_id)
    failures: list[dict[str, Any]] = []
    checked = 0
    with store.read() as transaction:
        connection = transaction.connection
        for record in prepared.records:
            source = record.source
            reference = (source.namespace, source.project, source.kind, source.source_id)
            facts = connection.execute("""SELECT * FROM import_records WHERE source_namespace=?
                AND source_project=? AND source_kind=? AND source_id=?""", reference).fetchone()
            provenance = connection.execute("""SELECT * FROM provenance WHERE source_namespace=?
                AND source_project=? AND source_kind=? AND source_id=?""", reference).fetchone()
            try:
                if facts is None or provenance is None:
                    raise MigrationError("MISSING_PROVENANCE", "An expected source record has no complete mapping")
                if facts["source_sha256"] != source.source_sha256 or provenance["source_sha256"] != source.source_sha256:
                    raise MigrationError("SOURCE_HASH_MISMATCH", "Imported source hash differs from the retained source")
                archived_destinations = _json(storage_codec.decode(facts["destinations_json"]))
                expected_destinations = [{"table": row.table, "identity": row.identity,
                                          "values": row.values} for row in record.destinations]
                if canonical_json(archived_destinations) != canonical_json(expected_destinations):
                    raise MigrationError("CANONICAL_HASH_MISMATCH", "Retained destination archive differs from canonical preparation")
                content = {"source": _json(storage_codec.decode(facts["source_json"])), "destinations": [
                    _destination_snapshot(connection, row) for row in record.destinations]}
                if (_sha(content) != record.canonical_sha256 or facts["canonical_sha256"] != record.canonical_sha256
                        or provenance["canonical_sha256"] != record.canonical_sha256):
                    raise MigrationError("CANONICAL_HASH_MISMATCH", "Mapped destination content differs from canonical preparation")
                for destination in record.destinations:
                    if destination.table == "jobs":
                        event = connection.execute("""SELECT 1 FROM events WHERE domain='jobs' AND kind='imported'
                            AND record_id=? AND actor_id=? AND metadata_json=?""", (
                            destination.identity["id"], destination.values["actor_id"],
                            canonical_json({"source": source.reference}))).fetchone()
                        if event is None:
                            raise MigrationError("MISSING_JOB_EVENT", "Imported job has no canonical pending/history event")
                    if destination.table == "attachments":
                        reference_json = _json(destination.values["reference_json"])
                        path = Path(reference_json["path"])
                        if (not path.is_file() or path.is_symlink()
                                or hashlib.sha256(path.read_bytes()).hexdigest() != destination.values["content_sha256"]):
                            raise MigrationError("ATTACHMENT_HASH_MISMATCH", "Retained attachment is absent or changed")
                checked += 1
            except (MigrationError, OSError, ValueError) as error:
                failures.append({"source": source.reference, "code": getattr(error, "code", "VERIFY_ERROR")})
        failures.extend({"table": row[0], "code": "FOREIGN_KEY_VIOLATION"}
                        for row in connection.execute("PRAGMA foreign_key_check"))
    return {"valid": not failures, "checked_records": checked, "expected_records": len(prepared.records), "failures": failures}
