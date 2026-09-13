"""Capture the actual urllib request produced by real drivers, without network."""
import json

import pytest

from harness.request_snapshot import FrozenRequest
from pmharness.drivers.anthropic import AnthropicDriver
from pmharness.drivers.openai_compat import OpenAICompatDriver
from pmharness.drivers.codex_responses import CodexResponsesDriver


class Captured(BaseException):
    pass


@pytest.mark.parametrize('kind', ['openai', 'anthropic', 'codex'])
@pytest.mark.parametrize('stream', [False, True])
def test_frozen_body_reaches_http_with_fresh_auth(monkeypatch, kind, stream):
    constructors = {
        'openai': lambda: OpenAICompatDriver('test', 'original', 'https://example.invalid/v1', 'TEST_REQUEST_KEY'),
        'anthropic': lambda: AnthropicDriver('test', 'original'),
        'codex': lambda: CodexResponsesDriver('test', 'original', chatgpt_backend=False),
    }
    monkeypatch.setenv('HARNESS_CODEX_REASONING_EFFORT', 'low')
    driver = constructors[kind]()
    token = ['old-token']
    monkeypatch.setattr(type(driver), '_key', lambda self: token[0])
    messages = [{'role': 'user', 'content': 'original-message'}]
    tools = [{'type': 'function', 'function': {'name': 'original_tool', 'parameters': {'type': 'object', 'properties': {}}}}]
    kwargs = {'tools': tools, 'system': 'original-system'}
    method = driver.chat_stream if stream else driver.chat
    request = FrozenRequest.capture(method, messages, kwargs, driver.model)
    assert request.wire_receipt()['wire_status'] == 'absent'
    driver.model = 'changed'
    driver.max_tokens = 7
    if kind == 'openai':
        driver.extra_body['model'] = 'changed-extra'
    messages[0]['content'] = 'changed-message'
    tools[0]['function']['name'] = 'changed_tool'
    token[0] = 'fresh-token'
    monkeypatch.setenv('HARNESS_CODEX_REASONING_EFFORT', 'high')
    captured = []

    def transport(req, **kw):
        captured.append(json.loads(req.data))
        assert 'fresh-token' in str(req.headers)
        raise Captured()

    monkeypatch.setattr('urllib.request.urlopen', transport)
    value, arguments = request.materialize()
    if stream:
        arguments['on_delta'] = lambda text: None
    with pytest.raises(Captured):
        request.method(value, **arguments)
    body = captured[0]
    assert body['model'] == 'original'
    assert body.get('max_tokens', body.get('max_output_tokens')) != 7
    if kind == 'codex':
        assert body['reasoning']['effort'] == 'low'
    assert 'original-message' in json.dumps(body.get('messages', body.get('input')))
    assert 'original_tool' in json.dumps(body['tools'])
    assert 'changed' not in json.dumps(body)
    receipt = request.wire_receipt()
    assert receipt['wire_status'] == 'verified'
    assert receipt['model_status'] == 'frozen_body'
    assert 'original-message' not in json.dumps(receipt)
    assert 'fresh-token' not in json.dumps(receipt)


def test_unsupported_driver_has_explicit_scope():
    class Cli:
        def chat(self, messages, **kwargs):
            pass
    request = FrozenRequest.capture(Cli().chat, [], {})
    assert request.wire_receipt()['wire_status'] == 'unsupported'


def test_reasoning_fallback_is_an_authorized_wire_boundary(monkeypatch):
    import io
    import urllib.error
    driver = OpenAICompatDriver('test', 'original', 'https://example.invalid/v1',
                                'TEST_REQUEST_KEY', enable_reasoning=True)
    monkeypatch.setattr(OpenAICompatDriver, '_key', lambda self: 'fresh-token')
    request = FrozenRequest.capture(driver.chat, [{'role': 'user', 'content': 'hello'}], {})
    bodies = []

    def transport(req, **kw):
        bodies.append(json.loads(req.data))
        if len(bodies) == 1:
            raise urllib.error.HTTPError(req.full_url, 400, 'bad', {},
                                         io.BytesIO(b'Unknown parameter reasoning'))
        raise Captured()

    monkeypatch.setattr('urllib.request.urlopen', transport)
    value, kwargs = request.materialize()
    with pytest.raises(Captured):
        request.method(value, **kwargs)
    assert 'reasoning' in bodies[0] and 'reasoning' not in bodies[1]
    assert request.wire_receipt()['wire_status'] == 'verified'
    assert request.wire_receipt()['wire_attempts'] == 2
    assert request.wire_receipt()['fallback_wire'][0]['wire_status'] == 'verified'


def test_length_continuation_keeps_captured_provider_settings(monkeypatch):
    driver = OpenAICompatDriver(
        'test', 'original', 'https://example.invalid/v1', 'TEST_REQUEST_KEY',
        extra_body={'top_p': 0.9},
    )
    monkeypatch.setattr(OpenAICompatDriver, '_key', lambda self: 'fresh-token')
    request = FrozenRequest.capture(
        driver.chat_stream, [{'role': 'user', 'content': 'hello'}], {},
    )
    driver.extra_body['top_p'] = 0.1
    bodies = []

    class Stream:
        def __init__(self, finish):
            self.finish = finish

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def __iter__(self):
            payload = {'choices': [{
                'delta': {'content': 'part'},
                'finish_reason': self.finish,
            }]}
            yield ('data: ' + json.dumps(payload) + '\n').encode()

    def transport(req, **kwargs):
        bodies.append(json.loads(req.data))
        return Stream('length' if len(bodies) < 3 else 'stop')

    monkeypatch.setattr('urllib.request.urlopen', transport)
    value, kwargs = request.materialize()
    response = request.method(value, on_delta=lambda _text: None, **kwargs)

    assert response.error is None
    assert [body['top_p'] for body in bodies] == [0.9, 0.9, 0.9]
    assert request.wire_receipt()['wire_status'] == 'verified'


def test_cached_claude_stream_keeps_immutable_comparison_baseline(monkeypatch):
    driver = OpenAICompatDriver(
        'test', 'anthropic/claude-sonnet-4',
        'https://openrouter.ai/api/v1', 'TEST_REQUEST_KEY',
    )
    monkeypatch.setenv('HARNESS_PROMPT_CACHE', '1')
    monkeypatch.setattr(OpenAICompatDriver, '_key', lambda self: 'fresh-token')
    request = FrozenRequest.capture(
        driver.chat_stream,
        [{'role': 'user', 'content': 'hello'}],
        {'system': 'system text'},
    )
    calls = []

    class Stream:
        def __init__(self, finish):
            self.finish = finish

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def __iter__(self):
            payload = {'choices': [{
                'delta': {'content': 'ok'},
                'finish_reason': self.finish,
            }]}
            yield ('data: ' + json.dumps(payload) + '\n').encode()

    def transport(*args, **kwargs):
        calls.append(1)
        return Stream('length' if len(calls) < 3 else 'stop')

    monkeypatch.setattr('urllib.request.urlopen', transport)
    value, kwargs = request.materialize()
    response = request.method(value, on_delta=lambda _text: None, **kwargs)

    assert response.error is None
    assert calls == [1, 1, 1]
    assert request.wire_receipt()['wire_status'] == 'verified'


def test_dispatch_attaches_transport_receipt(monkeypatch):
    import io
    from types import SimpleNamespace
    from harness.send_loop_phases import dispatch_sync_pilot_chat
    driver = OpenAICompatDriver('test', 'original', 'https://example.invalid/v1', 'TEST_REQUEST_KEY')
    monkeypatch.setattr(OpenAICompatDriver, '_key', lambda self: 'fresh-token')
    raw = {'choices': [{'message': {'content': 'ok'}, 'finish_reason': 'stop'}], 'usage': {}}
    monkeypatch.setattr('urllib.request.urlopen', lambda *a, **kw: io.BytesIO(json.dumps(raw).encode()))
    session = SimpleNamespace(pilot=driver, _messages_for_provider=lambda: [{'role': 'user', 'content': 'hi'}])
    response = dispatch_sync_pilot_chat(session, [], 'system')
    assert response.meta['request_integrity']['wire_status'] == 'verified'
    assert session._last_log_reconstruction['wire_status'] == 'verified'


def test_changed_http_model_is_not_verified(monkeypatch):
    import urllib.request
    driver = OpenAICompatDriver('test', 'original', 'https://example.invalid/v1', 'TEST_REQUEST_KEY')
    monkeypatch.setattr(OpenAICompatDriver, '_key', lambda self: 'token')
    request = FrozenRequest.capture(driver.chat, [{'role': 'user', 'content': 'hello'}], {})
    constructor = urllib.request.Request

    def rewrite(*args, **kwargs):
        req = constructor(*args, **kwargs)
        body = json.loads(req.data)
        body['model'] = 'unexpected-model'
        req.data = json.dumps(body).encode()
        return req

    def transport(req, **kwargs):
        assert json.loads(req.data)['model'] == 'unexpected-model'
        raise Captured()

    monkeypatch.setattr(urllib.request, 'Request', rewrite)
    monkeypatch.setattr(urllib.request, 'urlopen', transport)
    value, kwargs = request.materialize()
    with pytest.raises(Captured):
        request.method(value, **kwargs)
    assert request.wire_receipt()['wire_status'] == 'mismatch'
