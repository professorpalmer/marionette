"""Explicit owned request projection through native registration and API."""
import json
from types import SimpleNamespace

import pytest

pytest.importorskip('harness.job_readmodel', reason='requires companion PR2 metadata reader')

from harness.api.job_readmodel import get_local_metadata_detail
from harness.command_jobs import _register_standalone_command
from harness.job_readmodel import ActiveContext, KnownSources, MetadataReader, ReadContext
from test_local_job_metadata import Runner, ctx, forbidden
from test_native_operator_metadata import NoTraversal


def selected(runner, jid, **kwargs):
    index = runner.local_metadata_handle()
    return index.read_selected(ctx(), index.ref(jid), lane='children', **kwargs)


def test_real_dispatch_command_preview_and_no_model(tmp_path):
    runner = Runner(tmp_path)
    row = _register_standalone_command(runner, SimpleNamespace(command='printf selected-context'), 'selected-command')
    result = selected(runner, row['id'], include_context=True)
    assert result['selected_context']['request'] == {'text': 'printf selected-context', 'truncated': False}
    assert result['selected_context']['source'] == 'command_preview'
    assert result['selected_context']['omission'] == 'raw_command_not_retained'
    assert result['selected_context']['cwd']['text'] == str(tmp_path)
    assert result['summary']['display']['model'] == ''
    assert result['page']['outcome'] == 'unavailable'
    assert result['summary']['revision'] > 0


def test_owned_provider_request_api_and_background_privacy(tmp_path):
    runner = Runner(tmp_path)
    runner._register_local_job('local-request', 'Explain selected code', model='actual-model', engine='native', skip_routing_preview=True)
    index = runner.local_metadata_handle()
    reader = MetadataReader(lambda: ActiveContext('A', '/repo', 'generation'), KnownSources(()), index)
    query = {k: [v] for k, v in dict(ctx(), job_id='local-request', incarnation=index.incarnation, lane='children', include_context='true').items()}
    status, result = get_local_metadata_detail(query, reader)
    assert status == 200
    assert result['selected_context']['request']['text'] == 'Explain selected code'
    assert result['summary']['display']['model'] == 'native/actual-model'
    for lane in ('active', 'history'):
        assert 'Explain selected code' not in json.dumps(index.read_page(ctx(), lane=lane))
    assert 'selected_context' not in selected(runner, 'local-request')
    for value in ('false', '1', '', 'TRUE'):
        assert get_local_metadata_detail(dict(query, include_context=[value]), reader)[0] == 400


def test_bounds_before_encoding_nested_access_and_body_budget(tmp_path, monkeypatch):
    runner = Runner(tmp_path)
    runner._register_local_job('local-request', 'initial', skip_routing_preview=True)
    class SliceFirst(str):
        def encode(self, *args, **kwargs):
            forbidden()
        def __getitem__(self, key):
            assert isinstance(key, slice) and key.stop <= 2048
            return super().__getitem__(key)
    row = runner._local_jobs['local-request']
    deep = NoTraversal(secret='not selected')
    for _ in range(2000):
        deep = NoTraversal(nested=deep)
    row.update(goal=SliceFirst('\U0001f642' * 1000000), cwd=SliceFirst('\U0001f642' * 100000), tasks=[deep], artifacts=[deep],
               output='\U0001f642' * 10000, actions=[dict(goal='\U0001f642' * 10000)] * 100)
    monkeypatch.setattr(runner, 'get_local_job', forbidden)
    monkeypatch.setattr(runner, 'live_local_jobs', forbidden)
    index = runner.local_metadata_handle()
    for lane in ('output', 'actions', 'children'):
        result = index.read_selected(ctx(), index.ref('local-request'), lane=lane, include_context=True)
        context = result['selected_context']
        assert len(context['request']['text'].encode()) == 2048
        assert len(context['cwd']['text'].encode()) == 512
        assert context['request']['truncated'] and context['cwd']['truncated']
        assert len(json.dumps(result).encode()) <= 32768
        assert result['page']['scanned'] <= 51
    row['goal'] = deep
    result = selected(runner, 'local-request', include_context=True)
    assert result['selected_context']['request'] is None
    assert result['selected_context']['omission'] == 'request_unavailable'
    assert 'selected_context' not in index.read_selected(dict(ctx(), session_id='B'), index.ref('local-request'), include_context=True)
    assert 'selected_context' not in index.read_selected(ctx(), dict(index.ref('local-request'), incarnation='old'), include_context=True)


def test_actual_foreground_dispatch_projects_owned_registered_request(tmp_path):
    from harness.pilot import PilotAction
    from harness.send_loop_phases import dispatch_local_action
    from test_command_jobs import _Session
    session = _Session(str(tmp_path), str(tmp_path), session_id='A')
    calls = []
    def execute(act, **kwargs):
        calls.append(act.command)
        return True, 'success', {'output': 'selected-context\n', 'exit_code': 0, 'status': 'ok'}
    session._do_run_command = execute
    events = list(dispatch_local_action(session, PilotAction(kind='run_command', command='printf selected-context'), 'selected-dispatch', True, []))
    jid = events[0].data['job_id']
    assert calls == ['printf selected-context']
    result = selected(session, jid, include_context=True)
    assert result['summary']['lifecycle'] == 'completed'
    assert result['summary']['display']['model'] == ''
    assert result['selected_context']['request']['text'] == 'printf selected-context'
    assert result['selected_context']['cwd']['text'] == str(tmp_path)


def test_explicit_context_cursor_scope_and_incarnation_fences(tmp_path):
    from harness.job_readmodel import InvalidReadRequest, ViewChanged
    runner = Runner(tmp_path)
    runner._register_local_job('local-request', 'owned instruction', skip_routing_preview=True)
    runner._local_jobs['local-request']['output'] = 'x' * 10000
    index = runner.local_metadata_handle()
    ref = index.ref('local-request')
    first = index.read_selected(ctx(), ref, lane='output', include_context=True)
    cursor = first['page']['next_cursor']
    assert cursor
    with pytest.raises(InvalidReadRequest):
        index.read_selected(ctx(), ref, lane='output', cursor=cursor)
    runner._local_jobs['local-request']['status'] = 'cancelled'
    expired = index.read_selected(ctx(), ref, lane='output', cursor=cursor, include_context=True)
    assert expired['page']['outcome'] == 'expired'
    assert 'summary' not in expired and 'selected_context' not in expired
    calls = 0
    def changed_context():
        nonlocal calls
        calls += 1
        return ActiveContext('A', '/repo', 'generation' if calls == 1 else 'replaced')
    reader = MetadataReader(changed_context, KnownSources(()), index)
    with pytest.raises(ViewChanged):
        reader.read_local_selected(ReadContext(**ctx()), ref, include_context=True)


def test_selected_command_retains_writer_redaction_and_never_reads_payloads(tmp_path):
    runner = Runner(tmp_path)
    command = 'printf ok # token=' + 'synthetic-fixture-value'
    row = _register_standalone_command(runner, SimpleNamespace(command=command), 'selected-redacted')
    result = selected(runner, row['id'], include_context=True)
    assert 'synthetic-fixture-value' not in result['selected_context']['request']['text']
    assert 'REDACTED' in result['selected_context']['request']['text']
    runner._local_jobs[row['id']]['provider_payload'] = NoTraversal(secret='hidden')
    assert selected(runner, row['id'], include_context=True)['selected_context'] == result['selected_context']


