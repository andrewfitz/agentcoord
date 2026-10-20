import json
import sys

import pytest

from agentcoord.config import Config, Workspace, load_config
from agentcoord.core import CoordinationError
from agentcoord.resource_provider import event_message, query_status, validate_status


def status(state='queued'):
    return {'request_id': 'request-1', 'resource': 'tests', 'state': state,
            'holder': 'runner-owner', 'queue_position': 2,
            'start_deadline': '2026-10-19T12:00:00Z',
            'result': {'exit_code': 0} if state in {'passed', 'failed', 'cancelled', 'expired', 'error', 'superseded'} else None}


def command(script, **kwargs):
    return {'argv': [sys.executable, '-c', script], **kwargs}


def test_status_reads_actual_command_without_shell(tmp_path):
    expected = status()
    assert query_status(command(f'print({json.dumps(expected)!r})'), cwd=tmp_path) == expected


@pytest.mark.parametrize('script', ['raise SystemExit(3)', 'print("not json")',
                                  'print("x" * 65537)',
                                  'import sys; sys.stderr.write("x" * 65537)'])
def test_provider_failures_are_honest_and_bounded(tmp_path, script):
    with pytest.raises(CoordinationError):
        query_status(command(script), cwd=tmp_path)


def test_deadline_kills_inherited_pipe(tmp_path):
    with pytest.raises(CoordinationError, match='deadline'):
        query_status(command('import subprocess, sys; subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])', timeout_seconds=1), cwd=tmp_path)


@pytest.mark.parametrize('field,value', [('queue_position', True), ('start_deadline', 'yesterday'),
                                       ('holder', 12), ('result', {'exit_code': 0})])
def test_invalid_status_is_not_success(field, value):
    data = status()
    data[field] = value
    with pytest.raises(CoordinationError):
        validate_status(data)


@pytest.mark.parametrize('state', ['passed', 'failed', 'cancelled', 'expired', 'error', 'superseded'])
def test_terminal_events_retain_runner_truth(state):
    event = {'event_id': 'runner-event-1', 'kind': 'terminal', 'status': status(state)}
    first = event_message('local', event, ['recipient'])
    assert first == event_message('local', event, ['recipient'])
    assert json.loads(first['params']['body']) == event['status']
    assert first['key'] != event_message('other', event, ['recipient'])['key']


def test_mismatched_event_rejected():
    with pytest.raises(CoordinationError):
        event_message('local', {'event_id': 'a', 'kind': 'terminal', 'status': status()}, ['recipient'])


def test_optional_config(tmp_path):
    workspace = Workspace('unused', tmp_path, tmp_path, tmp_path, tmp_path, tmp_path)
    assert load_config(workspace).resource_providers == Config().resource_providers == {}
    (tmp_path / '.agentcoord.toml').write_text('[resources.providers.local]\nargv=["runner", "status", "request-1"]\ntimeout_seconds=5\n')
    assert load_config(workspace).resource_providers['local'] == {
        'argv': ['runner', 'status', 'request-1'], 'timeout_seconds': 5}


def test_request_id_is_appended_as_literal_argv_token(tmp_path):
    script = 'import json,sys; data=' + repr(status()) + '; data["request_id"]=sys.argv[1]; print(json.dumps(data))'
    token = 'request-$(touch-marker)'
    provider = command(script)
    assert query_status(provider, cwd=tmp_path, request_id=token)['request_id'] == token
    assert len(provider['argv']) == 3


@pytest.mark.parametrize('request_id', ['', '--help', 'two tokens', 'line\nfeed', 12])
def test_request_id_cannot_be_an_option_or_multiple_tokens(tmp_path, request_id):
    with pytest.raises(CoordinationError):
        query_status(command('raise AssertionError("must not start")'), cwd=tmp_path, request_id=request_id)
