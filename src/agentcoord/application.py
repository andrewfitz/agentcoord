"""Compose the portable domains and own one workspace service lifecycle."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import signal
import socket
import sqlite3
import threading
import time
from pathlib import Path

from . import (
    __version__,
    commits,
    core,
    decisions,
    identity,
    jobs,
    messages,
    migrate,
    operator,
    pending,
    readiness,
    transport,
    work,
)
from .config import discover_workspace, load_config
from .core import Context, CoordinationError, canonical_json
from .store import SCHEMA_VERSION, Store

_LOG = logging.getLogger(__name__)
_DOMAINS = (identity, work, messages, decisions, readiness, commits, jobs, pending, migrate)


def open_workspace(root=None):
    return discover_workspace(explicit_root=Path(root) if root else None)


def build_service(workspace, config=None):
    config = config or load_config(workspace)
    with transport.ownership_lock(workspace.state_dir / "service.lock"):
        store = _initialize_store(workspace, config)
    operations = tuple(
        item
        for domain in (
            core,
            identity,
            work,
            messages,
            decisions,
            readiness,
            commits,
            jobs,
            pending,
            operator,
        )
        for item in domain.operations()
    )
    service = core.Service(
        store,
        workspace,
        config,
        operations,
        adapters={
            "pending": pending.select,
            "slow_handlers": {
                "commit.execute": commits.execute_operation,
                "commit.reconcile": commits.execute_operation,
                "job.execute": jobs.execute_operation,
                **readiness.slow_handlers(),
            },
        },
    )

    def boundary(context):
        with store.write() as tx:
            return pending.select(
                tx, context, new_only=True, present=True, byte_budget=config.action_bytes
            )

    service.adapters["boundary_digest"] = boundary
    service.adapters["observe_native"] = lambda target: _observe_native(service, target)
    return service


def _initialize_store(workspace, config=None):
    store = Store(workspace.database_path, workspace.id, config=config)
    store.initialize(tuple(domain.SCHEMA for domain in _DOMAINS))
    return store


def _observe_native(service, target):
    """Observe the registered native conversation, not just a captured dead PID."""
    with service.store.read() as tx:
        rows = tx.connection.execute(
            """SELECT a.*,e.process_identity_json AS execution_process
            FROM actors a LEFT JOIN executions e ON e.generation=a.current_execution_generation
            WHERE a.harness=? AND a.native_session_id=? AND a.child_id=''""",
            (target.get("harness"), target.get("native_session_id")),
        ).fetchall()
        if len(rows) != 1:
            return "unknown"
        row = dict(rows[0])
        if tx.connection.execute(
            "SELECT 1 FROM bindings WHERE actor_id=? AND revoked_us IS NULL LIMIT 1", (row["id"],)
        ).fetchone():
            return "unknown"
    captured = target.get("process_identity")
    if (
        not captured
        or not row["execution_process"]
        or json.loads(row["execution_process"]) != captured
    ):
        return "unknown"
    latest = json.loads(row["process_identity_json"]) if row["process_identity_json"] else None
    if latest != captured:
        return "unknown"
    return {"alive": "live", "gone": "offline", "unknown": "unknown"}[
        identity.process_status(latest)
    ]


def _lifecycle(service, state):
    with service.store.write(maintenance=True) as tx:
        current = json.loads(
            tx.connection.execute(
                "SELECT value_json FROM meta WHERE key='service_state'"
            ).fetchone()[0]
        )
        if state == "draining" and current in {"fenced", "importing", "upgrading", "quiescent"}:
            return
        if state == "active":
            if current == "fenced":
                raise CoordinationError(
                    "AUTHORITY_FENCED", "This workspace authority has been retired"
                )
            blocked = (
                tx.connection.execute(
                    "SELECT 1 FROM import_runs WHERE state!='complete' LIMIT 1"
                ).fetchone()
                or tx.connection.execute(
                    "SELECT 1 FROM import_issues WHERE required=1 AND state='open' LIMIT 1"
                ).fetchone()
            )
            if blocked:
                raise CoordinationError(
                    "RECONCILIATION_REQUIRED", "Complete and verify the import before activating"
                )
        tx.connection.execute(
            "UPDATE meta SET value_json=? WHERE key='service_state'", (canonical_json(state),)
        )


def _recover(service):
    """A lost worker is not evidence that its external action did not happen."""
    with service.store.read() as tx:
        rows = [
            dict(row)
            for row in tx.connection.execute("SELECT * FROM operations WHERE state='running'")
        ]
    observations = [
        (
            row,
            identity.process_status(json.loads(row["owner_identity_json"]))
            if row["owner_identity_json"]
            else "unknown",
        )
        for row in rows
    ]
    with service.store.write(maintenance=True) as tx:
        for row, observed in observations:
            current = tx.connection.execute(
                "SELECT state,claim_token FROM operations WHERE id=?", (row["id"],)
            ).fetchone()
            if (
                not current
                or current["state"] != "running"
                or current["claim_token"] != row["claim_token"]
            ):
                continue
            # Even when an owned subprocess might survive the daemon, quarantine it.
            # Reconciliation inspects that exact process/publication before retry.
            pure_readiness = (
                row["kind"] in readiness.slow_handlers() and row["effect_started_us"] is None
            )
            service.finish_operation(
                tx,
                row["id"],
                "failed" if pure_readiness else "uncertain",
                claim_token=row["claim_token"],
                error={
                    "code": "OPERATION_FAILED" if pure_readiness else "RECONCILIATION_REQUIRED",
                    "message": "Interrupted readiness calculation; submit fresh inputs"
                    if pure_readiness
                    else "Previous worker ended without a terminal receipt",
                    "retryable": False,
                    "details": {"owner_observation": observed},
                },
            )
        tx.connection.execute("DELETE FROM bindings")
        tx.connection.execute(
            "INSERT OR REPLACE INTO meta(key,value_json) VALUES ('release',?)",
            (canonical_json(__version__),),
        )


def _presence_batch(service, after):
    with service.store.read() as tx:
        rows = [
            dict(row)
            for row in tx.connection.execute(
                """SELECT id,process_identity_json,
            current_execution_generation FROM actors WHERE archived=0 AND id>? ORDER BY id LIMIT 32""",
                (after,),
            )
        ]
    if not rows:
        return ""
    observed = [
        (
            row,
            identity.process_status(json.loads(row["process_identity_json"]))
            if row["process_identity_json"]
            else "unknown",
        )
        for row in rows
    ]
    with service.store.write() as tx:
        for row, status in observed:
            current = tx.connection.execute(
                "SELECT * FROM actors WHERE id=?", (row["id"],)
            ).fetchone()
            if not current or (
                current["process_identity_json"],
                current["current_execution_generation"],
            ) != (row["process_identity_json"], row["current_execution_generation"]):
                continue
            state = {"alive": "running", "gone": "offline", "unknown": "unknown"}[status]
            tx.connection.execute(
                """INSERT INTO presence(actor_id,observed_state,observed_us,process_identity_json,confidence)
                VALUES (?,?,?,?,?) ON CONFLICT(actor_id) DO UPDATE SET observed_state=excluded.observed_state,
                observed_us=excluded.observed_us,process_identity_json=excluded.process_identity_json,confidence=excluded.confidence""",
                (
                    row["id"],
                    state,
                    tx.now_us,
                    row["process_identity_json"],
                    "unknown" if status == "unknown" else "verified",
                ),
            )
            if state == "offline" and current["reported_state"] == "completed":
                tx.connection.execute(
                    """UPDATE actors SET archived=1 WHERE id=? AND NOT EXISTS
                    (SELECT 1 FROM bindings WHERE actor_id=? AND revoked_us IS NULL) AND NOT EXISTS
                    (SELECT 1 FROM operations WHERE actor_id=? AND state IN ('queued','running','uncertain'))""",
                    (row["id"], row["id"], row["id"]),
                )
    return rows[-1]["id"]


class _Runtime:
    def __init__(self, service, server):
        self.service, self.server = service, server
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.owner = identity.process_identity(os.getpid())
        self.thread = threading.Thread(target=self.run, name="agentcoord-maintenance", daemon=True)
        self.error = None

    def run(self):
        after, next_presence = "", 0.0
        while not self.stop.is_set():
            try:
                with self.service.store.read() as tx:
                    state = json.loads(
                        tx.connection.execute(
                            "SELECT value_json FROM meta WHERE key='service_state'"
                        ).fetchone()[0]
                    )
                if state == "active":
                    jobs.claim_due(self.service, limit=16)
                    with self.service.store.write() as tx:
                        decisions.route_due(
                            tx,
                            Context(self.service.workspace.id, None, transport="worker"),
                            limit=20,
                        )
                    with self.service.store.read() as tx:
                        queued = [
                            row[0]
                            for row in tx.connection.execute(
                                "SELECT id FROM operations WHERE state='queued' ORDER BY created_us,id LIMIT ?",
                                (
                                    self.service.config.slow_workers
                                    + self.service.config.slow_queue,
                                ),
                            )
                        ]
                    for operation_id in queued:
                        if not self.server.submit_slow(
                            operation_id,
                            lambda oid: self.service.run_operation(oid, owner_identity=self.owner),
                        ):
                            break
                    if time.monotonic() >= next_presence:
                        after = _presence_batch(self.service, after)
                        next_presence = time.monotonic() + 5
                self.error = None
            except Exception as error:  # noqa: BLE001 — retain unexpected maintenance failures in health and logs.
                # Health exposes failure; do not turn a broken scheduler into a healthy idle service.
                self.error = {
                    "code": getattr(error, "code", "MAINTENANCE_FAILED"),
                    "type": type(error).__name__,
                }
                _LOG.exception("Workspace maintenance failed")
            self.wake.wait(1)
            self.wake.clear()


@contextlib.contextmanager
def make_server(service):
    runtime = None
    restart_active = False

    def bind(native, transport, connection_id):
        return identity.bind_native(
            service.store, native, transport=transport, connection_id=connection_id
        )["context"]

    def unbind(context):
        with service.store.write(maintenance=True) as tx:
            tx.connection.execute(
                "DELETE FROM bindings WHERE id=? AND actor_id=?",
                (context.connection_id, context.actor_id),
            )

    def health():
        with service.store.read() as tx:
            counts = dict(
                tx.connection.execute(
                    "SELECT state,COUNT(*) FROM operations WHERE state IN ('running','uncertain') GROUP BY state"
                ).fetchall()
            )
            manual = tx.connection.execute("""SELECT COUNT(*) FROM commit_grants g JOIN commit_admissions a ON a.id=g.admission_id
                WHERE g.state IN ('active','uncertain') AND a.mode='manual'""").fetchone()[0]
        return {
            "schema_version": SCHEMA_VERSION,
            "release": __version__,
            "database_state": "ready",
            "running_effects": counts.get("running", 0) + manual,
            "uncertain_effects": counts.get("uncertain", 0),
            "maintenance_error": runtime.error if runtime else None,
        }

    def restore_activation():
        # Restore normal-stop activation after all requests close, while the
        # departing service still exclusively owns the workspace state.
        if restart_active:
            _lifecycle(service, "active")

    with transport.owned_server(
        service.workspace.socket_path,
        service,
        lock_path=service.workspace.state_dir / "service.lock",
        on_close=restore_activation,
        resource_scope=service.store.writer_lifespan,
        workspace_id=service.workspace.id,
        bind=bind,
        unbind=unbind,
        operator_context=lambda cid: Context(
            service.workspace.id, None, cid, "operator", operator=True, identity_mode="operator"
        ),
        routine_workers=service.config.fast_workers,
        routine_queue=service.config.fast_queue,
        max_connections=service.config.fast_workers + service.config.fast_queue,
        slow_workers=service.config.slow_workers,
        slow_queue=service.config.slow_queue,
        health=health,
        lifecycle=lambda state: _lifecycle(service, state),
    ) as server:
        _recover(service)
        with service.store.read() as tx:
            state = json.loads(
                tx.connection.execute(
                    "SELECT value_json FROM meta WHERE key='service_state'"
                ).fetchone()[0]
            )
        if state != "active":
            server._draining = True
        runtime = _Runtime(service, server)
        service.adapters["work_available"] = runtime.wake.set
        runtime.thread.start()
        try:
            yield server
        finally:
            try:
                with service.store.read() as tx:
                    restart_active = (
                        json.loads(
                            tx.connection.execute(
                                "SELECT value_json FROM meta WHERE key='service_state'"
                            ).fetchone()[0]
                        )
                        == "active"
                    )
            finally:
                runtime.stop.set()
                runtime.wake.set()
                runtime.thread.join()


def run_service(workspace, config=None):
    service = build_service(workspace, config)
    with make_server(service) as server:
        previous = {}

        def stop(signum, frame):
            threading.Thread(target=server.shutdown, daemon=True).start()

        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGINT):
                previous[sig] = signal.signal(sig, stop)
        try:
            server.serve_forever(poll_interval=0.25)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)


@contextlib.contextmanager
def open_offline_store(workspace):
    """Hold the same exclusive lock as the service throughout operator maintenance."""
    path = workspace.socket_path
    with transport.ownership_lock(workspace.state_dir / "service.lock"):
        if path.exists():
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.5)
                try:
                    probe.connect(str(path))
                except ConnectionRefusedError:
                    pass
                else:
                    raise CoordinationError("SERVICE_BUSY", "Workspace service is still listening")
        yield _initialize_store(workspace)


def backup_workspace(workspace, path):
    with open_offline_store(workspace) as store:
        return store.backup(Path(path))


def restore_workspace(workspace, path):
    """Restore only an empty destination; accepted native work is never overwritten."""
    source_path = Path(path).expanduser().absolute()
    if (
        source_path.is_symlink()
        or not source_path.is_file()
        or source_path == workspace.database_path
    ):
        raise CoordinationError(
            "INVALID_ARGUMENT", "Restore requires a separate regular backup file"
        )
    with open_offline_store(workspace) as store:
        with store.read() as tx:
            tables = [
                r[0]
                for r in tx.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT IN ('meta','sqlite_sequence')"
                )
            ]
            if any(
                tx.connection.execute(
                    'SELECT 1 FROM "' + table.replace('"', '""') + '" LIMIT 1'
                ).fetchone()
                for table in tables
            ):
                raise CoordinationError(
                    "RECONCILIATION_REQUIRED",
                    "Destination contains retained data; restore to an empty workspace state or reconcile forward",
                )
            expected = dict(
                tx.connection.execute(
                    "SELECT key,value_json FROM meta WHERE key IN ('workspace_id','schema_signature')"
                )
            )
        with sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True) as source:
            if (
                source.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
                or source.execute("PRAGMA foreign_key_check").fetchall()
            ):
                raise CoordinationError("STORAGE_UNAVAILABLE", "Backup verification failed")
            actual = dict(
                source.execute(
                    "SELECT key,value_json FROM meta WHERE key IN ('workspace_id','schema_signature')"
                )
            )
            if (
                actual != expected
                or source.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION
            ):
                raise CoordinationError(
                    "SCHEMA_MISMATCH", "Backup does not match this workspace and release"
                )
            with sqlite3.connect(store.path) as target:
                source.backup(target)
        return {"workspace_id": workspace.id, "restored": True, "path": str(source_path)}
