"""Persistent, bounded Unix-socket adapters for one workspace service."""

from __future__ import annotations

import contextlib
import copy
import fcntl
import json
import logging
import os
import secrets
import socket
import socketserver
import stat
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .core import UNSET, Call, CoordinationError, identifier

PROTOCOL = 1
MAX_FRAME = 262_144


def error_envelope(
    code, message, *, request_id=None, retryable=False, details=None, next_action=None
):
    return {
        "ok": False,
        "protocol": PROTOCOL,
        "request_id": request_id,
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
            "details": details or {},
            "next_action": next_action,
        },
    }


def encode_frame(value):
    try:
        data = (
            json.dumps(
                value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
            + b"\n"
        )
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise CoordinationError(
            "INVALID_ARGUMENT", "Frame must contain canonical JSON-safe values"
        ) from exc
    if len(data) > MAX_FRAME:
        raise CoordinationError("INVALID_ARGUMENT", "Frame exceeds 256 KiB; request a bounded page")
    return data


def read_frame(stream):
    try:
        data = stream.readline(MAX_FRAME + 1)
    except ValueError as error:
        if getattr(stream, "closed", False):
            # close() can interrupt the bind read before or during readline;
            # both are transport loss, not an uncaught worker exception.
            raise EOFError from error
        raise
    if not data:
        raise EOFError
    if len(data) > MAX_FRAME or not data.endswith(b"\n"):
        raise CoordinationError("INVALID_ARGUMENT", "Expected a bounded newline JSON frame")
    try:
        value = json.loads(
            data.decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError())
        )
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise CoordinationError("INVALID_ARGUMENT", "Invalid UTF-8 JSON frame") from exc
    if not isinstance(value, dict):
        raise CoordinationError("INVALID_ARGUMENT", "Frame must be a JSON object")
    return value


def private_directory(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise CoordinationError("NOT_AUTHORIZED", "Socket directory must be owned and private")


def _exception(exc, request_id=None):
    if isinstance(exc, CoordinationError):
        return error_envelope(
            exc.code,
            exc.message,
            request_id=request_id,
            retryable=exc.retryable,
            details=exc.details,
            next_action=exc.next_action,
        )
    logging.getLogger(__name__).exception("Coordination boundary failed", exc_info=exc)
    return error_envelope(
        "OPERATION_FAILED",
        "Operation failed; inspect the service diagnostic log",
        request_id=request_id,
    )


class Client:
    """One native binding per connection; responses never expose its capability."""

    def __init__(
        self,
        path,
        native_context=None,
        *,
        workspace_id,
        operator=False,
        transport="cli",
        timeout=30,
    ):
        self.path = Path(path)
        self.native_context = dict(native_context or {})
        self._startup_context = copy.deepcopy(self.native_context)
        self.workspace_id = workspace_id
        self.operator = operator
        self.transport = transport
        self.timeout = timeout
        self._socket = None
        self._binding_epoch = 0
        self._stream = None
        self._lock = threading.Lock()
        self._next_context = {"task_generation": None, "execution_generation": None}
        self._retry_context = OrderedDict()
        self._uncertain_keys = set()

    def connect(self):
        if self._socket is not None:
            return self
        private_directory(self.path.parent)
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout)
        self._socket = connection
        try:
            connection.connect(str(self.path))
            stream = connection.makefile("rb")
            self._stream = stream
            bind_frame = encode_frame(
                {
                    "protocol": PROTOCOL,
                    "workspace_id": self.workspace_id,
                    "kind": "bind",
                    "transport": self.transport,
                    "operator": self.operator,
                    "native_context": self._startup_context,
                }
            )
            try:
                connection.sendall(bind_frame)
            except OSError as send_error:
                # Overload admission can reject and close before the bind is
                # sent. Recover only the peer's actual buffered error frame;
                # a missing frame remains the original transport failure.
                try:
                    reply = read_frame(stream)
                except (OSError, EOFError):
                    raise send_error
                rejection = reply.get("error")
                if (reply.get("ok") is not False or not isinstance(rejection, dict)
                        or not isinstance(rejection.get("code"), str) or not rejection["code"]
                        or not isinstance(rejection.get("message"), str)):
                    raise
            else:
                reply = read_frame(stream)
            if reply.get("protocol") != PROTOCOL:
                raise CoordinationError("PROTOCOL_MISMATCH", "Reconnect using the installed client")
            if not reply.get("ok"):
                error = reply.get("error", {})
                raise CoordinationError(
                    error.get("code", "UNBOUND_ACTOR"),
                    error.get("message", "Binding failed"),
                    retryable=error.get("retryable", False),
                    details=error.get("details", {}),
                    next_action=error.get("next_action"),
                )
            if self._socket is not connection:
                raise EOFError
            self._next_context.update(reply.get("next_context", {}))
            self._binding_epoch += 1
        except Exception:
            if self._socket is connection:
                self._socket = self._stream = None
            if "stream" in locals():
                stream.close()
            connection.close()
            raise
        return self

    @property
    def binding_revision(self):
        """Local binding/execution revision, for connection-scoped push routes."""
        return self._binding_epoch, self._next_context.get("execution_generation")

    def close(self):
        stream, connection = self._stream, self._socket
        self._stream = self._socket = None
        # Interrupt readline before closing its buffered stream; close alone may
        # wait for the reader's internal lock and freeze an operator exit.
        if connection is not None:
            with contextlib.suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)
        if stream is not None:
            stream.close()
        if connection is not None:
            connection.close()

    def __enter__(self):
        return self.connect()

    def __exit__(self, *_):
        self.close()

    def call(self, operation, arguments=None, key=None, *,
             expected_task_generation=UNSET, expected_execution_generation=UNSET):
        """Call with optional fixed generation preconditions, retained for keyed retries."""
        explicit = {}
        for name, value in (("task_generation", expected_task_generation),
                            ("execution_generation", expected_execution_generation)):
            if value is not UNSET:
                if value is not None:
                    identifier(value, "expected_" + name)
                explicit[name] = value
        request_id = secrets.token_hex(16)
        with self._lock:
            try:
                self.connect()
                requested_guards = {**self._next_context, **explicit}
                if key:
                    if key in self._retry_context and any(
                        self._retry_context[key].get(name) != value for name, value in explicit.items()
                    ):
                        return error_envelope(
                            "IDEMPOTENCY_CONFLICT", "Retry key retains its original generation preconditions",
                            request_id=request_id,
                        )
                    if key not in self._retry_context and len(self._retry_context) >= 256:
                        candidate = next(
                            (old for old in self._retry_context if old not in self._uncertain_keys),
                            None,
                        )
                        if candidate is None:
                            return error_envelope(
                                "SERVICE_BUSY",
                                "Client has 256 unresolved retry receipts; reconcile them before new mutations",
                                request_id=request_id,
                                next_action="Inspect retained receipts",
                            )
                        del self._retry_context[candidate]
                    guards = self._retry_context.setdefault(key, requested_guards)
                    self._retry_context.move_to_end(key)
                else:
                    guards = requested_guards
                frame = {
                    "protocol": PROTOCOL,
                    "workspace_id": self.workspace_id,
                    "request_id": request_id,
                    "operation": operation,
                    "arguments": arguments or {},
                    "key": key,
                    "expected_task_generation": guards.get("task_generation"),
                    "expected_execution_generation": guards.get("execution_generation"),
                }
                connection, stream = self._socket, self._stream
                if connection is None or stream is None:
                    raise EOFError
                connection.sendall(encode_frame(frame))
                result = read_frame(stream)
                if result.get("protocol") != PROTOCOL or result.get("request_id") != request_id:
                    raise CoordinationError(
                        "PROTOCOL_MISMATCH", "Service response identity does not match the request"
                    )
                if isinstance(result.get("next_context"), dict):
                    self._next_context.update(result["next_context"])
                self._uncertain_keys.discard(key)
                return result
            except (OSError, EOFError):
                self.close()
                if key:
                    self._uncertain_keys.add(key)
                return error_envelope(
                    "RECONCILIATION_REQUIRED" if key else "STORAGE_UNAVAILABLE",
                    "Service response unavailable; the operation may have committed"
                    if key
                    else "Workspace service unavailable",
                    request_id=request_id,
                    details={"retry_key": key} if key else {},
                    next_action="Inspect receipt.get with the same retry key before retrying"
                    if key
                    else "Start the workspace service",
                )

    def health(self):
        return self.call("service.health")

    def drain(self, *, key=None):
        return self.call("service.drain", key=key or secrets.token_hex(16))

    def activate(self, *, key=None):
        return self.call("service.activate", key=key or secrets.token_hex(16))


class _RoutineAdmission:
    """Hand routine slots to bounded FIFO waiters before admitting fresh calls."""

    def __init__(self, workers, capacity):
        self.workers, self.capacity = workers, capacity
        self._mutex = threading.Lock()
        self._running = 0
        self._pending = deque()

    def _release_locked(self):
        if self._pending:
            self._pending.popleft().set()
        else:
            self._running -= 1

    def _withdraw_locked(self, waiter):
        if waiter.is_set():
            self._release_locked()
        else:
            self._pending.remove(waiter)

    def acquire(self, timeout):
        waiter = threading.Event()
        with self._mutex:
            if self._running < self.workers and not self._pending:
                self._running += 1
                return True
            if len(self._pending) >= self.capacity:
                return False
            self._pending.append(waiter)
        try:
            acquired = waiter.wait(timeout)
        except BaseException:
            with self._mutex:
                self._withdraw_locked(waiter)
            raise
        if not acquired:
            with self._mutex:
                self._withdraw_locked(waiter)
        return acquired

    def release(self):
        with self._mutex:
            if not self._running:
                raise ValueError("Routine admission released without a slot")
            self._release_locked()


class RPCServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    """Connections and routine/slow execution each have independent budgets."""

    daemon_threads = False
    block_on_close = True
    request_queue_size = 256

    def __init__(
        self,
        path,
        service,
        *,
        workspace_id,
        bind: Callable,
        operator_context: Callable,
        routine_workers=16,
        routine_queue=128,
        max_connections=256,
        slow_workers=4,
        slow_queue=64,
        admission_timeout=10,
        idle_timeout=300,
        health=None,
        lifecycle=None,
        unbind=None,
    ):
        if min(routine_workers, routine_queue, max_connections, slow_workers, slow_queue) < 1:
            raise ValueError("Service budgets must be positive")
        self.service, self.workspace_id = service, workspace_id
        self.bind, self.operator_context = bind, operator_context
        self.unbind = unbind
        self.health_provider, self.lifecycle = health, lifecycle
        self.admission_timeout, self.idle_timeout = admission_timeout, idle_timeout
        self._connections = threading.BoundedSemaphore(max_connections)
        self._routine = _RoutineAdmission(routine_workers, routine_queue)
        self._slow_slots = threading.BoundedSemaphore(slow_workers + slow_queue)
        self._slow = ThreadPoolExecutor(
            max_workers=slow_workers, thread_name_prefix="agentcoord-slow"
        )
        self._condition = threading.Condition()
        self._active = 0
        self._slow_ids = set()
        self._sockets = set()
        self._draining = False
        self._path = Path(path)
        private_directory(self._path.parent)
        if len(os.fsencode(path)) >= 104:
            raise CoordinationError(
                "INVALID_ARGUMENT", "Unix socket path exceeds the portable 103-byte address budget"
            )
        super().__init__(str(path), _Handler)
        self._path.chmod(0o600)
        self._inode = self._path.stat().st_ino

    def process_request(self, request, client_address):
        if not self._connections.acquire(blocking=False):
            request.sendall(
                encode_frame(
                    error_envelope("SERVICE_BUSY", "Connection budget exhausted", retryable=True)
                )
            )
            self.shutdown_request(request)
            return
        try:
            with self._condition:
                self._sockets.add(request)
            super().process_request(request, client_address)
        except BaseException:
            with self._condition:
                self._sockets.discard(request)
            self._connections.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._condition:
                self._sockets.discard(request)
            self._connections.release()

    def health(self):
        report = dict(self.health_provider() if self.health_provider else {})
        with self._condition:
            report.update(
                workspace_id=self.workspace_id,
                protocol=PROTOCOL,
                service_state="quiescent"
                if self._draining
                and not self._active
                and not self._slow_ids
                and not report.get("running_effects", 0)
                and not report.get("uncertain_effects", 0)
                else "draining"
                if self._draining
                else "active",
                active_requests=self._active,
                slow_operations=len(self._slow_ids),
            )
        return report

    def begin_drain(self):
        with self._condition:
            self._draining = True
        if self.lifecycle:
            self.lifecycle("draining")
        return self.health()

    def activate(self):
        if self.lifecycle:
            self.lifecycle("active")
        with self._condition:
            self._draining = False
        return self.health()

    def wait_quiescent(self, timeout=30):
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._active or self._slow_ids:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
        return self.health()["service_state"] == "quiescent"

    def submit_slow(self, operation_id, runner):
        with self._condition:
            if operation_id in self._slow_ids:
                return True
            if self._draining or not self._slow_slots.acquire(blocking=False):
                return False
            self._slow_ids.add(operation_id)

        def execute():
            try:
                runner(operation_id)
            finally:
                with self._condition:
                    self._slow_ids.discard(operation_id)
                    self._condition.notify_all()
                self._slow_slots.release()

        try:
            self._slow.submit(execute)
        except RuntimeError:
            with self._condition:
                self._slow_ids.discard(operation_id)
                self._condition.notify_all()
            self._slow_slots.release()
            return False
        return True

    def dispatch(self, context, frame):
        request_id = frame.get("request_id")
        if set(frame) != {
            "protocol",
            "workspace_id",
            "request_id",
            "operation",
            "arguments",
            "key",
            "expected_task_generation",
            "expected_execution_generation",
        }:
            return error_envelope(
                "INVALID_ARGUMENT", "Call contains missing or unknown fields", request_id=request_id
            )
        if type(frame["protocol"]) is not int or frame["protocol"] != PROTOCOL:
            return error_envelope(
                "PROTOCOL_MISMATCH", "Reconnect using the installed client", request_id=request_id
            )
        if frame["workspace_id"] != self.workspace_id:
            return error_envelope(
                "WRONG_WORKSPACE", "Connection belongs to another workspace", request_id=request_id
            )
        if (
            not isinstance(request_id, str)
            or not 1 <= len(request_id) <= 128
            or "\x00" in request_id
        ):
            return error_envelope("INVALID_ARGUMENT", "Invalid request ID", request_id=None)
        operation, arguments, key = frame["operation"], frame["arguments"], frame["key"]
        guards = frame["expected_task_generation"], frame["expected_execution_generation"]
        if any(
            value is not None
            and (not isinstance(value, str) or not value or len(value) > 128 or "\0" in value)
            for value in guards
        ):
            return error_envelope(
                "INVALID_ARGUMENT", "Invalid generation guard", request_id=request_id
            )
        if (
            not isinstance(operation, str)
            or not isinstance(arguments, dict)
            or (key is not None and not isinstance(key, str))
        ):
            return error_envelope("INVALID_ARGUMENT", "Invalid typed call", request_id=request_id)
        if operation in {"service.health", "service.drain", "service.activate"}:
            if arguments or not context.operator:
                return error_envelope(
                    "NOT_AUTHORIZED",
                    "Service maintenance requires an operator connection",
                    request_id=request_id,
                )
            try:
                data = (
                    self.health()
                    if operation == "service.health"
                    else self.begin_drain()
                    if operation == "service.drain"
                    else self.activate()
                )
            except Exception as exc:  # noqa: BLE001 - maintenance faults must preserve the invoking request identity.
                return _exception(exc, request_id)
            return {
                "ok": True,
                "protocol": PROTOCOL,
                "request_id": request_id,
                "data": data,
                "action_digest": None,
            }
        if not self._routine.acquire(timeout=self.admission_timeout):
            return error_envelope(
                "SERVICE_BUSY",
                "Routine admission budget exhausted",
                request_id=request_id,
                retryable=True,
            )
        try:
            with self._condition:
                spec = getattr(self.service, "operations", {}).get(operation)
                if self._draining and (spec is None or spec.mutation):
                    return error_envelope(
                        "AUTHORITY_FENCED",
                        "Service is draining; inspect outcomes and reconnect after upgrade",
                        request_id=request_id,
                    )
                self._active += 1
            try:
                result = self.service.execute(
                    context,
                    Call(
                        operation,
                        arguments,
                        key,
                        expected_task_generation=guards[0],
                        expected_execution_generation=guards[1],
                    ),
                )
                result = dict(result)
                result.update(protocol=PROTOCOL, request_id=request_id)
                encode_frame(result)
                return result
            except Exception as exc:  # noqa: BLE001 - RPC faults need a structured reply before releasing admission.
                return _exception(exc, request_id)
            finally:
                with self._condition:
                    self._active -= 1
                    self._condition.notify_all()
        finally:
            self._routine.release()

    def server_close(self):
        try:
            self.begin_drain()
        finally:
            # Failed lifecycle persistence cannot release ownership while admitted
            # requests or owned effects are still running.
            self._slow.shutdown(wait=True, cancel_futures=False)
            with self._condition:
                sockets = list(self._sockets)
            for connection in sockets:
                with contextlib.suppress(OSError):
                    connection.shutdown(socket.SHUT_RDWR)
            super().server_close()
            with contextlib.suppress(FileNotFoundError):
                if self._path.lstat().st_ino == self._inode:
                    self._path.unlink()


@contextlib.contextmanager
def ownership_lock(path):
    """Hold one private local authority lock independently of transport routing."""
    path = Path(path)
    private_directory(path.parent)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise CoordinationError("NOT_AUTHORIZED", "Unsafe service ownership lock")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CoordinationError(
                "SERVICE_BUSY", "Another service or maintenance operation owns this workspace"
            ) from exc
        yield
    finally:
        os.close(descriptor)


@contextlib.contextmanager
def owned_server(path, service, *, lock_path=None, on_close=None, resource_scope=None, **kwargs):
    """Only the exclusive service owner can remove a stale socket."""
    path = Path(path)
    private_directory(path.parent)
    with contextlib.ExitStack() as scope:
        scope.enter_context(ownership_lock(lock_path if lock_path is not None else path.with_suffix(".lock")))
        if resource_scope is not None:
            scope.enter_context(resource_scope())
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise CoordinationError("NOT_AUTHORIZED", "Unsafe existing socket path")
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.settimeout(0.5)
            try:
                probe.connect(str(path))
            except ConnectionRefusedError:
                path.unlink()
            else:
                raise CoordinationError("SERVICE_BUSY", "A workspace service is already listening")
            finally:
                probe.close()
        server = RPCServer(path, service, **kwargs)
        try:
            yield server
        finally:
            server.server_close()
            if on_close is not None:
                on_close()


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        context = None
        self.request.settimeout(self.server.idle_timeout)
        try:
            frame = read_frame(self.rfile)
            if set(frame) != {
                "protocol",
                "workspace_id",
                "kind",
                "transport",
                "operator",
                "native_context",
            }:
                raise CoordinationError("INVALID_ARGUMENT", "Expected a connection binding frame")
            if type(frame["protocol"]) is not int or frame["protocol"] != PROTOCOL:
                raise CoordinationError("PROTOCOL_MISMATCH", "Reconnect using the installed client")
            if frame["workspace_id"] != self.server.workspace_id:
                raise CoordinationError("WRONG_WORKSPACE", "Socket belongs to another workspace")
            if (
                frame["kind"] != "bind"
                or type(frame["operator"]) is not bool
                or not isinstance(frame["native_context"], dict)
            ):
                raise CoordinationError("INVALID_ARGUMENT", "Invalid connection binding")
            if frame["transport"] not in {"cli", "mcp", "hook", "operator", "worker"}:
                raise CoordinationError("INVALID_ARGUMENT", "Unsupported transport")
            connection_id = secrets.token_hex(16)
            if frame["operator"]:
                if frame["native_context"]:
                    raise CoordinationError(
                        "INVALID_ARGUMENT", "Operator connection cannot select an actor"
                    )
                context = self.server.operator_context(connection_id)
            else:
                context = self.server.bind(
                    frame["native_context"], frame["transport"], connection_id
                )
            self.wfile.write(
                encode_frame(
                    {
                        "ok": True,
                        "protocol": PROTOCOL,
                        "request_id": None,
                        "data": {"bound": True},
                        "action_digest": None,
                        "next_context": {
                            "task_generation": context.task_generation,
                            "execution_generation": context.execution_generation,
                        },
                    }
                )
            )
            while True:
                reply = self.server.dispatch(context, read_frame(self.rfile))
                self.wfile.write(encode_frame(reply))
        except (EOFError, OSError):
            return
        except Exception as exc:  # noqa: BLE001 - report binding/protocol faults while preserving final unbind cleanup.
            with contextlib.suppress(OSError):
                self.wfile.write(encode_frame(_exception(exc)))
        finally:
            if context is not None and self.server.unbind:
                self.server.unbind(context)
