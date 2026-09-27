"""Canonical result semantics survive history and provider projection."""
import json

import pytest

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.pilot import PilotAction
from pmharness.drivers.anthropic import AnthropicDriver
from pmharness.drivers.base import chat_completions_messages
from harness.send_loop_phases import dispatch_readonly_action


@pytest.mark.parametrize('content,ok,status,is_error', [
    ('permission refused', False, 'error', True),
    ('unspecified outcome', None, None, None),
    ('{"is_error":true}', True, 'error', True),
    ('plain output', True, 'ok', False),
    ('{"status":"failed","output":"oops"}', True, 'failed', True),
    ('{"status":"completed"}', True, 'completed', False),
    ('{"status":"unknown"}', True, 'unknown', None),
    ('{"status":"running"}', True, 'running', None),
    ('{"status":"future-state"}', True, 'future-state', None),
])
def test_result_status_survives_projection(tmp_path, content, ok, status, is_error):
    session = ConversationalSession(HarnessConfig(driver='stub-oracle-v2', state_dir=str(tmp_path)))
    session._history = [{'role': 'system', 'content': 'sys'}, {
        'role': 'assistant', 'content': '', 'tool_calls': [{
            'id': 'call', 'type': 'function',
            'function': {'name': 'run_command', 'arguments': '{}'},
        }],
    }]
    action = PilotAction(kind='run_command', tool_call_id='call')
    session._append_action_result(action, 'call', content, True, ok=ok, force_inline=True)
    message = session._history[-1]
    expected_history_content = content
    if is_error is True:
        expected_history_content += (
            '\n\nHost guidance: This tool call failed. Describe this check as failed or '
            'unverified, preserve any later recovery separately, and infer no cause '
            'beyond the diagnostic above.'
        )
    assert message['content'] == expected_history_content
    assert message.get('status') == status
    assert message.get('is_error') is is_error
    if is_error is None:
        assert 'is_error' not in message
    outbound = session._messages_for_provider()
    driver = AnthropicDriver(name='test', model='test', base_url='https://unused', api_key_env='UNUSED')
    block = driver._build_body(outbound, None, None)['messages'][-1]['content'][0]
    expected_content = expected_history_content if is_error is not True else json.dumps({
        'output': expected_history_content, 'status': status, 'is_error': is_error,
    })
    assert block['content'] == expected_content
    assert block.get('is_error') is is_error
    if is_error is None:
        assert 'is_error' not in block
    projected = chat_completions_messages(outbound)[-1]
    assert projected == {'role': 'tool', 'tool_call_id': 'call', 'content': expected_content}
    assert message.get('status') == status  # projection never mutates canonical history
    assert json.loads(json.dumps(message)) == message


def test_receipt_status_is_captured_before_output_compaction(tmp_path):
    from unittest.mock import PropertyMock, patch

    session = ConversationalSession(HarnessConfig(driver='stub-oracle-v2', state_dir=str(tmp_path)))
    action = PilotAction(kind='run_command_batch', tool_call_id='call')
    with patch.object(ConversationalSession, '_turn_economy', new_callable=PropertyMock) as economy, \
            patch('harness.repeat_tool_reminder.note_repeat_and_maybe_nudge',
                  side_effect=lambda _session, _act, value: value + '\nrepeat reminder'), \
            patch('harness.runaway_guard.note_runaway_and_maybe_steer',
                  side_effect=lambda _session, _act, value, **_kwargs: value + '\nrunaway reminder'):
        economy.return_value.persist_tool_result.return_value = 'output stored elsewhere'
        session._append_action_result(action, 'call', '{"status":"failed"}', True)
    expected_content = (
        'output stored elsewhere\nrepeat reminder\nrunaway reminder\n\n'
        'Host guidance: This tool call failed. Describe this check as failed or '
        'unverified, preserve any later recovery separately, and infer no cause '
        'beyond the diagnostic above.'
    )
    assert session._history[-1] == {
        'role': 'tool', 'tool_call_id': 'call', 'content': expected_content,
        'status': 'failed', 'is_error': True, '_spill_tool': 'run_command_batch',
    }
    outbound = session._messages_for_provider()[-1]
    assert '_spill_tool' not in outbound
    assert 'tool_name' not in outbound


@pytest.mark.parametrize("prefetched", [False, True])
def test_readonly_failure_persists_status_and_guidance(tmp_path, prefetched):
    session = ConversationalSession(HarnessConfig(driver='stub-oracle-v2', state_dir=str(tmp_path)))
    session._history = []
    action = PilotAction(kind='search_files', query='needle', tool_call_id='search-call')
    result = (False, 'exception', 'search backend unavailable')
    session._do_search_files = lambda _act: result
    prefetch = {0: result} if prefetched else {}
    list(dispatch_readonly_action(session, action, 0, 'search-call', prefetch, True))
    message = session._history[-1]
    assert message['status'] == 'error'
    assert message['is_error'] is True
    assert "search backend unavailable" in message['content']
    assert "This tool call failed" in message['content']


def test_readonly_success_persists_non_error_without_failure_guidance(tmp_path):
    session = ConversationalSession(HarnessConfig(driver='stub-oracle-v2', state_dir=str(tmp_path)))
    session._history = []
    action = PilotAction(kind='read_file', path='ok.py', tool_call_id='read-call')
    session._do_read_file = lambda _act: (True, 'ok', 'body')
    list(dispatch_readonly_action(session, action, 0, 'read-call', {}, True))
    message = session._history[-1]
    assert message['status'] == 'ok'
    assert message['is_error'] is False
    assert "Host guidance" not in message['content']


def test_legacy_failure_result_includes_diagnostic_and_guidance(tmp_path):
    session = ConversationalSession(HarnessConfig(driver='stub-oracle-v2', state_dir=str(tmp_path)))
    session._history = []
    action = PilotAction(kind='search_files', query='needle', tool_call_id='search-call')
    session._append_action_result(
        action, 'search-call', 'search backend unavailable', False, ok=False,
    )
    message = session._history[-1]
    assert message['role'] == 'user'
    assert "search backend unavailable" in message['content']
    assert "This tool call failed" in message['content']


def test_later_success_stays_separate_from_prior_failure_guidance(tmp_path):
    session = ConversationalSession(HarnessConfig(driver='stub-oracle-v2', state_dir=str(tmp_path)))
    session._history = []
    failed = PilotAction(kind='read_file', path='a.py', tool_call_id='read-failed')
    recovered = PilotAction(kind='read_file', path='a.py', tool_call_id='read-recovered')
    session._append_action_result(failed, 'read-failed', 'permission refused', True, ok=False)
    session._append_action_result(recovered, 'read-recovered', 'body', True, ok=True)
    assert "permission refused" in session._history[-2]['content']
    assert "This tool call failed" in session._history[-2]['content']
    assert session._history[-1]['content'] == 'body'
    assert session._history[-1]['is_error'] is False
