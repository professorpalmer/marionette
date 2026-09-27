import copy
import io
import json

import pytest

from pmharness.drivers.base import tool_result_semantics
from pmharness.drivers.anthropic import AnthropicDriver
from pmharness.drivers.gemini import GeminiDriver
from pmharness.drivers.codex_responses import CodexResponsesDriver
from pmharness.drivers.openai_compat import OpenAICompatDriver


@pytest.mark.parametrize('receipt', [
    {'status': 'timed_out'}, {'status': 'canceled'},
    {'status': 'ok', 'ok': False}, {'status': 'ok', 'error': 'oops'},
])
def test_contradictory_and_failure_receipts(receipt):
    assert tool_result_semantics(json.dumps(receipt), ok=True)['is_error'] is True


def test_absent_outcome_is_distinct_from_explicit_unknown():
    assert tool_result_semantics('ordinary text') == {}
    assert tool_result_semantics('{"status":"unknown"}', ok=True) == {'status': 'unknown'}


@pytest.mark.parametrize('provider', ['openai', 'gemini', 'codex', 'anthropic'])
@pytest.mark.parametrize('status,error', [('failed', True), ('cancelled', True), ('canceled', True), ('timed_out', True), ('unknown', None), ('running', None), ('truncated', None)])
def test_compacted_result_in_actual_body(provider, status, error, monkeypatch, tmp_path):
    messages = [
        {'role': 'assistant', 'content': '', 'tool_calls': [
            {'id': 'exact-call-1', 'type': 'function', 'function': {'name': 'run_command', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'exact-call-1', 'content': 'output stored elsewhere', 'status': status},
    ]
    if error is not None:
        messages[-1]['is_error'] = error
    # Exercise real compaction and the session's outbound projection first.
    from unittest.mock import PropertyMock, patch
    from harness.config import HarnessConfig
    from harness.conversation import ConversationalSession
    from harness.pilot import PilotAction
    session = ConversationalSession(HarnessConfig(driver='stub-oracle-v2', state_dir=str(tmp_path)))
    session._history = [{"role": "system", "content": "sys"}] + messages[:-1]
    with patch.object(ConversationalSession, '_turn_economy', new_callable=PropertyMock) as economy:
        economy.return_value.persist_tool_result.return_value = 'output stored elsewhere'
        session._append_action_result(
            PilotAction(kind='run_command', tool_call_id='exact-call-1'), 'exact-call-1',
            json.dumps({'status': status}), True,
        )
    messages = json.loads(json.dumps([
        message for message in session._messages_for_provider() if message["role"] != "system"
    ]))
    before = copy.deepcopy(messages)
    kwargs = dict(name='test', model='test', api_key_env='TEST_RESULT_KEY')
    monkeypatch.setenv('TEST_RESULT_KEY', 'unused')
    if provider == 'openai':
        captured = []
        def urlopen(req, **kwargs):
            captured.append(json.loads(req.data))
            return io.BytesIO(b'{"choices":[{"message":{"content":"done"}}]}')
        monkeypatch.setattr('urllib.request.urlopen', urlopen)
        OpenAICompatDriver(base_url='https://unused', **kwargs).chat(messages)
        body = captured[0]
        result = body['messages'][-1]
        assert set(result) == {'role', 'tool_call_id', 'content'}
        assert result['tool_call_id'] == body['messages'][0]['tool_calls'][0]['id'] == 'exact-call-1'
        payload = json.loads(result['content'])
    elif provider == 'codex':
        body = CodexResponsesDriver(**kwargs)._build_body(messages)
        call, result = body['input']
        assert result['call_id'] == call['call_id'] == 'exact-call-1'
        assert set(result) == {'type', 'call_id', 'output'}
        payload = json.loads(result['output'])
    elif provider == 'gemini':
        body = GeminiDriver(**kwargs)._chat_body(messages, None, None)
        call = body['contents'][0]['parts'][0]['functionCall']
        result = body['contents'][1]['parts'][0]['functionResponse']
        assert result['name'] == call['name'] == 'run_command'
        payload = result['response']
    else:
        body = AnthropicDriver(base_url='https://unused', **kwargs)._build_body(messages, None, None)
        call = body['messages'][0]['content'][0]
        result = body['messages'][1]['content'][0]
        assert result['tool_use_id'] == call['id'] == 'exact-call-1'
        assert result.get('is_error') is error
        payload = json.loads(result['content'])
    assert payload['status'] == status
    assert payload.get('is_error') is error
    expected_output = 'output stored elsewhere'
    if error is True:
        expected_output += (
            '\n\nHost guidance: This tool call failed. Describe this check as failed or '
            'unverified, preserve any later recovery separately, and infer no cause '
            'beyond the diagnostic above.'
        )
    assert payload['output'] == expected_output
    assert messages == before


@pytest.mark.parametrize('content,metadata', [
    ('ordinary text', {}),
    ('ordinary text', {'status': 'ok', 'is_error': False}),
    ('{"status":"unknown","output":{"nested":[1,2]}}', {'status': 'unknown'}),
    ('{"status":"failed","error":"bad","output":"details"}', {'status': 'failed', 'is_error': True}),
    ('{"status":"ok","ok":false,"output":"details"}', {'status': 'ok', 'is_error': True}),
])
def test_projection_preserves_existing_content(content, metadata):
    from pmharness.drivers.base import chat_completions_messages
    messages = [{'role': 'tool', 'tool_call_id': 'pair', 'content': content, **metadata}]
    kwargs = dict(name='test', model='test', api_key_env='UNUSED')
    assert chat_completions_messages(messages)[0]['content'] == content
    assert CodexResponsesDriver(**kwargs)._build_body(messages)['input'][0]['output'] == content
    block = AnthropicDriver(base_url='https://unused', **kwargs)._build_body(messages, None, None)['messages'][0]['content'][0]
    assert block['content'] == content
    assert block.get('is_error') is metadata.get('is_error')
    response = GeminiDriver(**kwargs)._chat_body(messages, None, None)['contents'][0]['parts'][0]['functionResponse']['response']
    assert response == (json.loads(content) if content.startswith('{') else {'content': content})


@pytest.mark.parametrize('status', [
    'error', 'failed', 'failure', 'exception', 'timeout', 'timed_out',
    'cancelled', 'canceled', 'blocked', 'denied', 'interrupted', 'aborted',
    'validation_error', 'stale_anchor', 'repo_not_open', 'path_traversal',
    'invalid_arguments', 'stale_generation', 'read_only_role', 'disabled',
    'not_found', 'is_directory', 'not_a_directory', 'filenotfound',
    'corrupt_store', 'cap_exceeded', 'verification_failed', 'ambiguous',
    'internal_uri_error',
])
def test_failure_status_vocabulary(status):
    assert tool_result_semantics(json.dumps({'status': status, 'ok': True}), ok=True) == {
        'status': status, 'is_error': True,
    }


@pytest.mark.parametrize('status', ['registered', 'queued', 'pending', 'running', 'unknown', 'truncated', 'future-state'])
def test_nonterminal_and_uncertain_status_is_not_success(status):
    assert tool_result_semantics(json.dumps({'status': status}), ok=True) == {'status': status}


@pytest.mark.parametrize('execution,status,error', [
    ((False, 'error', 'dispatch failed'), 'error', True),
    ((True, 'success', {'output': 'oops', 'exit_code': 7, 'status': 'ok'}), 'ok', True),
    ((True, 'success', {'output': 'done', 'exit_code': 0, 'status': 'ok'}), 'ok', False),
    ((True, 'success', {'output': 'partial', 'exit_code': 0, 'status': 'truncated'}), 'truncated', None),
])
def test_foreground_command_call_sites_preserve_outcome(tmp_path, monkeypatch, execution, status, error):
    from harness.config import HarnessConfig
    from harness.conversation import ConversationalSession
    from harness.pilot import PilotAction
    from harness.send_loop_phases import dispatch_local_action
    session = ConversationalSession(HarnessConfig(driver='stub-oracle-v2', repo=str(tmp_path), state_dir=str(tmp_path)))
    monkeypatch.setattr(session, '_do_run_command', lambda act: execution)
    action = PilotAction(kind='run_command', command='echo done', tool_call_id='foreground-call')
    list(dispatch_local_action(session, action, 'foreground-call', True, []))
    receipt = session._history[-1]
    assert receipt['tool_call_id'] == 'foreground-call'
    assert receipt['status'] == status
    assert receipt.get('is_error') is error
    if error is False:
        assert receipt['content'].startswith("(run_command foreground-call completed with exit code 0)")


@pytest.mark.parametrize('status', ['ok', 'success', 'completed', 'done', 'no_op', 'native_image'])
def test_known_success_status_vocabulary(status):
    assert tool_result_semantics(json.dumps({'status': status})) == {'status': status, 'is_error': False}
