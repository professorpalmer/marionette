"""Canonical result semantics survive history and provider projection."""
import json

import pytest

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.pilot import PilotAction
from pmharness.drivers.anthropic import AnthropicDriver
from pmharness.drivers.base import chat_completions_messages


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
    assert message['content'] == content
    assert message.get('status') == status
    assert message.get('is_error') is is_error
    if is_error is None:
        assert 'is_error' not in message
    outbound = session._messages_for_provider()
    driver = AnthropicDriver(name='test', model='test', base_url='https://unused', api_key_env='UNUSED')
    block = driver._build_body(outbound, None, None)['messages'][-1]['content'][0]
    expected_content = content if ok is not False else json.dumps({
        'output': content, 'status': status, 'is_error': is_error,
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
    with patch.object(ConversationalSession, '_turn_economy', new_callable=PropertyMock) as economy:
        economy.return_value.persist_tool_result.return_value = 'output stored elsewhere'
        session._append_action_result(action, 'call', '{"status":"failed"}', True)
    assert session._history[-1] == {
        'role': 'tool', 'tool_call_id': 'call', 'content': 'output stored elsewhere',
        'status': 'failed', 'is_error': True, '_spill_tool': 'run_command_batch',
    }
    outbound = session._messages_for_provider()[-1]
    assert '_spill_tool' not in outbound
    assert 'tool_name' not in outbound
