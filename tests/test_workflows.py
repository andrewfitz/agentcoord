"""Real workflow boundaries: actor authority, durable notices and scoped hashes."""
from __future__ import annotations

import hashlib
import json
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from agentcoord import decisions, identity, messages, pending, readiness, work
from agentcoord.config import Config
from agentcoord.core import Call, Context, CoordinationError, Service
from agentcoord.store import Store


def uid():
    return str(uuid.uuid4())


def test_intent_is_discoverable_and_readable_after_reconnect(runtime):
    service, bindings = runtime
    published = call(runtime, 1, 'work.intent', {
        'paths': ['src/parser.py'], 'purpose': 'Preserve escaped delimiters',
        'invariants': ['Quoted separators retain their original meaning'],
    })
    intent_id = published['ids'][0]
    with service.store.read() as tx:
        native = dict(tx.connection.execute('SELECT harness,native_session_id FROM actors WHERE id=?',
                                           (context(runtime, 1).actor_id,)).fetchone())
    bindings[1] = identity.bind_native(service.store, native)
    found = call(runtime, 2, 'work.evidence', {'paths': ['src'], 'limit': 1})
    assert [item['id'] for item in found['intents']] == [intent_id]
    detail = call(runtime, 2, 'work.evidence_detail', {'kind': 'intent', 'id': intent_id})
    assert detail['purpose'] == 'Preserve escaped delimiters'
    assert detail['invariants'] == ['Quoted separators retain their original meaning']
    assert detail['path'] == 'src/parser.py'
    assert not call(runtime, 2, 'work.evidence', {'paths': ['src/parse']})['intents']


def test_operator_intent_search_scope_and_detail_keep_authored_meaning(tmp_path):
    from agentcoord.application import build_service
    service = build_service(SimpleNamespace(id=uid(), root=tmp_path,
                                            state_dir=tmp_path / 'state',
                                            database_path=tmp_path / 'state/runtime.sqlite3'))
    native = identity.bind_native(service.store, {'harness': 'codex', 'native_session_id': uid(),
                                                 'task': 'parser-repair'})['context']
    published = service.execute(native, Call('work.intent', {
        'paths': ['src/parser.py'], 'purpose': 'Keep escaped token behavior',
        'invariants': ['Invariant needle remains searchable'],
    }, uid()))
    assert published['ok'], published
    reader = Context(service.workspace.id, None, operator=True, transport='operator')
    query = {'query': 'Invariant needle', 'filters': {'task': 'parser-repair', 'path': 'src'}}
    history = service.execute(reader, Call('operator.history', query))
    assert history['ok'], history
    assert [item['record_id'] for item in history['data']['items']] == published['data']['ids']
    sequence = history['data']['items'][0]['sequence']
    detail = service.execute(reader, Call('operator.history', {'sequence': sequence}))
    assert detail['ok'], detail
    record = detail['data']['items'][0]['record']
    assert record['purpose'] == 'Keep escaped token behavior'
    assert record['invariants'] == ['Invariant needle remains searchable']
    unrelated = service.execute(reader, Call('operator.history', {
        'query': 'Invariant needle', 'filters': {'path': 'src/parse'},
    }))
    assert unrelated['ok'] and not unrelated['data']['items'], unrelated


@pytest.fixture
def runtime(tmp_path):
    store = Store(tmp_path / 'state' / 'runtime.sqlite3', uid())
    store.initialize((identity.SCHEMA, work.SCHEMA, messages.SCHEMA,
                      decisions.SCHEMA, readiness.SCHEMA, pending.SCHEMA))
    service = Service(store, SimpleNamespace(id=store.workspace_id, root=tmp_path), Config(),
        (*identity.operations(), *work.operations(), *messages.operations(), *decisions.operations(), *readiness.operations()),
        {'slow_handlers': readiness.slow_handlers()})
    bindings = [identity.bind_native(store, {'harness': 'codex', 'native_session_id': uid(),
                                           'task': name}) for name in ('requester', 'supplier', 'observer')]
    return service, bindings


def call(runtime, actor, name, arguments, *, key=None, ok=True):
    service, bindings = runtime
    context = identity.context_from_token(service.store, bindings[actor]['token'])
    operation = service.operations[name]
    response = service.execute(context, Call(name, arguments, key or uid() if operation.keyed else None))
    assert response['ok'] is ok, response
    return response['data'] if ok else response['error']


def context(runtime, actor):
    service, bindings = runtime
    return identity.context_from_token(service.store, bindings[actor]['token'])


def test_activity_discovery_defaults_to_current_scopes_with_explicit_history(runtime):
    rows = []
    for number in range(3):
        for actor in (0, 1):
            rows.append(call(runtime, actor, 'work.activity', {
                'paths': ['src/parser'], 'state': 'working', 'note': f'Repair step {number}',
            })['activity'])
    current = call(runtime, 2, 'work.activities', {'paths': ['src'], 'limit': 1})
    assert [row['id'] for row in current['activities']] == [rows[-2]['id']]
    second = call(runtime, 2, 'work.activities', {'paths': ['src'], 'after': current['after']})
    assert [row['id'] for row in second['activities']] == [rows[-1]['id']]
    assert second['after'] is None
    history = call(runtime, 2, 'work.activities', {'paths': ['src'], 'current': False})
    assert [row['id'] for row in history['activities']] == [row['id'] for row in rows]
    assert not call(runtime, 2, 'work.activities', {'paths': ['src/par']})['activities']


def test_completed_fix_remains_discoverable_after_owner_changes_task(runtime):
    first = call(runtime, 1, 'work.activity', {'paths': ['src/parser.py'], 'state': 'completed',
        'note': 'Keep quoted commas intact', 'evidence': {'test': 'parser-regression'}})['activity']
    call(runtime, 1, 'identity.checkpoint', {'state': 'working', 'note': 'Resume for the followup'})
    second = call(runtime, 1, 'work.activity', {'task': 'parser-followup', 'paths': ['src/parser.py'],
        'state': 'completed', 'note': 'Preserve escapes too'})['activity']
    call(runtime, 1, 'identity.checkpoint', {'state': 'working', 'note': 'Start the next task'})
    call(runtime, 1, 'work.activity', {'task': 'new-task', 'paths': ['other'], 'note': 'Unrelated work'})
    found = call(runtime, 2, 'work.evidence', {'paths': ['src/parser.py'], 'limit': 1})
    assert not found['activities']
    assert [row['id'] for row in found['outcomes']] == [second['id']]
    older = call(runtime, 2, 'work.evidence', {'paths': ['src/parser.py'], 'limit': 1,
        'outcomes_before': found['outcomes_before']})
    assert [row['id'] for row in older['outcomes']] == [first['id']]
    assert older['outcomes_before'] is None
    detail = call(runtime, 2, 'work.evidence_detail', {'kind': 'activity', 'id': first['id']})
    assert detail['evidence'] == {'test': 'parser-regression'}
    assert not call(runtime, 2, 'work.evidence', {'paths': ['src/parse']})['outcomes']


def test_archived_and_old_assignment_scopes_are_history_not_current(runtime):
    row = call(runtime, 1, 'work.activity', {'paths': ['src/parser.py'], 'note': 'Parser work'})['activity']
    service, _ = runtime
    with service.store.write() as tx:
        tx.connection.execute('UPDATE actors SET archived=1 WHERE id=?', (context(runtime, 1).actor_id,))
    assert not call(runtime, 2, 'work.activities', {'paths': ['src']})['activities']
    assert call(runtime, 2, 'work.activities', {'paths': ['src'], 'current': False})['activities'][0]['id'] == row['id']
    with service.store.write() as tx:
        tx.connection.execute('UPDATE actors SET archived=0 WHERE id=?', (row['actor_id'],))
        identity.assign_task(tx, context(runtime, 1), 'different assignment')
    assert not call(runtime, 2, 'work.activities', {'paths': ['src']})['activities']


def test_scope_context_is_advisory_bounded_and_only_on_new_scope(runtime):
    peer = call(runtime, 1, 'work.activity', {'paths': ['src'], 'note': 'Own the schema'})['activity']
    start = call(runtime, 0, 'work.activity', {'paths': ['src/parser.py'], 'note': 'Repair quoting'})
    assert [row['id'] for row in start['scope_context']['items']] == [peer['id']]
    assert not start['scope_context']['more']
    assert 'scope_context' not in call(runtime, 0, 'work.activity', {'paths': ['src/parser.py'], 'note': 'Added regression'})
    assert 'scope_context' not in call(runtime, 0, 'work.activity', {'state': 'completed'})
    with runtime[0].store.read() as tx:
        assert tx.connection.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 0


def test_repeated_intent_keeps_timestamp_and_event_count(runtime):
    args = {'paths': ['src/parser.py'], 'purpose': 'Keep quoting', 'invariants': ['Escapes survive']}
    first = call(runtime, 1, 'work.intent', args)
    with runtime[0].store.read() as tx:
        before = tx.connection.execute('SELECT COUNT(*) FROM events').fetchone()[0]
        updated = tx.connection.execute('SELECT updated_us FROM intents WHERE id=?', (first['ids'][0],)).fetchone()[0]
    second = call(runtime, 1, 'work.intent', args)
    assert second['ids'] == first['ids'] and not second['recorded']
    with runtime[0].store.read() as tx:
        assert tx.connection.execute('SELECT COUNT(*) FROM events').fetchone()[0] == before
        assert tx.connection.execute('SELECT updated_us FROM intents WHERE id=?', (first['ids'][0],)).fetchone()[0] == updated
    assert call(runtime, 1, 'work.intent', {**args, 'state': 'completed'})['recorded']


def test_ambiguous_hook_repetition_does_not_invent_execution_proof(runtime):
    service, _ = runtime
    owner = context(runtime, 1)
    with service.store.write() as tx:
        before = identity._actor(tx, owner.actor_id)
        for _ in range(3):
            result = identity.apply_lifecycle_event(tx, owner, state='completed', execution_generation=None, event='stop')
            assert result == {'applied': False, 'reason': 'ambiguous_generation'}
        assert tx.connection.execute("SELECT COUNT(*) FROM events WHERE actor_id=? AND kind='ambiguous_event'", (owner.actor_id,)).fetchone()[0] == 1
        assert identity._actor(tx, owner.actor_id) == before
        identity.apply_lifecycle_event(tx, owner, state='completed', execution_generation=None, event='child_stop')
        assert tx.connection.execute("SELECT COUNT(*) FROM events WHERE actor_id=? AND kind='ambiguous_event'", (owner.actor_id,)).fetchone()[0] == 2


@pytest.mark.parametrize('cursor', [0, True, 'bad', -1])
def test_outcome_cursor_validation(runtime, cursor):
    assert call(runtime, 2, 'work.evidence', {'paths': ['src'], 'outcomes_before': cursor}, ok=False)['code'] == 'INVALID_ARGUMENT'


def test_activity_previews_and_detail_pages_preserve_exact_content(runtime):
    paths = [f'src/{number:03}/' + 'p' * 400 for number in range(90)]
    note = '🧩' * 450
    evidence = {'receipt': 'e' * 4000}
    row = call(runtime, 1, 'work.activity', {'paths': paths, 'note': note, 'evidence': evidence})['activity']
    assert row['note_excerpt'] and row['note_bytes'] == len(note.encode())
    assert row['evidence_omitted'] and 'evidence' not in row
    assert row['path_count'] == len(paths) and row['paths_more']
    recovered, after = [], None
    with runtime[0].store.read() as tx:
        before = tx.connection.execute('SELECT COUNT(*) FROM events').fetchone()[0]
    while True:
        args = {'kind': 'activity', 'id': row['id'], 'limit': 100}
        if after:
            args['paths_after'] = after
        detail = call(runtime, 2, 'work.evidence_detail', args)
        assert detail['note'] == note and detail['evidence'] == evidence
        recovered.extend(detail['paths'])
        after = detail['paths_after']
        if not after:
            break
    assert recovered == paths
    with runtime[0].store.read() as tx:
        assert tx.connection.execute('SELECT COUNT(*) FROM events').fetchone()[0] == before


def test_activity_byte_budget_has_a_lossless_continuation(runtime):
    service, _ = runtime
    expected = []
    for number in range(16):
        owner = identity.bind_native(service.store, {'harness': 'claude', 'native_session_id': uid(), 'task': 'scope-budget'})['context']
        response = service.execute(owner, Call('work.activity', {
            'paths': [f'src/{index}/' + 'p' * 600 for index in range(8)], 'note': '🧩' * 450,
        }, uid()))
        assert response['ok'], response
        expected.append(response['data']['activity']['id'])
    seen, after, pages = [], 0, 0
    while True:
        result = call(runtime, 2, 'work.activities', {'paths': ['src'], 'limit': 100, 'after': after})
        assert len(json.dumps(result, ensure_ascii=False).encode()) < 42000
        seen.extend(row['id'] for row in result['activities'])
        pages += 1
        after = result['after']
        if after is None:
            break
    assert seen == expected and pages > 1


def test_current_discovery_keeps_shared_reports_and_canonical_latest(runtime):
    service, _ = runtime
    canonical = call(runtime, 0, 'work.activity', {'paths': ['src/api'], 'note': 'Own API repair'})['activity']
    shared_context = identity.bind_native(service.store, {
        'harness': 'codex', 'native_session_id': uid(), 'task': 'shared',
    }, transport='mcp')['context']
    with service.store.write() as tx:
        for note in ('Parser investigation', 'Parser handoff'):
            shared = work.activity(service, shared_context, {
                'task': 'parser-report', 'paths': ['src/parser'], 'note': note,
            }, tx)['activity']
    current = call(runtime, 2, 'work.activities', {})
    assert {row['id'] for row in current['activities']} == {canonical['id'], shared['id']}


def request(runtime, recipient=1, **extra):
    return call(runtime, 0, 'decision.request', {'recipient': context(runtime, recipient).actor_id,
        'subject': 'Which migration?', 'body': 'Choose the reviewed input', 'paths': ['src/item'], **extra})


def run(runtime, queued):
    result = runtime[0].run_operation(queued['operation_id'])
    assert result['state'] == 'succeeded', result
    return result['result']


def publish(runtime, *, paths=('a', 'b'), actor=1, **extra):
    return run(runtime, call(runtime, actor, 'readiness.publish', {
        'artifact': 'candidate', 'paths': list(paths), 'evidence': {'check': 'passing'}, **extra}))


def subscription(runtime):
    return call(runtime, 0, 'readiness.subscribe', {'producer': context(runtime, 1).actor_id,
        'artifact': 'candidate', 'paths': ['a', 'b'], 'next_action': 'Run integration checks'})


def test_message_visibility_receipts_and_retry_payload(runtime):
    key = uid()
    args = {'recipients': [context(runtime, 1).actor_id], 'kind': 'note', 'subject': 'Review',
            'body': 'α🙂complete', 'requested_ack': True, 'paths': ['src/item']}
    result = call(runtime, 0, 'message.send', args, key=key)
    assert call(runtime, 0, 'message.send', args, key=key)['id'] == result['id']
    assert call(runtime, 0, 'message.send', {**args, 'body': 'changed'}, key=key, ok=False)['code'] == 'IDEMPOTENCY_CONFLICT'
    assert call(runtime, 2, 'message.get', {'id': result['id']}, ok=False)['code'] == 'NOT_FOUND'
    assert call(runtime, 0, 'message.consume', {'id': result['id']}, ok=False)['code'] == 'NOT_AUTHORIZED'
    first = call(runtime, 1, 'message.get', {'id': result['id'], 'limit': 3})
    assert first['body'] == 'α' and first['next_offset'] == 2
    assert call(runtime, 1, 'message.get', {'id': result['id'], 'offset': 3}, ok=False)['code'] == 'INVALID_ARGUMENT'
    call(runtime, 1, 'message.ack', {'id': result['id']})
    with runtime[0].store.read() as tx:
        row = tx.connection.execute('SELECT * FROM recipients WHERE message_id=?', (result['id'],)).fetchone()
        assert row['acknowledged_us'] and row['handled_us'] is None and row['presented_us'] is None
    call(runtime, 1, 'message.consume', {'id': result['id']})
    assert call(runtime, 1, 'message.inbox', {})['pending_counts']['messages'] == 0


def test_imported_large_body_chunks_and_history_highwater_do_not_hide_pending(runtime):
    service, _ = runtime
    sender, recipient = context(runtime, 0), context(runtime, 1)
    raw = ('🙂'*25000 + 'retained tail').encode()
    imported_id = uid()
    with service.store.write() as tx:
        sequence = tx.event('messages', 'import', imported_id, sender.actor_id, {})
        tx.connection.execute('INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', (
            imported_id, sender.actor_id, sender.task_generation, '', 'note', 'Archive', raw,
            hashlib.sha256(raw).hexdigest(), len(raw), '{}', sequence, tx.now_us))
        tx.connection.execute("INSERT INTO recipients VALUES (?,?,?,'to',NULL,NULL,NULL,NULL)",
                              (imported_id, recipient.actor_id, recipient.task_generation))
    parts, offset = [], 0
    while True:
        chunk = call(runtime, 1, 'message.get', {'id': imported_id, 'offset': offset, 'limit': 20000})
        parts.append(chunk['body'])
        if chunk['next_offset'] is None:
            break
        offset = chunk['next_offset']
    assert ''.join(parts).encode() == raw
    for number in range(7):
        call(runtime, 0, 'message.send', {'recipients': [recipient.actor_id], 'kind': 'note',
             'subject': f'new-{number}', 'body': 'new', 'paths': ['unrelated']})
    page = call(runtime, 1, 'message.history', {'limit': 3})
    highwater = page['high_water']
    call(runtime, 0, 'message.send', {'recipients': [recipient.actor_id], 'kind': 'note', 'subject': 'later', 'body': 'later'})
    ids = [m['id'] for m in page['messages']]
    while page['cursor']:
        page = call(runtime, 1, 'message.history', {'limit': 3, 'cursor': page['cursor']})
        assert page['high_water'] == highwater
        ids.extend(m['id'] for m in page['messages'])
    assert len(ids) == len(set(ids)) == 8
    with service.store.read() as tx:
        assert messages.count_pending(tx, recipient)['messages'] == 9
        assert messages.select_unhandled(tx, recipient, limit=1)[0]['id'] == imported_id


def test_decision_notification_failure_rolls_back_domain_and_retry_receipt(runtime, monkeypatch):
    service, _ = runtime
    original = messages.append_message
    def fail_after_insert(*args, **kwargs):
        original(*args, **kwargs)
        raise CoordinationError('OPERATION_FAILED', 'Injected notification failure')
    monkeypatch.setattr(messages, 'append_message', fail_after_insert)
    error = call(runtime, 0, 'decision.request', {'recipient': context(runtime, 1).actor_id,
        'subject': 'atomic', 'body': 'request', 'paths': ['src']}, ok=False)
    assert error['code'] == 'OPERATION_FAILED'
    with service.store.read() as tx:
        for table in ('decisions', 'messages', 'recipients', 'idempotency'):
            assert tx.connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0


def test_stale_answer_requires_exact_reconciliation_and_follower_has_no_authority(runtime):
    decision = request(runtime)
    call(runtime, 2, 'decision.follow', {'id': decision['id']})
    assert call(runtime, 2, 'decision.resolve', {'id': decision['id'], 'state': 'answered', 'response': 'pretend'}, ok=False)['code'] == 'NOT_AUTHORIZED'
    call(runtime, 1, 'work.activity', {'task': 'reassigned supplier', 'paths': ['src'], 'note': 'Different task'})
    proposal = call(runtime, 1, 'decision.resolve', {'id': decision['id'], 'state': 'answered', 'response': 'recorded answer'})
    assert proposal['state'] == 'reconciliation_required'
    assert call(runtime, 0, 'decision.get', {'id': decision['id']})['state'] == 'open'
    with runtime[0].store.read() as tx:
        updates = decisions.select_actions(tx, context(runtime, 2))
        assert len(updates) == 1 and updates[0]['kind'] == 'decision_follow'
    resolved = call(runtime, 0, 'decision.reconcile', {'id': decision['id'], 'proposal_id': proposal['proposal']['id'], 'accept': True})
    assert resolved['state'] == 'answered' and resolved['resolved_by'] == context(runtime, 1).actor_id
    assert call(runtime, 2, 'decision.get', {'id': decision['id']})['response'] == 'recorded answer'


def test_proposal_rejection_keeps_open_and_generation_change_blocks_accept(runtime):
    decision = request(runtime)
    call(runtime, 1, 'work.activity', {'task': 'new task', 'note': 'Reassigned', 'paths': ['src']})
    proposal = call(runtime, 1, 'decision.resolve', {'id': decision['id'], 'state': 'declined', 'response': 'No input'})['proposal']
    call(runtime, 1, 'work.activity', {'task': 'newer task', 'note': 'Reassigned again', 'paths': ['src']})
    assert call(runtime, 0, 'decision.reconcile', {'id': decision['id'], 'proposal_id': proposal['id'], 'accept': True}, ok=False)['code'] == 'STALE_GENERATION'
    assert call(runtime, 0, 'decision.reconcile', {'id': decision['id'], 'proposal_id': proposal['id'], 'accept': False})['state'] == 'open'


def test_requester_external_settlement_requires_actual_authorized_supplier(runtime):
    decision = request(runtime)
    args = {'id': decision['id'], 'state': 'answered', 'response': 'Actual external answer'}
    assert call(runtime, 0, 'decision.resolve', args, ok=False)['code'] == 'NOT_AUTHORIZED'
    assert call(runtime, 0, 'decision.resolve', {**args, 'external_settlement': {
        'answered_by': context(runtime, 2).actor_id, 'evidence': 'discussion'}}, ok=False)['code'] == 'NOT_AUTHORIZED'
    resolved = call(runtime, 0, 'decision.resolve', {**args, 'external_settlement': {
        'answered_by': context(runtime, 1).actor_id, 'evidence': 'Authenticated recipient conversation'}})
    assert resolved['state'] == 'answered'


def test_parent_route_once_and_stale_recipient_diagnostic(runtime):
    service, bindings = runtime
    parent = context(runtime, 2)
    with service.store.read() as tx:
        native_session = tx.connection.execute('SELECT native_session_id FROM actors WHERE id=?', (parent.actor_id,)).fetchone()[0]
    bindings.append(identity.bind_native(service.store, {'harness': 'codex', 'native_session_id': native_session,
        'child_id': uid(), 'parent_id': parent.actor_id, 'source': 'native_child_start', 'task': 'child'}))
    decision = request(runtime, recipient=3)
    with service.store.write() as tx:
        tx.connection.execute('UPDATE decisions SET deadline_us=0 WHERE id=?', (decision['id'],))
        routed = decisions.route_due(tx, context(runtime, 0))
        assert routed[0]['routing_state'] == 'escalated'
    with service.store.write() as tx:
        assert decisions.route_due(tx, context(runtime, 0)) == []
    assert call(runtime, 2, 'decision.resolve', {'id': decision['id'], 'state': 'answered', 'response': 'Parent answer'})['state'] == 'answered'
    stale = request(runtime)
    call(runtime, 1, 'work.activity', {'task': 'changed', 'note': 'new work', 'paths': ['src']})
    with service.store.write() as tx:
        routed = decisions.route_due(tx, context(runtime, 0))
        assert routed == [{'id': stale['id'], 'routing_state': 'diagnostic', 'reason': 'stale_recipient'}]


def test_deferral_bounded_by_deadline_and_closed_notices_remain_pending(runtime):
    service, _ = runtime
    decision = request(runtime)
    assert call(runtime, 1, 'decision.defer', {'id': decision['id'], 'until_us': decision['deadline_us']+1, 'note': 'later'}, ok=False)['code'] == 'INVALID_ARGUMENT'
    call(runtime, 1, 'decision.defer', {'id': decision['id'], 'until_us': decision['deadline_us']-1, 'note': 'Preparing'})
    call(runtime, 1, 'decision.resolve', {'id': decision['id'], 'state': 'answered', 'response': 'ready'})
    actor = context(runtime, 0)
    with service.store.read() as tx:
        actions = decisions.select_actions(tx, actor)
        assert len(actions) == 1 and actions[0]['state'] == 'answered' and actions[0]['notice_count'] == 2
        assert messages.count_pending(tx, actor, exclude_domain_notifications=True)['messages'] == 0
    for message_id in actions[0]['message_ids']:
        call(runtime, 0, 'message.consume', {'id': message_id})
    with service.store.read() as tx:
        assert sum(decisions.count_pending(tx, actor).values()) == 0


def test_scoped_discovery_and_new_only_filter_before_limit(runtime):
    service, _ = runtime
    first = request(runtime)
    second = request(runtime)
    actor = context(runtime, 1)
    with service.store.write() as tx:
        tx.connection.execute('INSERT INTO action_presentations VALUES (?,?,?,?,?)', (actor.actor_id, 'decision', first['id'], first['version'], tx.now_us))
    with service.store.read() as tx:
        rows = decisions.select_actions(tx, actor, limit=1, new_only=True, filters={'path': 'src'})
        assert [r['id'] for r in rows] == [second['id']]
        assert sum(decisions.count_pending(tx, actor, new_only=True).values()) == 1
    assert not call(runtime, 2, 'decision.find', {'paths': ['src/it']})['decisions']
    assert len(call(runtime, 2, 'decision.find', {'paths': ['src'], 'limit': 1})['decisions']) == 1


def test_activity_dedup_completion_and_shared_mcp_preserve_parent(runtime):
    service, _bindings = runtime
    args = {'task': 'requester', 'paths': ['src/item'], 'note': 'Implement domain', 'evidence': {'check': 'unit'}}
    assert call(runtime, 0, 'work.activity', args)['recorded']
    assert not call(runtime, 0, 'work.activity', args)['recorded']
    completed = call(runtime, 0, 'work.activity', {'state': 'completed'})
    assert completed['activity']['paths'] == ['src/item'] and completed['activity']['note'] == 'Implement domain'
    shared = identity.bind_native(service.store, {'harness': 'codex', 'native_session_id':
        'shared-test', 'task': 'parent task'}, transport='mcp')['context']
    with service.store.read() as tx:
        original = dict(tx.connection.execute('SELECT * FROM actors WHERE id=?', (shared.actor_id,)).fetchone())
    result = service.execute(shared, Call('work.activity', {'task': 'Reported sibling', 'paths': ['other'],
        'note': 'Sibling complete', 'state': 'completed'}, uid()))
    assert result['ok'], result
    with service.store.read() as tx:
        current = dict(tx.connection.execute('SELECT * FROM actors WHERE id=?', (shared.actor_id,)).fetchone())
        assert current['current_task_generation'] == original['current_task_generation']
        assert current['reported_state'] == original['reported_state']
        assert tx.connection.execute('SELECT COUNT(*) FROM current_activity WHERE actor_id=?', (shared.actor_id,)).fetchone()[0] == 0


def test_readiness_narrowing_supersedes_and_withdrawal_remains_unaccepted(runtime, tmp_path):
    (tmp_path/'a').write_text('a'); (tmp_path/'b').write_text('b')
    subscription(runtime)
    wide = publish(runtime)
    update = call(runtime, 0, 'readiness.updates', {})['updates'][0]
    assert run(runtime, call(runtime, 0, 'readiness.accept', {'id': update['id']}))['accepted']
    narrow = publish(runtime, paths=('a',))
    assert narrow['version'] == wide['version'] + 1
    updates = call(runtime, 0, 'readiness.updates', {})['updates']
    assert len(updates) == 1 and updates[0]['status'] == 'incomplete_scope'
    assert call(runtime, 0, 'readiness.accept', {'id': updates[0]['id']}, ok=False)['code'] == 'STALE_VERSION'
    call(runtime, 1, 'readiness.withdraw', {'artifact': 'candidate', 'reason': 'Input changed'})
    updates = call(runtime, 0, 'readiness.updates', {})['updates']
    assert len(updates) == 1 and updates[0]['status'] == 'withdrawn'
    assert call(runtime, 0, 'readiness.accept', {'id': updates[0]['id']}, ok=False)['code'] == 'STALE_VERSION'


def test_readiness_accept_rechecks_hashes_and_late_subscription(runtime, tmp_path):
    (tmp_path/'a').write_text('a'); (tmp_path/'b').write_text('b')
    receipt = publish(runtime)
    subscription(runtime)
    update = call(runtime, 0, 'readiness.updates', {})['updates'][0]
    queued = call(runtime, 0, 'readiness.accept', {'id': update['id']})
    (tmp_path/'b').write_text('changed')
    failed = runtime[0].run_operation(queued['operation_id'])
    assert failed['state'] == 'failed' and failed['error']['code'] == 'STALE_VERSION'
    (tmp_path/'b').write_text('b')
    accepted = run(runtime, call(runtime, 0, 'readiness.accept', {'id': update['id']}))
    assert accepted['accepted'] and accepted['receipt_id'] == receipt['id']
    assert call(runtime, 0, 'readiness.updates', {})['updates'] == []


def test_hashes_outside_writer_and_stale_prepared_generation_cannot_publish(runtime, tmp_path, monkeypatch):
    (tmp_path/'a').write_text('a'); (tmp_path/'b').write_text('b')
    queued = call(runtime, 1, 'readiness.publish', {'artifact': 'candidate', 'paths': ['a','b'], 'evidence': 'check'})
    original = readiness.prepare_hashes
    def prepare_then_reassign(*args, **kwargs):
        hashes = original(*args, **kwargs)
        # This must acquire the same authority's writer lane while hashing is outside it.
        actor = context(runtime, 1)
        with runtime[0].store.write() as tx:
            generation = uid()
            tx.connection.execute('INSERT INTO assignments VALUES (?,?,?,?)', (generation, actor.actor_id, 'reassigned', tx.now_us))
            tx.connection.execute('UPDATE actors SET current_task_generation=? WHERE id=?', (generation, actor.actor_id))
        return hashes
    monkeypatch.setattr(readiness, 'prepare_hashes', prepare_then_reassign)
    result = runtime[0].run_operation(queued['operation_id'])
    assert result['state'] == 'failed' and result['error']['code'] == 'STALE_GENERATION'
    with runtime[0].store.read() as tx:
        assert tx.connection.execute('SELECT COUNT(*) FROM receipts').fetchone()[0] == 0


def test_handoff_atomic_receipt_notice_and_pending_coalescing(runtime, tmp_path, monkeypatch):
    (tmp_path/'a').write_text('a'); (tmp_path/'b').write_text('b')
    subscription(runtime)
    args = {'artifact': 'candidate', 'paths': ['a','b'], 'evidence': 'passing',
        'recipients': [context(runtime, 0).actor_id], 'subject': 'Handoff', 'body': 'Use these inputs'}
    queued = call(runtime, 1, 'readiness.handoff', args)
    result = run(runtime, queued)
    with runtime[0].store.read() as tx:
        actions = readiness.select_actions(tx, context(runtime, 0))
        assert len(actions) == 1 and actions[0]['message_ids'] == [result['message']['id']]
        assert sum(readiness.count_pending(tx, context(runtime, 0)).values()) == 1
        assert messages.count_pending(tx, context(runtime, 0), exclude_domain_notifications=True)['messages'] == 0
    original = messages.append_message
    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise CoordinationError('OPERATION_FAILED', 'notice failed')
    monkeypatch.setattr(messages, 'append_message', fail)
    (tmp_path/'a').write_text('different')
    failed = runtime[0].run_operation(call(runtime, 1, 'readiness.handoff', args)['operation_id'])
    assert failed['state'] == 'failed'
    with runtime[0].store.read() as tx:
        assert tx.connection.execute('SELECT COUNT(*) FROM receipts').fetchone()[0] == 1
        assert tx.connection.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 1
        assert tx.connection.execute('SELECT COUNT(*) FROM dependency_updates').fetchone()[0] == 1


def test_readiness_symlink_and_cancelled_subscription_fail_explicitly(runtime, tmp_path):
    (tmp_path/'a').write_text('a'); (tmp_path/'b').symlink_to(tmp_path/'a')
    queued = call(runtime, 1, 'readiness.publish', {'artifact': 'candidate','paths': ['b'],'evidence': 'check'})
    assert runtime[0].run_operation(queued['operation_id'])['state'] == 'failed'
    (tmp_path/'b').unlink(); (tmp_path/'b').write_text('b')
    sub = subscription(runtime)
    publish(runtime)
    update = call(runtime, 0, 'readiness.updates', {})['updates'][0]
    call(runtime, 0, 'readiness.cancel', {'id': sub['id']})
    assert call(runtime, 0, 'readiness.accept', {'id': update['id']}, ok=False)['code'] == 'STALE_VERSION'
    history = call(runtime, 0, 'readiness.updates', {'history': True})['updates']
    assert history[0]['id'] == update['id'] and history[0]['status'] == 'cancelled'


def test_operator_pending_readonly_unique_sequences_and_plain_message_grouping(runtime, tmp_path):
    service, _ = runtime
    (tmp_path/'a').write_text('a'); (tmp_path/'b').write_text('b')
    request(runtime)
    subscription(runtime)
    call(runtime, 2, 'readiness.subscribe', {'producer': context(runtime, 1).actor_id,
        'artifact': 'candidate', 'paths': ['a'], 'next_action': 'Review'})
    publish(runtime)
    note = call(runtime, 1, 'message.send', {'recipients': [context(runtime, 0).actor_id,context(runtime, 2).actor_id],
        'kind': 'note','subject': 'Both','body': 'Review'})
    operator = Context(service.workspace.id, None, operator=True, identity_mode='operator', transport='operator')
    with service.store.read() as tx:
        actions = decisions.select_actions(tx, operator) + readiness.select_actions(tx, operator) + messages.select_unhandled(tx, operator, exclude_domain_notifications=True)
        sequences = [row['sequence'] for row in actions]
        assert len(sequences) == len(set(sequences)) == 4
        assert len([row for row in actions if row['id'] == note['id']]) == 1
        assert messages.count_pending(tx, operator, exclude_domain_notifications=True)['messages'] == 1
        assert readiness.count_pending(tx, operator)['readiness'] == 2
        assert sum(decisions.count_pending(tx, operator).values()) == 1
        assert tx.connection.execute('SELECT COUNT(*) FROM recipients WHERE handled_us IS NOT NULL OR presented_us IS NOT NULL').fetchone()[0] == 0
    with service.store.write() as tx, pytest.raises(CoordinationError):
        messages.mark_presented(tx, operator, [note['id']])


def test_follow_presentation_preserves_full_pending_until_explicit_unfollow(runtime):
    service, _ = runtime
    decision = request(runtime)
    call(runtime, 2, 'decision.follow', {'id': decision['id']})
    call(runtime, 1, 'decision.resolve', {'id': decision['id'], 'state': 'answered', 'response': 'answer'})
    observer = context(runtime, 2)
    with service.store.write() as tx:
        actions = decisions.select_actions(tx, observer)
        assert len(actions) == 1
        decisions.mark_presented(tx, observer, actions)
        tx.connection.execute('INSERT INTO action_presentations VALUES (?,?,?,?,?)',
            (observer.actor_id, actions[0]['kind'], actions[0]['id'], actions[0]['version'], tx.now_us))
    with service.store.read() as tx:
        assert len(decisions.select_actions(tx, observer)) == 1
        assert decisions.select_actions(tx, observer, new_only=True) == []
    call(runtime, 2, 'work.activity', {'task': 'new observer task', 'note': 'Other work', 'paths': ['other']})
    with service.store.read() as tx:
        assert decisions.select_actions(tx, context(runtime, 2))[0]['status'] == 'obsolete_generation'
    call(runtime, 2, 'decision.unfollow', {'id': decision['id']})
    with service.store.read() as tx:
        assert decisions.select_actions(tx, context(runtime, 2)) == []


def test_exact_notification_folding_excludes_records_beyond_selected_page(runtime):
    service, _ = runtime
    recipient = context(runtime, 1)
    first = None
    for number in range(105):
        row = request(runtime, subject=f'Request {number}')
        first = first or row
    with service.store.write() as tx:
        sender = context(runtime, 0)
        for number in range(24):
            decisions._notice(tx, sender, first, 'diagnostic', [recipient.actor_id], f'Retained diagnostic {number}')
    with service.store.read() as tx:
        actions = decisions.select_actions(tx, recipient, limit=3)
        assert len(actions) == 3
        assert actions[0]['notice_count'] == 25 and len(actions[0]['message_ids']) == 20 and actions[0]['message_ids_more']
        assert messages.select_unhandled(tx, recipient, exclude_domain_notifications=True) == []
        assert messages.count_pending(tx, recipient, exclude_domain_notifications=True)['messages'] == 0
        assert sum(decisions.count_pending(tx, recipient).values()) == 105


def test_readiness_new_only_and_scoped_filters_precede_limit(runtime, tmp_path):
    service, _ = runtime
    (tmp_path/'a').write_text('a'); (tmp_path/'b').write_text('b')
    subscription(runtime)
    first = publish(runtime)
    consumer = context(runtime, 0)
    with service.store.write() as tx:
        action = readiness.select_actions(tx, consumer)[0]
        readiness.mark_presented(tx, consumer, [action])
        tx.connection.execute('INSERT INTO action_presentations VALUES (?,?,?,?,?)',
            (consumer.actor_id, action['kind'], action['id'], action['version'], tx.now_us))
    with service.store.read() as tx:
        assert readiness.select_actions(tx, consumer, new_only=True) == []
        assert len(readiness.select_actions(tx, consumer)) == 1
    (tmp_path/'a').write_text('a2')
    second = publish(runtime)
    with service.store.read() as tx:
        actions = readiness.select_actions(tx, consumer, limit=1, new_only=True, filters={'path': 'b'})
        assert len(actions) == 1 and actions[0]['receipt_id'] == second['id'] != first['id']
        assert readiness.select_actions(tx, consumer, filters={'path': 'bb'}) == []
        assert readiness.count_pending(tx, consumer, filters={'path': 'bb'})['readiness'] == 0


def test_async_handoff_rechecks_recipient_generation_and_worker_claim(runtime, tmp_path, monkeypatch):
    service, _ = runtime
    (tmp_path/'a').write_text('a')
    queued = call(runtime, 1, 'readiness.handoff', {'artifact': 'candidate', 'paths': ['a'], 'evidence': 'passing',
        'recipients': [context(runtime, 0).actor_id], 'subject': 'Ready', 'body': 'Candidate'})
    call(runtime, 0, 'work.activity', {'task': 'new recipient task', 'note': 'Different scope', 'paths': ['other']})
    result = service.run_operation(queued['operation_id'])
    assert result['state'] == 'failed' and result['error']['code'] == 'STALE_GENERATION'
    queued = call(runtime, 1, 'readiness.publish', {'artifact': 'candidate', 'paths': ['a'], 'evidence': 'passing'})
    original = readiness.prepare_hashes
    def cancel_during_hash(*args, **kwargs):
        hashes = original(*args, **kwargs)
        with service.store.write() as tx:
            tx.connection.execute("UPDATE operations SET state='cancelled' WHERE id=?", (queued['operation_id'],))
        return hashes
    monkeypatch.setattr(readiness, 'prepare_hashes', cancel_during_hash)
    assert service.run_operation(queued['operation_id'])['state'] == 'cancelled'
    with service.store.read() as tx:
        assert tx.connection.execute('SELECT COUNT(*) FROM receipts').fetchone()[0] == 0
        assert tx.connection.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 0


def test_decision_chunks_retrieve_complete_multibyte_body_and_exact_proposal(runtime):
    body = '🙂' * 4000
    decision = request(runtime, body=body)
    result = call(runtime, 0, 'decision.get', {'id': decision['id']})
    chunks = [result['body']]
    while result['body_chunk']['next_offset'] is not None:
        result = call(runtime, 0, 'decision.get', {'id': decision['id'], 'offset': result['body_chunk']['next_offset']})
        chunks.append(result['body'])
    assert ''.join(chunks) == body
    call(runtime, 1, 'work.activity', {'task': 'reassigned', 'paths': ['src'], 'note': 'New task'})
    proposal = call(runtime, 1, 'decision.resolve', {'id': decision['id'], 'state': 'answered', 'response': body})['proposal']
    result = call(runtime, 0, 'decision.get', {'id': decision['id'], 'proposal_id': proposal['id']})
    assert result['proposal']['response_chunk']['bytes'] == len(body.encode())
    assert result['proposal']['response_chunk']['next_offset'] == 8192


def test_readiness_scope_pages_and_inspection_traverse_all_latest_artifacts(runtime, tmp_path):
    for number in range(27):
        (tmp_path/f'p{number:02}').write_text(str(number))
    receipt = publish(runtime, paths=tuple(f'p{number:02}' for number in range(27)))
    assert receipt['path_count'] == 27 and len(receipt['hashes']) == 20 and receipt['paths_more']
    first = call(runtime, 0, 'work.evidence_detail', {'kind': 'readiness', 'id': receipt['id'], 'limit': 10})
    hashes = dict(first['hashes'])
    while first['paths_after'] is not None:
        first = call(runtime, 0, 'work.evidence_detail', {'kind': 'readiness', 'id': receipt['id'],
                     'limit': 10, 'paths_after': first['paths_after']})
        hashes.update(first['hashes'])
    assert set(hashes) == {f'p{number:02}' for number in range(27)}
    for number in range(4):
        run(runtime, call(runtime, 1, 'readiness.publish', {'artifact': f'other-{number}',
            'paths': ['p00'], 'evidence': 'check'}))
    inspection = run(runtime, call(runtime, 0, 'readiness.inspect', {'limit': 2}))
    ids = [row['id'] for row in inspection['receipts']]
    while inspection['after'] is not None:
        inspection = run(runtime, call(runtime, 0, 'readiness.inspect', {'limit': 2, 'after': inspection['after']}))
        ids.extend(row['id'] for row in inspection['receipts'])
    assert len(ids) == len(set(ids)) == 5


def test_explicit_transfer_replaces_authority_and_supersedes_old_proposal(runtime):
    decision = request(runtime)
    call(runtime, 1, 'work.activity', {'task': 'reassigned', 'note': 'new work', 'paths': ['src']})
    proposal = call(runtime, 1, 'decision.resolve', {'id': decision['id'], 'state': 'answered', 'response': 'old authority'})['proposal']
    transferred = call(runtime, 0, 'decision.transfer', {'id': decision['id'], 'recipient': context(runtime, 2).actor_id, 'reason': 'Confirmed transfer'})
    assert transferred['recipient_id'] == context(runtime, 2).actor_id and transferred['state'] == 'open'
    assert call(runtime, 1, 'decision.resolve', {'id': decision['id'], 'state': 'answered', 'response': 'stale answer'}, ok=False)['code'] == 'NOT_AUTHORIZED'
    assert call(runtime, 0, 'decision.reconcile', {'id': decision['id'], 'proposal_id': proposal['id'], 'accept': True}, ok=False)['code'] == 'STALE_VERSION'
    assert call(runtime, 2, 'decision.resolve', {'id': decision['id'], 'state': 'answered', 'response': 'new authority'})['state'] == 'answered'


def test_rejected_answer_can_be_explicitly_reproposed_without_reusing_disposed_identity(runtime):
    decision = request(runtime)
    call(runtime, 1, 'work.activity', {'task': 'reassigned', 'note': 'new task', 'paths': ['src']})
    args = {'id': decision['id'], 'state': 'answered', 'response': 'Same recorded answer'}
    first = call(runtime, 1, 'decision.resolve', args)['proposal']
    call(runtime, 0, 'decision.reconcile', {'id': decision['id'], 'proposal_id': first['id'], 'accept': False})
    second = call(runtime, 1, 'decision.resolve', args)['proposal']
    assert second['id'] != first['id'] and second['state'] == 'pending'


def test_imported_oversized_decision_escalation_retains_complete_content(runtime):
    service, bindings = runtime
    parent = context(runtime, 2)
    with service.store.read() as tx:
        session = tx.connection.execute('SELECT native_session_id FROM actors WHERE id=?', (parent.actor_id,)).fetchone()[0]
    bindings.append(identity.bind_native(service.store, {'harness': 'codex', 'native_session_id': session,
        'child_id': uid(), 'parent_id': parent.actor_id, 'source': 'native_child_start', 'task': 'child'}))
    decision = request(runtime, recipient=3)
    full = 'Retained full content. ' * 6000
    with service.store.write() as tx:
        tx.connection.execute('UPDATE decisions SET body=?,deadline_us=0 WHERE id=?', (full, decision['id']))
        assert decisions.route_due(tx, context(runtime, 0))[0]['routing_state'] == 'escalated'
    reply = call(runtime, 2, 'decision.get', {'id': decision['id']})
    assert reply['body_chunk']['bytes'] == len(full.encode()) and reply['body_chunk']['next_offset']
    with service.store.read() as tx:
        notice = messages.select_unhandled(tx, parent)[0]
        message = messages.read_message(tx, parent, notice['id'])
        assert 'retrieve the complete retained decision' in message['body']


def test_interleaved_uncertainty_is_compacted_per_execution_without_state_effects(runtime):
    service, _ = runtime
    owner = context(runtime, 1)
    with service.store.write() as tx:
        before = identity._actor(tx, owner.actor_id)
        for _ in range(10):
            for event in ('stop', 'end', 'failure'):
                identity.apply_lifecycle_event(tx, owner, state='completed', execution_generation=None, event=event)
        assert identity._actor(tx, owner.actor_id) == before
        assert tx.connection.execute("SELECT COUNT(*) FROM events WHERE actor_id=? AND kind='ambiguous_event'", (owner.actor_id,)).fetchone()[0] == 3
        identity.start_execution(tx, owner, native_run_id='new-execution')
        identity.apply_lifecycle_event(tx, owner, state='completed', execution_generation=None, event='stop')
        assert tx.connection.execute("SELECT COUNT(*) FROM events WHERE actor_id=? AND kind='ambiguous_event'", (owner.actor_id,)).fetchone()[0] == 4
