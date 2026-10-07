"""Harness-specific transports. No terminal keys, offline loads or extra daemons."""
from __future__ import annotations

import json
import os
import selectors
import shlex
import socket
import stat
import subprocess
import time
from pathlib import Path

from .core import CoordinationError, canonical_json
from .wake import channel_path

MAX_NATIVE_FRAME = 2 * 1024 * 1024


def signal_text(operation):
    # Peer content remains untrusted data in the database, never a control prompt.
    return ('Agentcoord has pending directed messages. Run agentcoord sync once; '
            'read and handle relevant messages within your existing authorized scope. '
            'Do not send a courtesy acknowledgment or poll. Wake receipt: ' + operation['id'])


def private_socket(path, *, owned_alias=False):
    path = Path(path).expanduser().absolute()
    if owned_alias and path.is_symlink():
        # Codex publishes a private owned alias to its actual daemon socket.
        # Validate the alias directory before resolving; generic channel routes
        # never accept symlinks or sender-selected addresses.
        info, parent = path.lstat(), path.parent.stat()
        if (info.st_uid != os.getuid() or parent.st_uid != os.getuid() or parent.st_mode & 0o077
                or any(p.is_symlink() for p in path.parents)):
            raise CoordinationError('NOT_AUTHORIZED', 'Native socket alias is not private')
        path = path.resolve(strict=True)
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise CoordinationError('NOT_AVAILABLE', 'Native socket path contains a symlink')
    try:
        info, parent = path.stat(), path.parent.stat()
    except OSError as error:
        raise CoordinationError('NOT_AVAILABLE', 'Native wake endpoint is unavailable') from error
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or parent.st_uid != os.getuid()
            or (info.st_mode & 0o077 and parent.st_mode & 0o077)):
        raise CoordinationError('NOT_AUTHORIZED', 'Native wake endpoint must be private to the local user')
    return path


class CodexRPC:
    def __init__(self, path):
        import websocket

        raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            raw.settimeout(4)
            raw.connect(str(private_socket(path, owned_alias=True)))
            self.ws = websocket.create_connection('ws://localhost/', socket=raw, timeout=4,
                                                   enable_multithread=False, http_no_proxy=['localhost'])
        except BaseException:
            raw.close()
            raise
        self.next_id = 0
        try:
            self.call('initialize', {'clientInfo': {'name': 'agentcoord', 'version': '1'},
                                    'capabilities': {'experimentalApi': True}})
            self.ws.send(canonical_json({'method': 'initialized', 'params': {}}))
        except BaseException:
            self.close()
            raise

    def call(self, method, params):
        self.next_id += 1
        self.ws.send(canonical_json({'id': self.next_id, 'method': method, 'params': params}))
        deadline = time.monotonic() + 8
        for _ in range(2048):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self.ws.settimeout(min(4, remaining))
            frame = self.ws.recv()
            if not frame or len(frame) > MAX_NATIVE_FRAME:
                raise CoordinationError('NOT_AVAILABLE', 'Native RPC frame is invalid or too large')
            reply = json.loads(frame)
            if reply.get('id') == self.next_id:
                if 'error' in reply:
                    raise CoordinationError('NATIVE_REJECTED', 'Native RPC rejected ' + method,
                                            details={'method': method, 'native_code': reply['error'].get('code')})
                return reply['result']
            if 'id' in reply and 'method' in reply:
                # This adapter must never grant tool/permission requests.
                self.ws.send(canonical_json({'id': reply['id'], 'error': {'code': -32601,
                    'message': 'Agentcoord wake transport cannot answer approval requests'}}))
        raise CoordinationError('NOT_AVAILABLE', 'Native RPC response deadline exceeded')

    def close(self):
        self.ws.close(timeout=1)


def codex(service, operation, actor, channels, effect):
    path = service.config.wake_sockets.get('codex') or (Path(os.environ.get('CODEX_HOME') or Path.home()/'.codex')
                                                     / 'app-server-control/app-server-control.sock')
    rpc = CodexRPC(path)
    thread = actor['native_session_id']
    try:
        cursor = None
        for _ in range(16):
            page = rpc.call('thread/loaded/list', {'cursor': cursor, 'limit': 100})
            if thread in page.get('data', []):
                break
            cursor = page.get('nextCursor')
            if not cursor:
                raise CoordinationError('NOT_AVAILABLE', 'Codex thread is not loaded in this native daemon')
        else:
            raise CoordinationError('NOT_AVAILABLE', 'Loaded-thread discovery exceeded its bounded budget')
        snapshot = rpc.call('thread/read', {'threadId': thread, 'includeTurns': False})['thread']
        if snapshot.get('id') != thread or Path(snapshot.get('cwd', '')).resolve() != service.workspace.root.resolve():
            raise CoordinationError('NOT_AUTHORIZED', 'Codex thread identity or workspace does not match')
        status = snapshot.get('status', {}).get('type')
        if status not in {'idle', 'active'}:
            return {'delivery': 'unavailable', 'reason': 'Codex thread is not ready for a native signal'}
        # Do not repeat an effect on recovery. The stable ID is correlation,
        # not a promise that the native queue deduplicates requests.
        effect()
        queued = rpc.call('thread/queue/add', {'threadId': thread,
            'clientUserMessageId': 'agentcoord:' + operation['id'],
            'input': [{'type': 'text', 'text': signal_text(operation)}]})['queuedSubmission']
        result = {'delivery': 'native_queued', 'native_receipt': queued['id']}
        if status == 'idle':
            try:
                turn = rpc.call('thread/queue/start', {'threadId': thread, 'queuedSubmissionId': queued['id']})['turn']
                result.update(delivery='turn_started', turn_id=turn['id'])
            except CoordinationError as error:
                # Accepted queue receipt remains valid if a turn raced us.
                result['start_error'] = error.code
        return result
    finally:
        rpc.close()


def claude(service, operation, actor, channels, effect):
    generation = actor['current_execution_generation']
    matches = []
    for binding in channels:
        route = json.loads(binding['native_evidence_json'] or '{}').get('wake_channel')
        if route and route.get('execution_generation') == generation:
            matches.append((binding, route))
    if len(matches) != 1:
        raise CoordinationError('NOT_AVAILABLE', 'Claude needs one activated Agentcoord MCP Channel')
    binding, route = matches[0]
    path = private_socket(channel_path(service.workspace, binding['id']))
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
        stream.settimeout(4)
        stream.connect(str(path))
        effect()
        stream.sendall((canonical_json({'nonce': route['nonce'], 'operation_id': operation['id']}) + '\n').encode())
        reply = bytearray()
        while len(reply) < 1024 and not reply.endswith(b'\n'):
            chunk = stream.recv(1024 - len(reply))
            if not chunk:
                break
            reply.extend(chunk)
        if json.loads(reply).get('delivered') is not True:
            raise CoordinationError('NOT_AVAILABLE', 'Claude Channel did not confirm notification delivery')
        return {'delivery': 'notification_sent'}


class GrokRPC:
    """Short-lived ACP client attached to an existing shared leader only."""
    def __init__(self, executable, path, cwd):
        self.proc = subprocess.Popen([executable, 'agent', '--leader', '--leader-socket', str(path), 'stdio'],
            cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        self.next_id = 0
        self.buffer = bytearray()
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.proc.stdout, selectors.EVENT_READ)

    def call(self, method, params, *, timeout=10):
        self.next_id += 1
        self.proc.stdin.write((canonical_json({'jsonrpc': '2.0', 'id': self.next_id,
                                              'method': method, 'params': params}) + '\n').encode())
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            while b'\n' in self.buffer:
                line, _, tail = self.buffer.partition(b'\n')
                self.buffer = bytearray(tail)
                item = json.loads(line)
                if item.get('id') == self.next_id and 'method' not in item:
                    if 'error' in item:
                        raise CoordinationError('NATIVE_REJECTED', 'Grok rejected ' + method)
                    return item.get('result', {})
                if 'method' in item and 'id' in item:
                    # Do not bypass or impersonate native UI approval.
                    self.proc.stdin.write((canonical_json({'jsonrpc': '2.0', 'id': item['id'],
                        'error': {'code': -32601, 'message': 'Approval requires the native owner'}}) + '\n').encode())
            if not self.selector.select(max(0, deadline-time.monotonic())):
                break
            chunk = os.read(self.proc.stdout.fileno(), 65536)
            if not chunk:
                break
            self.buffer.extend(chunk)
            if len(self.buffer) > MAX_NATIVE_FRAME:
                raise CoordinationError('NOT_AVAILABLE', 'Grok ACP frame exceeds the native budget')
        raise CoordinationError('NOT_AVAILABLE', 'Grok ACP response deadline exceeded')

    def close(self):
        self.selector.close()
        self.proc.stdin.close()
        try:
            self.proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            # Owned ACP client, never the shared native leader/session.
            self.proc.terminate()
            try:
                self.proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=1)
        self.proc.stdout.close()


def grok(service, operation, actor, channels, effect):
    path = private_socket(service.config.wake_sockets.get('grok') or Path.home()/'.grok/leader.sock')
    process = json.loads(actor['process_identity_json'])
    argv = subprocess.run(['ps', '-p', str(process['pid']), '-o', 'args='],
                          capture_output=True, text=True, timeout=3, check=True).stdout
    words = shlex.split(argv)
    # Shared ACP owners were verified. Current standalone TUIs have no safe
    # attach endpoint; their history must not be loaded in another process.
    if not ('agent' in words and 'stdio' in words and '--leader' in words):
        raise CoordinationError('NOT_AVAILABLE', 'Grok wake currently requires a live shared-leader ACP owner')
    selected = words[words.index('--leader-socket')+1] if '--leader-socket' in words else str(Path.home()/'.grok/leader.sock')
    if Path(selected).expanduser().resolve() != path.resolve():
        raise CoordinationError('NOT_AUTHORIZED', 'Grok owner uses a different native leader')
    if actor['reported_state'] != 'idle':
        return {'delivery': 'deferred_busy', 'reason': 'Grok has no safe busy-session queue'}
    rpc = GrokRPC(service.config.native_executables.get('grok', 'grok'), path, service.workspace.root)
    try:
        rpc.call('initialize', {'protocolVersion': 1, 'clientCapabilities': {'fs': {
            'readTextFile': False, 'writeTextFile': False}, 'terminal': False}})
        rpc.call('authenticate', {'methodId': 'cached_token', '_meta': {'headless': True}})
        # Exact native identity supplied by the authenticated recipient, never sender.
        rpc.call('session/load', {'sessionId': actor['native_session_id'],
                                 'cwd': str(service.workspace.root), 'mcpServers': []})
        effect()
        result = rpc.call('session/prompt', {'sessionId': actor['native_session_id'],
            'prompt': [{'type': 'text', 'text': signal_text(operation)}]}, timeout=15)
        return {'delivery': 'turn_completed', 'stop_reason': result.get('stopReason')}
    finally:
        rpc.close()


def deliver(service, operation, actor, channels, effect):
    adapter = {'codex': codex, 'claude': claude, 'grok': grok}.get(actor['harness'])
    if adapter is None:
        raise CoordinationError('NOT_AVAILABLE', 'Harness has no supported native wake adapter')
    try:
        return adapter(service, operation, actor, channels, effect)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        raise CoordinationError('NOT_AVAILABLE', 'Native wake transport failed',
                                details={'type': type(error).__name__}) from error
