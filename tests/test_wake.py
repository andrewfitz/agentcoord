"""Native delivery intent, authority checks and actual push framing."""
import json
import os
import socket
import tempfile
import uuid
from dataclasses import replace
from pathlib import Path

import anyio
import pytest

from agentcoord import identity, wake, wake_adapters
from agentcoord.application import _recover, build_service
from agentcoord.cli import BY_TOOL, parser
from agentcoord.config import Config, register_workspace
from agentcoord.core import Call, CoordinationError
from agentcoord.mcp import ChannelReceiver


def uid():
    return str(uuid.uuid4())


@pytest.fixture
def runtime(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    service = build_service(register_workspace(repo, state_root=tmp_path/'state'), Config(wake_enabled=True))
    contexts = []
    for harness in ('codex', 'claude', 'grok'):
        proof = identity.process_identity(os.getpid())
        context = identity.bind_native(service.store, {'harness': harness, 'native_session_id': uid(),
                                                      'process_identity': proof})['context']
        with service.store.write() as tx:
            actor = identity.start_execution(tx, context, native_run_id=uid(), process_proof=proof)
        contexts.append(replace(context, execution_generation=actor['execution_generation']))
    return service, contexts


def call(service, context, operation, args, key=None):
    result = service.execute(context, Call(operation, args, key or uid()))
    assert result['ok'], result
    return result['data']


def send(runtime, *, sender=0, recipient=1, key=None, **kwargs):
    service, contexts = runtime
    return call(service, contexts[sender], 'message.send', {'recipients': [contexts[recipient].actor_id],
        'kind': 'handoff', 'subject': 'Parser fixed', 'body': 'Preserve the intentional escape fix.',
        'wake': True, **kwargs}, key)


def test_common_command_and_atomic_retry_receipt(runtime):
    service, contexts = runtime
    parsed = parser().parse_args(['send', '--recipients', contexts[1].actor_id,
        '--kind', 'handoff', '--subject', 'Fix', '--body', 'Evidence', '--wake'])
    assert parsed.wake is True
    assert BY_TOOL['send'].schema()['properties']['wake']['type'] == 'boolean'
    key = uid()
    first = send(runtime, key=key)
    assert send(runtime, key=key) == first
    with service.store.read() as tx:
        assert tx.connection.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 1
        assert tx.connection.execute("SELECT COUNT(*) FROM operations WHERE kind='message.wake'").fetchone()[0] == 1


def test_bad_wake_argument_rolls_back_message(runtime):
    service, contexts = runtime
    result = service.execute(contexts[0], Call('message.send', {'recipients': [contexts[1].actor_id],
        'kind': 'handoff', 'subject': 'Fix', 'body': 'Body', 'wake': 'yes'}, uid()))
    assert not result['ok']
    with service.store.read() as tx:
        assert not tx.connection.execute('SELECT 1 FROM messages').fetchone()


def test_unsent_burst_coalesces_across_senders_and_receipt_is_visible(runtime):
    service, contexts = runtime
    first, second = send(runtime), send(runtime, sender=2)
    oid = first['wake'][0]['operation_id']
    assert second['wake'][0]['operation_id'] == oid and second['wake'][0]['coalesced']
    for ctx in contexts:
        record = service.execute(ctx, Call('wake.get', {'operation_id': oid}))
        assert record['ok']
        assert record['data']['messages'] == 2
    unrelated = identity.bind_native(service.store, {'harness': 'codex', 'native_session_id': uid()})['context']
    result = service.execute(unrelated, Call('wake.get', {'operation_id': oid}))
    assert result['error']['code'] == 'NOT_FOUND'


def test_default_silent_send_and_recipient_opt_out(runtime):
    service, contexts = runtime
    no_signal = send(runtime, wake=False)
    assert 'wake' not in no_signal
    call(service, contexts[1], 'wake.configure', {'enabled': False})
    disabled = send(runtime)
    assert disabled['wake'] == [{'recipient': contexts[1].actor_id, 'delivery': 'disabled'}]
    with service.store.read() as tx:
        assert tx.connection.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 2
        assert not tx.connection.execute('SELECT 1 FROM operations').fetchone()


@pytest.mark.parametrize('change', ['task', 'execution', 'paused', 'completed', 'consent', 'offline'])
def test_dispatch_rechecks_recipient_before_native_effect(runtime, change):
    service, contexts = runtime
    oid = send(runtime)['wake'][0]['operation_id']
    with service.store.write() as tx:
        if change == 'task':
            identity.assign_task(tx, contexts[1], 'new task')
        elif change == 'execution':
            identity.start_execution(tx, contexts[1], native_run_id=uid(), process_proof=identity.process_identity(os.getpid()))
        elif change in ('paused', 'completed'):
            tx.connection.execute('UPDATE actors SET reported_state=? WHERE id=?', (change, contexts[1].actor_id))
        elif change == 'consent':
            wake._configure(service, contexts[1], {'enabled': False}, tx)
        else:
            tx.connection.execute("UPDATE executions SET state='ended' WHERE generation=?", (contexts[1].execution_generation,))
    def forbidden(*args):
        pytest.fail('Invalid recipient reached a native adapter')
    service.adapters['wake_delivery'] = forbidden
    outcome = service.run_operation(oid)
    assert outcome['state'] == 'succeeded'
    assert outcome['result']['delivery'] == 'unavailable'
    assert outcome['effect_started_us'] is None


def test_sender_can_finish_without_cancelling_an_accepted_signal(runtime):
    service, contexts = runtime
    oid = send(runtime)['wake'][0]['operation_id']
    with service.store.write() as tx:
        identity.assign_task(tx, contexts[0], 'next task')
    def deliver(service, operation, actor, channels, effect):
        effect()
        return {'delivery': 'notification_sent'}
    service.adapters['wake_delivery'] = deliver
    assert service.run_operation(oid)['state'] == 'succeeded'


def test_failure_before_and_after_signal_and_restart_never_replay(runtime):
    service, _ = runtime
    calls = []
    def unavailable(service, operation, actor, channels, effect):
        calls.append(operation['id'])
        raise CoordinationError('NOT_AVAILABLE', 'Transport disconnected')
    service.adapters['wake_delivery'] = unavailable
    first = service.run_operation(send(runtime)['wake'][0]['operation_id'])
    assert first['state'] == 'succeeded' and first['effect_started_us'] is None
    def uncertain(service, operation, actor, channels, effect):
        calls.append(operation['id'])
        effect()
        raise CoordinationError('NOT_AVAILABLE', 'Lost native response')
    service.adapters['wake_delivery'] = uncertain
    oid = send(runtime)['wake'][0]['operation_id']
    result = service.run_operation(oid)
    assert result['state'] == 'uncertain' and result['effect_started_us']
    _recover(service)
    assert service.run_operation(oid)['state'] == 'uncertain'
    assert len(calls) == 2


def test_running_signal_does_not_absorb_messages_after_recipient_sync(runtime):
    service, _ = runtime
    oid = send(runtime)['wake'][0]['operation_id']
    with service.store.write() as tx:
        tx.connection.execute("UPDATE operations SET state='running' WHERE id=?", (oid,))
    assert send(runtime)['wake'][0]['operation_id'] != oid


def test_native_delivery_does_not_claim_handling(runtime):
    service, contexts = runtime
    message = send(runtime)
    oid = message['wake'][0]['operation_id']
    service.adapters['wake_delivery'] = lambda *a: {'delivery': 'notification_sent'}
    service.run_operation(oid)
    before = service.execute(contexts[0], Call('wake.get', {'operation_id': oid}))['data']
    assert before['handled_messages'] == 0
    call(service, contexts[1], 'message.consume', {'id': message['id']})
    after = service.execute(contexts[0], Call('wake.get', {'operation_id': oid}))['data']
    assert after['handled_messages'] == 1


def test_channel_registration_uses_exact_native_process_and_binding(runtime):
    service, contexts = runtime
    native = {'harness': 'claude', 'native_session_id': None,
              'process_identity': identity.process_identity(os.getpid())}
    with service.store.read() as tx:
        native['native_session_id'] = tx.connection.execute('SELECT native_session_id FROM actors WHERE id=?',
            (contexts[1].actor_id,)).fetchone()[0]
    channel = identity.bind_native(service.store, native, transport='mcp')['context']
    route = call(service, channel, 'wake.channel', {})
    assert Path(route['path']) == wake.channel_path(service.workspace, channel.connection_id)
    assert len(route['nonce']) == 64
    forbidden = service.execute(contexts[0], Call('wake.channel', {}, uid()))
    assert forbidden['error']['code'] == 'NOT_AUTHORIZED'
    injected = service.execute(channel, Call('wake.channel', {'path': '/tmp/other'}, uid()))
    assert injected['error']['code'] == 'INVALID_ARGUMENT'


@pytest.fixture
def short_socket_dir():
    with tempfile.TemporaryDirectory(prefix='acw-') as path:
        yield Path(path).resolve()


def test_real_private_channel_push_and_id_only_notification(short_socket_dir):
    async def exercise():
        path = short_socket_dir/'push.sock'
        route = {'path': str(path), 'nonce': 'a'*64}
        class Client:
            def call(self, *args, **kwargs):
                return {'ok': True, 'data': route}
        writer, reader = anyio.create_memory_object_stream(4)
        async with anyio.create_task_group() as group:
            receiver = ChannelReceiver(Client(), writer, group)
            await receiver.start()
            assert path.stat().st_mode & 0o777 == 0o600
            oid = uid()
            async def deliver(nonce):
                async with await anyio.connect_unix(path) as conn:
                    await conn.send((json.dumps({'nonce': nonce, 'operation_id': oid})+'\n').encode())
                    try:
                        return await conn.receive(1024)
                    except anyio.EndOfStream:
                        return b''
            assert await deliver('wrong') == b''
            assert await deliver(route['nonce']) == b'{"delivered":true}\n'
            notification = await reader.receive()
            assert notification.message.method == 'notifications/claude/channel'
            assert notification.message.params['meta'] == {'wake_id': oid}
            assert 'Parser fixed' not in notification.message.params['content']
            assert await deliver(route['nonce']) == b'{"delivered":true}\n'
            with pytest.raises(anyio.WouldBlock):
                reader.receive_nowait()
            group.cancel_scope.cancel()
        await receiver.close()
        assert not path.exists()
    anyio.run(exercise)


def test_native_socket_permissions_and_symlinks(short_socket_dir):
    private = short_socket_dir/'private'
    private.mkdir(mode=0o700)
    path = private/'s.sock'
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(path))
        assert wake_adapters.private_socket(path) == path
        link = private/'link'
        link.symlink_to(path)
        with pytest.raises(CoordinationError):
            wake_adapters.private_socket(link)
        private.chmod(0o755)
        path.chmod(0o755)
        with pytest.raises(CoordinationError):
            wake_adapters.private_socket(path)


def test_codex_uses_loaded_native_queue_and_does_not_start_busy(runtime, monkeypatch):
    service, contexts = runtime
    requests = []
    with service.store.read() as tx:
        actor = dict(tx.connection.execute('SELECT * FROM actors WHERE id=?', (contexts[0].actor_id,)).fetchone())
    class RPC:
        def __init__(self, path):
            pass
        def call(self, method, params):
            requests.append((method, params))
            return {'thread/loaded/list': {'data': [actor['native_session_id']]},
                'thread/read': {'thread': {'id': actor['native_session_id'], 'cwd': str(service.workspace.root), 'status': {'type': 'active'}}},
                'thread/queue/add': {'queuedSubmission': {'id': 'native-id'}}}[method]
        def close(self):
            pass
    monkeypatch.setattr(wake_adapters, 'CodexRPC', RPC)
    effect = []
    result = wake_adapters.codex(service, {'id': uid()}, actor, [], lambda: effect.append(True))
    assert result['delivery'] == 'native_queued' and effect == [True]
    assert [r[0] for r in requests] == ['thread/loaded/list', 'thread/read', 'thread/queue/add']
    assert requests[-1][1]['clientUserMessageId'].startswith('agentcoord:')
    assert requests[-1][1]['input'][0]['text'].startswith('Agentcoord has pending')


def test_uncertain_reconciliation_requires_explicit_handling(runtime):
    service, contexts = runtime
    def uncertain(service, operation, actor, channels, effect):
        effect()
        raise CoordinationError('NOT_AVAILABLE', 'Lost native reply')
    service.adapters['wake_delivery'] = uncertain
    message = send(runtime)
    oid = message['wake'][0]['operation_id']
    assert service.run_operation(oid)['state'] == 'uncertain'
    first = call(service, contexts[0], 'wake.reconcile', {'operation_id': oid})
    unresolved = service.run_operation(first['operation_id'])
    assert unresolved['result']['state'] == 'uncertain' and not unresolved['result']['changed']
    call(service, contexts[1], 'message.consume', {'id': message['id']})
    second = call(service, contexts[0], 'wake.reconcile', {'operation_id': oid})
    resolved = service.run_operation(second['operation_id'])
    assert resolved['result']['state'] == 'succeeded' and resolved['result']['changed']
    record = service.execute(contexts[0], Call('wake.get', {'operation_id': oid}))['data']
    assert record['result']['delivery'] == 'handled'


def test_channel_reregisters_only_after_a_real_connection_change(short_socket_dir):
    async def exercise():
        class Client:
            binding_revision = (1, 'execution')
            calls = 0
            def call(self, *args, **kwargs):
                self.calls += 1
                return {'ok': True, 'data': {'path': str(short_socket_dir/f'p{self.calls}.sock'), 'nonce': 'a'*64}}
        client = Client()
        writer, _ = anyio.create_memory_object_stream(4)
        async with anyio.create_task_group() as group:
            receiver = ChannelReceiver(client, writer, group)
            await receiver.start()
            old = receiver.path
            await receiver.start(bound=True)
            assert client.calls == 1
            client.binding_revision = (2, 'execution')
            await receiver.start(bound=True)
            assert client.calls == 2 and not old.exists()
            await receiver.close()
            group.cancel_scope.cancel()
    anyio.run(exercise)


@pytest.mark.parametrize('action', ['complete', 'reassign'])
def test_uncertain_wake_cannot_block_its_sender_lifecycle(runtime, action):
    service, contexts = runtime
    def uncertain(service, operation, actor, channels, effect):
        effect()
        raise CoordinationError('NOT_AVAILABLE', 'Lost wake response')
    service.adapters['wake_delivery'] = uncertain
    oid = send(runtime)['wake'][0]['operation_id']
    assert service.run_operation(oid)['state'] == 'uncertain'
    if action == 'complete':
        result = call(service, contexts[0], 'identity.complete', {'note': 'Owned work finished'})
        assert result['actor']['reported_state'] == 'completed'
    else:
        with service.store.write() as tx:
            actor = identity.assign_task(tx, contexts[0], 'next independent task')
        assert actor['task'] == 'next independent task'


def test_assembled_daemon_delivers_to_real_mcp_receiver(runtime):
    import threading

    from agentcoord.application import make_server
    from agentcoord.transport import Client
    service, contexts = runtime
    with service.store.read() as tx:
        sessions = [tx.connection.execute('SELECT native_session_id FROM actors WHERE id=?', (c.actor_id,)).fetchone()[0]
                    for c in contexts]
    with make_server(service) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with Client(service.workspace.socket_path, {'harness': 'codex', 'native_session_id': sessions[0]},  # noqa: SIM117 — distinct caller lifetimes.
                        workspace_id=service.workspace.id) as sender:
                with Client(service.workspace.socket_path, {'harness': 'claude', 'native_session_id': sessions[1],
                            'process_identity': identity.process_identity(os.getpid())},
                            workspace_id=service.workspace.id, transport='mcp') as recipient:
                    async def exercise():
                        writer, reader = anyio.create_memory_object_stream(4)
                        async with anyio.create_task_group() as group:
                            receiver = ChannelReceiver(recipient, writer, group)
                            await receiver.start()
                            reply = await anyio.to_thread.run_sync(lambda: sender.call('message.send', {
                                'recipients': [contexts[1].actor_id], 'kind': 'handoff',
                                'subject': 'Fix is ready', 'body': 'Preserve peer edits.', 'wake': True}, key=uid()))
                            assert reply['ok'], reply
                            oid = reply['data']['wake'][0]['operation_id']
                            with anyio.fail_after(5):
                                notification = await reader.receive()
                            assert notification.message.params['meta']['wake_id'] == oid
                            assert notification.message.method == 'notifications/claude/channel'
                            await receiver.close()
                            group.cancel_scope.cancel()
                    anyio.run(exercise)
        finally:
            server.shutdown()
            worker.join(5)
            assert not worker.is_alive()


@pytest.mark.parametrize('argv,expected', [
    (['claude'], False),
    (['claude', '--dangerously-load-development-channels', 'server:agentcoord'], True),
    (['claude', '--channels=server:agentcoord'], True),
    (['claude', 'Explain --channels server:agentcoord'], False),
    (['claude', '--', '--channels', 'server:agentcoord'], False),
    (['claude', '--channels', 'server:other'], False),
])
def test_native_claude_channel_activation_is_explicit(monkeypatch, argv, expected):
    monkeypatch.setattr(wake_adapters, 'native_argv', lambda _: argv)
    assert wake_adapters.claude_channel_requested({'harness': 'claude', 'process_identity': {'pid': 1}}) is expected
    assert not wake_adapters.claude_channel_requested({'harness': 'codex', 'process_identity': {'pid': 1}})


def test_native_argument_boundaries_are_read_from_kernel():
    argv = wake_adapters.native_argv(identity.process_identity(os.getpid()))
    assert argv and any('pytest' in item for item in argv)
