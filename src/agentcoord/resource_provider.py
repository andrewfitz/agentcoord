"""Optional runner observations. Allocation and event production belong to the runner."""
from __future__ import annotations

import hashlib
import json
import os
import selectors
import signal
import subprocess
import time
from datetime import datetime
from pathlib import Path

from .core import (
    CoordinationError,
    bounded_text,
    canonical_json,
    integer,
    validate_fields,
)

MAX_OUTPUT = 65536
STATES = {'queued', 'running', 'passed', 'failed', 'cancelled', 'expired', 'error', 'superseded'}
TERMINAL = STATES - {'queued', 'running'}


def validate_provider(provider: dict) -> dict:
    validate_fields(provider, {'argv', 'timeout_seconds'}, {'argv'})
    argv = provider['argv']
    if not isinstance(argv, list) or not 1 <= len(argv) <= 64:
        raise CoordinationError('INVALID_ARGUMENT', 'Provider argv requires 1..64 arguments')
    return {'argv': [bounded_text(arg, 'provider argument', 4096) for arg in argv],
            'timeout_seconds': integer(provider.get('timeout_seconds', 10), 'provider timeout', 1, 30)}


def validate_status(status: dict) -> dict:
    fields = {'request_id', 'resource', 'state', 'holder', 'queue_position', 'start_deadline', 'result'}
    validate_fields(status, fields, fields)
    for name in ('request_id', 'resource', 'state'):
        bounded_text(status[name], name, 256)
    if status['state'] not in STATES:
        raise CoordinationError('INVALID_ARGUMENT', 'Unknown runner state')
    if status['holder'] is not None:
        bounded_text(status['holder'], 'holder', 1024)
    if status['queue_position'] is not None:
        integer(status['queue_position'], 'queue position', 0, 1000000)
    if status['start_deadline'] is not None:
        raw = bounded_text(status['start_deadline'], 'start deadline', 64)
        try:
            parsed = datetime.fromisoformat(raw)
            if parsed.tzinfo is None:
                raise ValueError('missing timezone')
        except ValueError as error:
            raise CoordinationError('INVALID_ARGUMENT', 'Start deadline needs an ISO timestamp with timezone') from error
    if status['result'] is not None and not isinstance(status['result'], dict):
        raise CoordinationError('INVALID_ARGUMENT', 'Runner result must be a JSON object or null')
    if status['state'] in TERMINAL and status['result'] is None:
        raise CoordinationError('INVALID_ARGUMENT', 'Terminal observation requires runner result')
    if status['state'] not in TERMINAL and status['result'] is not None:
        raise CoordinationError('INVALID_ARGUMENT', 'Nonterminal observation cannot claim a terminal result')
    if len(canonical_json(status).encode()) > 6144:
        raise CoordinationError('INVALID_ARGUMENT', 'Runner observation exceeds 6144 bytes')
    return status


def query_status(provider: dict, *, cwd: Path, request_id: str | None = None) -> dict:
    """Run one operator-configured JSON command; never reserve or poll resources."""
    provider = validate_provider(provider)
    if request_id is not None:
        bounded_text(request_id, 'runner request ID', 256)
        if request_id.startswith('-') or any(character.isspace() for character in request_id):
            raise CoordinationError('INVALID_ARGUMENT', 'Runner request ID must be one non-option token')
        provider['argv'].append(request_id)
    deadline = time.monotonic() + provider['timeout_seconds']
    output = bytearray()
    try:
        process = subprocess.Popen(provider['argv'], cwd=cwd, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True)
    except OSError as error:
        raise CoordinationError('PROVIDER_UNAVAILABLE', 'Runner status command could not start') from error
    try:
        with selectors.DefaultSelector() as selector:
            for stream in (process.stdout, process.stderr):
                selector.register(stream, selectors.EVENT_READ)
            total = 0
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CoordinationError('PROVIDER_UNAVAILABLE', 'Runner status command exceeded its deadline')
                for key, _ in selector.select(remaining):
                    chunk = os.read(key.fileobj.fileno(), 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(chunk)
                    if total > MAX_OUTPUT:
                        raise CoordinationError('PROVIDER_UNAVAILABLE', 'Runner status output exceeds 65536 bytes')
                    if key.fileobj is process.stdout:
                        output.extend(chunk)
            try:
                code = process.wait(timeout=max(0.001, deadline - time.monotonic()))
            except subprocess.TimeoutExpired as error:
                raise CoordinationError('PROVIDER_UNAVAILABLE', 'Runner status command exceeded its deadline') from error
            if code:
                raise CoordinationError('PROVIDER_UNAVAILABLE', 'Runner status command failed', details={'exit_code': code})
        try:
            result = json.loads(output)
        except (ValueError, UnicodeError, RecursionError) as error:
            raise CoordinationError('PROVIDER_UNAVAILABLE', 'Runner status command returned invalid JSON') from error
        return validate_status(result)
    finally:
        # Kill descendants too: a pipe inherited by a child must not outlive the deadline.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        process.stdout.close()
        process.stderr.close()


def event_message(provider_name: str, event: dict, recipients: list[str]) -> dict:
    """Convert a supplied runner event into an idempotent ordinary send request.

    The caller submits this through its authenticated send route. No event is
    inferred from elapsed time, and no second runner state is retained here.
    """
    bounded_text(provider_name, 'provider name', 128)
    validate_fields(event, {'event_id', 'kind', 'status'}, {'event_id', 'kind', 'status'})
    event_id = bounded_text(event['event_id'], 'runner event ID', 256)
    kind = event['kind']
    status = validate_status(event['status'])
    expected = {'wait': {'queued'}, 'admitted': {'running'}, 'terminal': TERMINAL}
    if not isinstance(kind, str) or kind not in expected or status['state'] not in expected[kind]:
        raise CoordinationError('INVALID_ARGUMENT', 'Runner event kind and status disagree')
    if not isinstance(recipients, list) or not 1 <= len(recipients) <= 16:
        raise CoordinationError('INVALID_ARGUMENT', 'Runner event needs 1..16 recipients')
    digest = hashlib.sha256(canonical_json([provider_name, event_id]).encode()).hexdigest()
    return {'key': f'resource-event:{digest}', 'params': {
        'recipients': recipients, 'kind': 'resource',
        'subject': f'Runner {kind}: {status["resource"]}',
        'body': canonical_json(status), 'thread': f'resource:{provider_name}:{status["request_id"]}'[:256],
        'context': {'resource_provider': provider_name, 'runner_event_id': event_id, 'runner_event': kind},
    }}
