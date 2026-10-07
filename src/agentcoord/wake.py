"""Live native message delivery, using the existing durable external-effect outbox."""
from __future__ import annotations

import hashlib
import json
import secrets
import uuid

from .core import (
    CoordinationError,
    Operation,
    canonical_json,
    identifier,
    validate_fields,
)
from .identity import actor_for_context, process_status

# Deliberately no schema: actors/bindings own configuration, operations own effects.
SCHEMA = ()


def enabled(actor, default=False):
    metadata = json.loads(actor['metadata_json'])
    consent = metadata.get('wake', {})
    if not consent:
        return default and bool(actor['current_execution_generation'])
    return (consent.get('enabled') is True
            and consent.get('execution_generation') == actor['current_execution_generation'])


def channel_path(workspace, binding_id):
    suffix = hashlib.sha256(binding_id.encode()).hexdigest()[:23]
    return workspace.socket_path.parent / ('w' + suffix + '.sock')


def _configure(service, context, args, tx):
    validate_fields(args, {'enabled'}, {'enabled'})
    if type(args['enabled']) is not bool:
        raise CoordinationError('INVALID_ARGUMENT', 'enabled must be boolean')
    if context.identity_mode != 'native_owner':
        raise CoordinationError('NOT_AUTHORIZED', 'Wake consent requires the native session owner')
    actor = service.require_actor(tx, context)
    if actor['child_id'] or not actor['current_execution_generation']:
        raise CoordinationError('NOT_AVAILABLE', 'Wake requires a registered native root execution')
    metadata = dict(actor['metadata'])
    metadata['wake'] = {'enabled': args['enabled'], 'execution_generation': actor['current_execution_generation']}
    tx.connection.execute('UPDATE actors SET metadata_json=?,version=version+1 WHERE id=?',
                          (canonical_json(metadata), actor['id']))
    tx.event('wake', 'configured', actor['id'], actor['id'], {'enabled': args['enabled']})
    return {'enabled': args['enabled'], 'harness': actor['harness'],
            'execution_generation': actor['current_execution_generation']}


def _channel(service, context, args, tx):
    """Private MCP transport registration, never a caller-selected destination."""
    validate_fields(args, set())
    actor = actor_for_context(tx, context)
    if (context.transport != 'mcp' or context.identity_mode not in {'native_owner', 'shared_group'}
            or actor['harness'] != 'claude' or actor['child_id']):
        raise CoordinationError('NOT_AUTHORIZED', 'Only a native Claude MCP owner can register a channel')
    row = tx.connection.execute('SELECT native_evidence_json FROM bindings WHERE id=? AND actor_id=? AND revoked_us IS NULL',
                                (context.connection_id, actor['id'])).fetchone()
    if not row:
        raise CoordinationError('NOT_AUTHORIZED', 'Channel binding is unavailable')
    nonce = secrets.token_hex(32)
    evidence = json.loads(row[0]) if row[0] else {}
    if not evidence.get('process_identity') or canonical_json(evidence['process_identity']) != actor['process_identity_json']:
        raise CoordinationError('NOT_AUTHORIZED', 'Channel requires exact native process evidence')
    evidence['wake_channel'] = {'nonce': nonce, 'execution_generation': actor['current_execution_generation']}
    tx.connection.execute('UPDATE bindings SET native_evidence_json=? WHERE id=?',
                          (canonical_json(evidence), context.connection_id))
    return {'path': str(channel_path(service.workspace, context.connection_id)), 'nonce': nonce}


def enqueue(service, context, tx, message, recipients):
    deliveries = []
    for recipient in recipients:
        actor = tx.connection.execute('SELECT * FROM actors WHERE id=?', (recipient,)).fetchone()
        if not enabled(actor, service.config.wake_enabled):
            deliveries.append({'recipient': recipient, 'delivery': 'disabled'})
            continue
        if actor['archived'] or actor['reported_state'] in {'paused', 'completed'}:
            deliveries.append({'recipient': recipient, 'delivery': 'paused'})
            continue
        if actor['child_id'] or actor['harness'] == 'cursor':
            deliveries.append({'recipient': recipient, 'delivery': 'unsupported'})
            continue
        # Only queued, unsent signals coalesce. Running signals cannot absorb
        # a message that might arrive after the recipient has already synced.
        row = tx.connection.execute("""SELECT * FROM operations WHERE state='queued' AND kind='message.wake'
            AND json_extract(arguments_json,'$.recipient')=?
            AND json_extract(arguments_json,'$.recipient_task')=?
            AND json_extract(arguments_json,'$.recipient_execution')=?
            ORDER BY created_us LIMIT 1""", (recipient, actor['current_task_generation'], actor['current_execution_generation'])).fetchone()
        if row:
            args = json.loads(row['arguments_json'])
            args['through_sequence'] = message['sequence']
            encoded = canonical_json(args)
            tx.connection.execute('UPDATE operations SET arguments_json=?,arguments_sha256=?,updated_us=? WHERE id=?',
                                  (encoded, hashlib.sha256(encoded.encode()).hexdigest(), tx.now_us, row['id']))
            receipt = {'operation_id': row['id'], 'state': 'queued', 'coalesced': True}
        else:
            args = {'recipient': recipient, 'recipient_task': actor['current_task_generation'],
                    'recipient_execution': actor['current_execution_generation'],
                    'ready_us': tx.now_us + 250_000,
                    'first_sequence': message['sequence'], 'through_sequence': message['sequence']}
            receipt = service.enqueue(tx, context, 'message.wake', args,
                                      key='wake:' + message['id'] + ':' + recipient)
        deliveries.append({'recipient': recipient, **receipt})
    return deliveries


def _get(service, context, args, tx):
    validate_fields(args, {'operation_id'}, {'operation_id'})
    oid = identifier(args['operation_id'], 'operation_id')
    row = tx.connection.execute("SELECT * FROM operations WHERE id=? AND kind='message.wake'", (oid,)).fetchone()
    if row is None:
        raise CoordinationError('NOT_FOUND', 'Wake receipt is unavailable')
    target = json.loads(row['arguments_json'])
    participant = tx.connection.execute('''SELECT 1 FROM messages m JOIN recipients r ON r.message_id=m.id
        WHERE r.actor_id=? AND m.sender_id=? AND m.sequence BETWEEN ? AND ? LIMIT 1''',
        (target['recipient'], context.actor_id, target['first_sequence'], target['through_sequence'])).fetchone()
    if not context.operator and context.actor_id != target['recipient'] and not participant:
        raise CoordinationError('NOT_FOUND', 'Wake receipt is unavailable')
    result = service.public_operation_record(row)
    # The recipient's explicit consume is the only handling evidence.
    counts = tx.connection.execute('''SELECT COUNT(*),COUNT(r.handled_us) FROM recipients r
        JOIN messages m ON m.id=r.message_id WHERE r.actor_id=? AND m.sequence BETWEEN ? AND ?''',
        (target['recipient'], target['first_sequence'], target['through_sequence'])).fetchone()
    result['messages'] = counts[0]
    result['handled_messages'] = counts[1]
    return result


def validate_recipient(tx, operation, *, default_enabled=False):
    args = operation['arguments']
    actor = tx.connection.execute('SELECT * FROM actors WHERE id=?', (args['recipient'],)).fetchone()
    if (not actor or actor['archived'] or actor['reported_state'] in {'paused', 'completed'}
            or actor['current_task_generation'] != args['recipient_task']
            or actor['current_execution_generation'] != args['recipient_execution']
            or not enabled(actor, default_enabled)):
        raise CoordinationError('WAKE_INHIBITED', 'Recipient consent, state or generation changed')
    execution = tx.connection.execute('SELECT * FROM executions WHERE generation=? AND actor_id=?',
                                     (args['recipient_execution'], actor['id'])).fetchone()
    if not execution or execution['state'] != 'running' or not execution['process_identity_json']:
        raise CoordinationError('NOT_AVAILABLE', 'Recipient has no verified live native execution')
    if execution['process_identity_json'] != actor['process_identity_json']:
        raise CoordinationError('NOT_AVAILABLE', 'Recipient native process evidence changed')
    return dict(actor), json.loads(execution['process_identity_json'])


def _reconcile(service, context, args, tx):
    record = _get(service, context, args, tx)
    if record['state'] != 'uncertain':
        return {'operation_id': record['id'], 'state': record['state'], 'changed': False}
    return service.enqueue(tx, context, 'wake.reconcile', {'wake_id': record['id']}, key=str(uuid.uuid4()))


def reconcile_operation(service, operation):
    """Resolve handling evidence only; absence of evidence never replays a signal."""
    with service.store.write() as tx:
        row = tx.connection.execute("SELECT * FROM operations WHERE id=? AND kind='message.wake'",
                                    (operation['arguments']['wake_id'],)).fetchone()
        if not row:
            raise CoordinationError('NOT_FOUND', 'Wake receipt is unavailable')
        original = service.operation_record(row)
        args = original['arguments']
        count = tx.connection.execute('''SELECT COUNT(*),COUNT(r.handled_us) FROM recipients r
            JOIN messages m ON m.id=r.message_id WHERE r.actor_id=? AND m.sequence BETWEEN ? AND ?''',
            (args['recipient'], args['first_sequence'], args['through_sequence'])).fetchone()
        if row['state'] == 'uncertain' and count[0] and count[0] == count[1]:
            resolved = service.finish_operation(tx, row['id'], 'succeeded',
                                                result={'delivery': 'handled', 'reconciled': True})
            return {'wake_id': row['id'], 'state': resolved['state'], 'changed': True}
        return {'wake_id': row['id'], 'state': row['state'], 'changed': False,
                'reason': 'Uncertain native delivery requires exact handling evidence; no signal was repeated'}


def execute_operation(service, operation):
    from . import wake_adapters

    try:
        with service.store.read() as tx:
            actor, process = validate_recipient(tx, operation, default_enabled=service.config.wake_enabled)
            channels = [dict(r) for r in tx.connection.execute('SELECT id,native_evidence_json FROM bindings WHERE actor_id=? AND revoked_us IS NULL AND transport=\'mcp\'',
                                                              (actor['id'],))]
        if process_status(process) != 'alive':
            return {'delivery': 'unavailable', 'reason': 'Native process is not verified live'}

        def effect():
            # Native inspection/connect occurs outside transactions; recheck at
            # the last safe point, persist intent before any signal is sent.
            with service.store.write() as tx:
                service.require_effect(tx, operation)
                validate_recipient(tx, operation, default_enabled=service.config.wake_enabled)
                tx.connection.execute('UPDATE operations SET effect_started_us=COALESCE(effect_started_us,?) WHERE id=? AND claim_token=?',
                                      (tx.now_us, operation['id'], operation['claim_token']))

        adapter = service.adapters.get('wake_delivery', wake_adapters.deliver)
        return adapter(service, operation, actor, channels, effect)
    except CoordinationError as error:
        with service.store.read() as tx:
            started = tx.connection.execute('SELECT effect_started_us FROM operations WHERE id=?', (operation['id'],)).fetchone()[0]
        if started is not None:
            raise
        return {'delivery': 'unavailable', 'reason': error.message, 'code': error.code}


def operations():
    return (Operation('wake.configure', _configure, True, True),
            Operation('wake.channel', _channel, True, True),
            Operation('wake.get', _get, actor_required=False),
            Operation('wake.reconcile', _reconcile, True, True))
