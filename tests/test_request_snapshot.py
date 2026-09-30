from tests._isolation import isolate_module_attr
from copy import deepcopy
from types import SimpleNamespace
import queue
import threading

import pytest

from harness.send_loop_phases import dispatch_sync_pilot_chat, run_stream
from harness.log_reconstruction import check_outbound_reconstruction


@pytest.mark.parametrize('stream', [False, True])
def test_pending_driver_input_isolated_from_live_mutation(stream):
    entered, release = threading.Event(), threading.Event()
    messages = [{'role': 'tool', 'content': [{'text': 'original'}], 'is_error': True}]
    tools = [{'name': 'read', 'input_schema': {'properties': {'path': {'type': 'string'}}}}]
    expected = deepcopy((messages, tools))
    seen = []

    def driver(value, **kwargs):
        entered.set()
        assert release.wait(3)
        seen.append(deepcopy((value, kwargs['tools'])))
        return SimpleNamespace(meta={}, tokens_out=0)

    session = SimpleNamespace(pilot=SimpleNamespace(chat=driver, chat_stream=driver),
                              _messages_for_provider=lambda: messages)
    q = queue.Queue()
    target = (lambda: run_stream(session, q, tools, 'system')) if stream else (
        lambda: dispatch_sync_pilot_chat(session, tools, 'system'))
    worker = threading.Thread(target=target)
    worker.start()
    try:
        assert entered.wait(3)
        messages[0]['content'][0]['text'] = 'changed'
        tools[0]['input_schema']['properties']['path']['type'] = 'integer'
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive()
    assert seen == [expected]


def test_missing_reconstruction_is_not_verified():
    session = SimpleNamespace()
    assert check_outbound_reconstruction(session, [], '') is False
    assert session._last_log_reconstruction['ok'] is False


def test_structured_error_field_is_compared():
    session = SimpleNamespace(_history=[{}, {'role': 'tool', 'content': 'x', 'is_error': True}],
                              _elide_stale_reads=lambda rows: rows)
    assert check_outbound_reconstruction(
        session, [{'role': 'tool', 'content': 'x', 'is_error': False}], '') is False


def test_frozen_revision_reconstructs_all_fields_without_leaking_receipt():
    import json
    from dataclasses import FrozenInstanceError
    from harness.request_snapshot import FrozenRequest

    secret = 'controlled-fixture-secret'
    messages = [{'role': 'assistant', 'content': secret, 'phase': 'analysis',
                 'tool_calls': [{'id': 'c1', 'thought_signature': secret}]},
                {'role': 'tool', 'tool_call_id': 'c1', 'is_error': True,
                 'content': [{'type': 'text', 'text': secret}]}]
    kwargs = {'tools': [{'name': 'read', 'schema': {'enum': [secret]}}],
              'system': secret, 'session_id': secret}
    snapshot = FrozenRequest.capture(lambda: None, messages, kwargs, secret)
    expected = deepcopy((messages, kwargs))
    messages[0]['phase'] = 'final'
    kwargs['system'] = 'changed'
    assert snapshot.materialize() == expected
    with pytest.raises(FrozenInstanceError):
        snapshot.payload = b'{}'
    value, arguments = snapshot.materialize()
    receipt = snapshot.receipt(value, arguments)
    assert receipt['ok'] and receipt['wire_status'] == 'unsupported'
    assert receipt['model_status'] == 'observed_only'
    assert secret not in json.dumps(receipt)
    value[0]['phase'] = 'changed'
    assert snapshot.receipt(value, arguments)['status'] == 'mismatch'
    assert snapshot.materialize() == expected
    for key, changed in [('tools', []), ('system', 'other'), ('session_id', 'other')]:
        value, arguments = snapshot.materialize()
        arguments[key] = changed
        assert not snapshot.receipt(value, arguments)['ok']


def test_stream_snapshot_precedes_thread_handoff_and_pilot_swap(monkeypatch):
    from harness import send_loop_phases as phases

    release = threading.Event()
    base_thread = threading.Thread
    messages = [{'role': 'user', 'content': 'original'}]
    tools = [{'type': 'function', 'function': {'name': 'read', 'parameters': {}}}]
    expected = deepcopy((messages, tools))
    seen = []
    calls = []

    def driver(value, *, tools, system, on_delta, on_reasoning_delta, on_tool_hint,
               session_id=None, on_wait_notice=None, on_stream_item_done=None):
        seen.append(deepcopy((value, tools)))
        assert system == 'original-system'
        assert session_id == 'session1'
        for callback in (on_delta, on_reasoning_delta, on_tool_hint,
                         on_wait_notice, on_stream_item_done):
            assert callable(callback)
        return SimpleNamespace(meta={}, tokens_out=0)

    class DelayedThread(base_thread):
        def run(self):
            assert release.wait(3)
            super().run()

    def normalized():
        calls.append(1)
        return messages

    session = SimpleNamespace(
        pilot=SimpleNamespace(chat=driver, chat_stream=driver, supports_streaming=True),
        config=SimpleNamespace(no_delegation=False), harness_session_id='session1',
        _build_visible_tools_schema=lambda: tools, _messages_for_provider=normalized,
    )

    def drain(q, accumulator=None):
        # Dispatch has prepared the request, but its worker cannot run yet.
        assert calls == [1]
        messages[0]['content'] = 'changed'
        tools[0]['function']['name'] = 'changed'
        session.pilot = SimpleNamespace(chat_stream=lambda *a, **kw: pytest.fail('swapped pilot'))
        release.set()
        kind, response = q.get(timeout=3)
        assert kind == 'done', response
        assert response.meta['request_integrity']['status'] == 'verified'
        assert seen == [expected]
        return '', response
        yield

    isolate_module_attr(monkeypatch, phases, 'threading', Thread=DelayedThread)
    monkeypatch.setattr(phases, 'drain_stream_queue', drain)
    try:
        list(phases.dispatch_pilot_provider_call(
            session, plan=False, sys_prompt='original-system', prompt='unused',
            synthesis_nudge_active=False,
        ))
    finally:
        release.set()


def test_complete_uses_frozen_dispatch_receipt():
    from harness.send_loop_phases import dispatch_pilot_provider_call
    response = SimpleNamespace(meta={}, tokens_out=0)

    def complete(prompt, *, system):
        assert prompt == 'prompt' and system == 'system'
        return response

    session = SimpleNamespace(pilot=SimpleNamespace(complete=complete, model='model'))
    list(dispatch_pilot_provider_call(session, plan=False, sys_prompt='system',
                                     prompt='prompt', synthesis_nudge_active=False))
    assert response.meta['request_integrity']['status'] == 'verified'
    assert response.meta['request_integrity']['message_count'] == 0


def test_comparison_error_is_not_verified_or_leaked():
    from harness.request_snapshot import FrozenRequest
    snapshot = FrozenRequest.capture(lambda: None, [], {'system': ''})
    session = SimpleNamespace()
    assert not check_outbound_reconstruction(
        session, [object()], request=snapshot, request_kwargs={'system': ''})
    assert session._last_log_reconstruction['status'] == 'error'
    assert session._last_log_reconstruction['ok'] is False


def test_capture_error_cannot_reuse_previous_verified_receipt():
    session = SimpleNamespace(
        pilot=SimpleNamespace(chat=lambda *a, **kw: pytest.fail('invalid dispatch')),
        _messages_for_provider=lambda: [{'content': object()}],
        _last_log_reconstruction={'ok': True, 'status': 'verified'},
    )
    with pytest.raises(TypeError):
        dispatch_sync_pilot_chat(session, [], '')
    assert session._last_log_reconstruction['ok'] is False
    assert session._last_log_reconstruction['status'] == 'error'


def test_real_normalizer_revision_survives_history_mutation():
    from harness.conversation import ConversationalSession
    from harness.request_snapshot import FrozenRequest

    session = SimpleNamespace(
        _history=[{'role': 'system', 'content': 'sys'},
                  {'role': 'assistant', 'content': '', 'tool_calls': [
                      {'id': 'run:0', 'type': 'function', 'function': {
                          'name': 'run', 'arguments': '{}'}}]},
                  {'role': 'tool', 'tool_call_id': 'run:0', 'content': 'result'},
                  {'role': 'system', 'source': 'goal_mode', 'content': 'continue'}],
        _sanitize_tool_pairs=lambda: None, _elide_stale_reads=lambda rows: rows,
    )
    outbound = ConversationalSession._messages_for_provider(session)
    assert outbound[-1]['role'] == 'user'
    assert outbound[0]['tool_calls'][0]['id'] != 'run:0'
    request = FrozenRequest.capture(lambda: None, outbound, {'system': 'sys'})
    session._history[1]['tool_calls'][0]['function']['arguments'] = 'changed'
    session._history[-1]['content'] = 'changed'
    assert request.materialize()[0] == outbound
    value, kwargs = request.materialize()
    assert check_outbound_reconstruction(session, value, request=request, request_kwargs=kwargs)
    assert session._last_log_reconstruction['scope'] == 'normalized_driver_inputs'
