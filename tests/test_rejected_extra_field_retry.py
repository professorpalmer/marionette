"""A relay that rejects a provider-dialect extra must not fail every turn.

Real report (2026-09-26): OpenCode Go began rejecting GLM-5.3's ``thinking``
field with HTTP 400 ``unknown field "thinking"``. Every glm-5.3-flash turn
failed. The driver now drops a rejected ``extra_body`` field for the session
and retries once. Core request fields are never dropped.
"""
from __future__ import annotations

import io
import json
import urllib.error

from pmharness.drivers.openai_compat import OpenAICompatDriver

_GO_THINKING_400 = {
    "error": {
        "param": "thinking",
        "type": "invalid_request_error",
        "message": 'Upstream request failed: [unknown_parameter] invalid request body: json: unknown field "thinking"',
    }
}


class _Resp:
    def __init__(self, payload):
        self._data = json.dumps(payload).encode()

    def read(self):
        return self._data

    def __iter__(self):
        return iter([])

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _ok_payload():
    return {
        "choices": [{"message": {"content": "ok", "tool_calls": []}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 1},
    }


def _driver(monkeypatch, extra_body):
    monkeypatch.setenv("TEST_OAI_KEY", "k")
    return OpenAICompatDriver(
        "opencode-go:glm-5.3-flash", "glm-5.3-flash", "http://x/v1", "TEST_OAI_KEY",
        extra_body=extra_body,
    )


def _reject(field: str, payload: dict):
    def fake_urlopen(req, timeout=None):
        body = json.loads(req.data.decode())
        fake_urlopen.bodies.append(body)
        if field in body:
            raise urllib.error.HTTPError(
                "http://x", 400, "bad", {}, io.BytesIO(json.dumps(payload).encode()))
        return _Resp(_ok_payload())
    fake_urlopen.bodies = []
    return fake_urlopen


def test_chat_drops_rejected_extra_field_and_retries(monkeypatch):
    d = _driver(monkeypatch, {"thinking": {"type": "enabled"}, "reasoning_effort": "low"})
    fake = _reject("thinking", _GO_THINKING_400)
    monkeypatch.setattr("urllib.request.urlopen", fake)

    resp = d.chat([{"role": "user", "content": "hi"}])

    assert not resp.error
    assert resp.text == "ok"
    assert "thinking" in fake.bodies[0]
    assert "thinking" not in fake.bodies[-1]
    assert fake.bodies[-1]["reasoning_effort"] == "low"
    assert d.extra_body == {"reasoning_effort": "low"}


def test_chat_stream_drops_rejected_extra_field_and_retries(monkeypatch):
    d = _driver(monkeypatch, {"thinking": {"type": "enabled"}, "reasoning_effort": "low"})
    fake = _reject("thinking", _GO_THINKING_400)
    monkeypatch.setattr("urllib.request.urlopen", fake)

    resp = d.chat_stream([{"role": "user", "content": "hi"}], on_delta=lambda _s: None)

    assert not resp.error
    assert resp.text == "ok"
    assert "thinking" not in fake.bodies[-1]
    assert "thinking" not in d.extra_body


def test_rejected_core_field_is_not_dropped(monkeypatch):
    d = _driver(monkeypatch, {})
    payload = {"error": {"param": "messages", "message": 'unknown field "messages"'}}
    fake = _reject("messages", payload)
    monkeypatch.setattr("urllib.request.urlopen", fake)

    resp = d.chat([{"role": "user", "content": "hi"}])

    assert resp.error and "400" in resp.error
    assert len(fake.bodies) == 1
