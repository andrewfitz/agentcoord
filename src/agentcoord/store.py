"""One SQLite authority with explicit transactional schema composition."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from pathlib import Path

from .config import Config
from .core import CoordinationError, canonical_json, now_us

_SQLITE_TIMEOUT = 10.0

SCHEMA_VERSION = 1
SCHEMA = (
    "CREATE TABLE meta(key TEXT PRIMARY KEY,value_json TEXT NOT NULL CHECK(json_valid(value_json)))",
    """CREATE TABLE events(sequence INTEGER PRIMARY KEY AUTOINCREMENT,domain TEXT NOT NULL,
        kind TEXT NOT NULL,record_id TEXT NOT NULL,actor_id TEXT,at_us INTEGER NOT NULL,
        metadata_json TEXT NOT NULL CHECK(json_valid(metadata_json)))""",
    "CREATE INDEX events_actor_sequence ON events(actor_id,sequence)",
    "CREATE INDEX events_record_sequence ON events(domain,record_id,sequence)",
    """CREATE TABLE idempotency(actor_id TEXT NOT NULL REFERENCES actors(id),retry_key TEXT NOT NULL,
        operation TEXT NOT NULL,input_sha256 TEXT NOT NULL,result_json TEXT NOT NULL
        CHECK(json_valid(result_json)),payload_sha256 TEXT NOT NULL,context_json TEXT NOT NULL
        CHECK(json_valid(context_json)),created_us INTEGER NOT NULL,PRIMARY KEY(actor_id,retry_key))""",
    """CREATE TABLE operations(id TEXT PRIMARY KEY,actor_id TEXT NOT NULL REFERENCES actors(id),kind TEXT NOT NULL,
        task_generation TEXT NOT NULL,execution_generation TEXT,authority_generation TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('queued','running','succeeded','failed','cancelled','uncertain')),
        arguments_json TEXT NOT NULL CHECK(json_valid(arguments_json)),arguments_sha256 TEXT NOT NULL,
        retry_key TEXT NOT NULL,owner_identity_json TEXT CHECK(owner_identity_json IS NULL OR json_valid(owner_identity_json)),
        claim_token TEXT,lease_until_us INTEGER,effect_started_us INTEGER,result_json TEXT
        CHECK(result_json IS NULL OR json_valid(result_json)),error_json TEXT
        CHECK(error_json IS NULL OR json_valid(error_json)),created_us INTEGER NOT NULL,updated_us INTEGER NOT NULL,
        sequence INTEGER NOT NULL DEFAULT 0,acknowledged_sequence INTEGER,acknowledged_us INTEGER,
        UNIQUE(actor_id,kind,retry_key))""",
    "CREATE INDEX operations_pending ON operations(state,created_us,id)",
    "CREATE INDEX operations_actor ON operations(actor_id,state,created_us,id)",
)


def private_directory(path: Path) -> None:
    path = Path(path).absolute()
    for item in (path, *path.parents):
        if item.is_symlink():
            raise CoordinationError("INVALID_ARGUMENT", "State path must not contain symlinks")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir() or path.stat().st_uid != os.getuid():
        raise CoordinationError("NOT_AUTHORIZED", "State directory must belong to the local user")
    os.chmod(path, 0o700)


class Transaction:
    def __init__(self, connection):
        self.connection = connection
        self.now_us = now_us()

    def event(self, domain: str, kind: str, record_id: str, actor_id: str | None,
              metadata: dict | None = None) -> int:
        return self.connection.execute("INSERT INTO events(domain,kind,record_id,actor_id,at_us,metadata_json) VALUES (?,?,?,?,?,?)",
            (domain, kind, record_id, actor_id, self.now_us, canonical_json(metadata or {}))).lastrowid


class _WriterAdmission:
    """FIFO admission for one Store's short write transactions.

    SQLite still owns database exclusion, including other processes. Directly
    handing the next waiter its slot avoids SQLite busy-backoff starvation and
    keeps waiting writers from opening connections. Readers use no slot.
    """

    def __init__(self, capacity: int, timeout: float):
        self.capacity, self.timeout = capacity, timeout
        self._mutex = threading.Lock()
        self._owner = None
        self._pending = deque()

    @contextmanager
    def enter(self):
        waiter = (threading.get_ident(), threading.Event())
        deadline = time.monotonic() + self.timeout
        with self._mutex:
            if self._owner is not None and self._owner[0] == waiter[0]:
                raise CoordinationError("INVALID_ARGUMENT", "Nested Store write transactions are not supported")
            if self._owner is None:
                self._owner = waiter
                waiter[1].set()
            elif len(self._pending) >= self.capacity:
                raise CoordinationError("SERVICE_BUSY", "Writer admission budget exhausted", retryable=True)
            else:
                self._pending.append(waiter)
        try:
            if not waiter[1].wait(max(0.0, deadline - time.monotonic())) or time.monotonic() >= deadline:
                raise CoordinationError("SERVICE_BUSY", "Writer admission deadline exceeded", retryable=True)
            yield deadline
        finally:
            with self._mutex:
                if self._owner is waiter:
                    self._owner = self._pending.popleft() if self._pending else None
                    if self._owner is not None:
                        self._owner[1].set()
                else:
                    self._pending.remove(waiter)


class Store:
    def __init__(self, path: Path, workspace_id: str, *, config: Config | None = None):
        self.path, self.workspace_id = Path(path).absolute(), workspace_id
        if self.path.is_symlink():
            raise CoordinationError("INVALID_ARGUMENT", "Database must not be a symlink")
        config = config or Config()
        # Every admitted socket can bind, mutate or unbind once at a time.
        # Slow workers, the runtime sweep and lifecycle caller also write, so
        # connection teardown must fit independently of routine queue depth.
        writer_population = config.fast_workers + config.fast_queue + config.slow_workers + 2
        self._writer = _WriterAdmission(writer_population, _SQLITE_TIMEOUT)
        self._writer_pid = None
        self._writer_identity = None
        self._writer_connection = None

    def _validate_connection(self, connection):
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            raise CoordinationError("SCHEMA_MISMATCH", "Database schema requires a compatible service")
        row = connection.execute("SELECT value_json FROM meta WHERE key='workspace_id'").fetchone()
        if row is None or json.loads(row[0]) != self.workspace_id:
            raise CoordinationError("WRONG_WORKSPACE", "Database belongs to another workspace")

    def _connect(self, *, readonly=False, initializing=False, timeout=_SQLITE_TIMEOUT, shared=False):
        for item in (self.path, *self.path.parents):
            if item.is_symlink():
                raise CoordinationError("INVALID_ARGUMENT", "Database path must not contain symlinks")
        if not self.path.is_file():
            raise CoordinationError("STORAGE_UNAVAILABLE", "Database is not initialized")
        connection = sqlite3.connect(self.path.as_uri() + ("?mode=ro" if readonly else "?mode=rw"),
                                     uri=True, timeout=timeout, isolation_level=None,
                                     check_same_thread=not shared)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            if readonly:
                connection.execute("PRAGMA query_only=ON")
            if not initializing:
                self._validate_connection(connection)
        except BaseException:
            connection.close()
            raise
        return connection

    def _database_identity(self):
        for item in (self.path, *self.path.parents):
            if item.is_symlink():
                raise CoordinationError("INVALID_ARGUMENT", "Database path must not contain symlinks")
        try:
            info = self.path.stat()
        except FileNotFoundError as error:
            raise CoordinationError("STORAGE_UNAVAILABLE", "Database is not initialized") from error
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise CoordinationError("NOT_AUTHORIZED", "Database must remain a local-user regular file")
        return info.st_dev, info.st_ino

    def _check_writer_identity(self):
        if self._database_identity() != self._writer_identity:
            raise CoordinationError("AUTHORITY_FENCED", "Database file changed during service ownership")

    def _close_writer_connection(self):
        connection, self._writer_connection = self._writer_connection, None
        if connection is not None:
            connection.close()

    @contextmanager
    def writer_lifespan(self):
        """Reuse one writer only within the daemon's exclusive ownership scope."""
        with self._writer.enter():
            if self._writer_pid is not None:
                raise CoordinationError("INVALID_ARGUMENT", "Writer lifespan is already active")
            self._writer_identity = self._database_identity()
            self._writer_pid = os.getpid()
        try:
            yield
        finally:
            if self._writer_pid != os.getpid():
                raise CoordinationError("AUTHORITY_FENCED", "Writer lifespan belongs to another process")
            with self._writer.enter():
                try:
                    self._close_writer_connection()
                finally:
                    self._writer_pid = self._writer_identity = None

    def _write_connection(self, deadline):
        remaining = max(0.0, deadline - time.monotonic())
        if self._writer_pid is None:
            return self._connect(timeout=remaining), False
        self._check_writer_identity()
        if self._writer_connection is None:
            connection = self._connect(timeout=remaining, shared=True)
            try:
                self._check_writer_identity()
            except BaseException:
                connection.close()
                raise
            self._writer_connection = connection
        return self._writer_connection, True

    def initialize(self, schema_fragments=()) -> None:
        private_directory(self.path.parent)
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise CoordinationError("NOT_AUTHORIZED", "Database must be a local-user regular file")
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        statements = (*SCHEMA, *(statement for fragment in schema_fragments for statement in fragment))
        signature = hashlib.sha256(canonical_json(statements).encode()).hexdigest()
        db = self._connect(initializing=True)
        try:
            if db.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
                raise CoordinationError("STORAGE_UNAVAILABLE", "Database cannot enable WAL")
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version:
                if version != SCHEMA_VERSION:
                    raise CoordinationError("SCHEMA_MISMATCH", "Database schema requires explicit upgrade")
                expected = {"workspace_id": self.workspace_id, "schema_signature": signature}
                for key, value in expected.items():
                    row = db.execute("SELECT value_json FROM meta WHERE key=?", (key,)).fetchone()
                    if row is None or json.loads(row[0]) != value:
                        raise CoordinationError("SCHEMA_MISMATCH" if key == "schema_signature" else "WRONG_WORKSPACE",
                                                "Existing database does not match this service")
            else:
                for statement in statements:
                    db.execute(statement)
                for key, value in {"workspace_id": self.workspace_id, "schema_signature": signature,
                                   "authority_generation": str(uuid.uuid4()), "service_state": "active"}.items():
                    db.execute("INSERT INTO meta(key,value_json) VALUES (?,?)", (key, canonical_json(value)))
                if db.execute("PRAGMA foreign_key_check").fetchall():
                    raise CoordinationError("SCHEMA_MISMATCH", "Schema contains invalid foreign keys")
                db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @contextmanager
    def read(self):
        db = self._connect(readonly=True)
        try:
            db.execute("BEGIN")
            yield Transaction(db)
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @contextmanager
    def write(self, *, maintenance=False):
        # A child must fail before touching an inherited gate or SQLite handle.
        if self._writer_pid is not None and self._writer_pid != os.getpid():
            raise CoordinationError("AUTHORITY_FENCED", "Writer lifespan belongs to another process")
        with self._writer.enter() as deadline:
            db, retained = self._write_connection(deadline)
            try:
                remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
                db.execute(f"PRAGMA busy_timeout={remaining_ms}")
                db.execute("BEGIN IMMEDIATE")
                if retained:
                    self._check_writer_identity()
                    self._validate_connection(db)
                    if (db.execute("PRAGMA synchronous").fetchone()[0] != 2
                            or db.execute("PRAGMA foreign_keys").fetchone()[0] != 1):
                        raise CoordinationError("STORAGE_UNAVAILABLE", "Writer durability or constraint settings changed")
                state = db.execute("SELECT value_json FROM meta WHERE key='service_state'").fetchone()
                if not maintenance and state and json.loads(state[0]) in {"fenced", "quiescent", "importing", "upgrading"}:
                    raise CoordinationError("AUTHORITY_FENCED", "Workspace mutations are fenced")
                yield Transaction(db)
                if retained:
                    self._check_writer_identity()
                    self._validate_connection(db)
                db.commit()
            except BaseException as error:
                try:
                    db.rollback()
                except BaseException:
                    if retained:
                        self._close_writer_connection()
                    raise
                if retained and isinstance(error, sqlite3.Error):
                    self._close_writer_connection()
                raise
            finally:
                if not retained:
                    db.close()

    def backup(self, destination: Path) -> dict:
        destination = Path(destination).absolute()
        if destination == self.path:
            raise CoordinationError("INVALID_ARGUMENT", "Backup destination must be separate")
        private_directory(destination.parent)
        descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)
        source = self._connect(readonly=True)
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or target.execute("PRAGMA foreign_key_check").fetchall():
                raise CoordinationError("STORAGE_UNAVAILABLE", "Backup verification failed")
            return {"workspace_id": self.workspace_id, "path": str(destination), "verified": True}
        finally:
            target.close()
            source.close()
