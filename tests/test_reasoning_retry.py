"""OpenAI-compat driver must self-heal when an endpoint rejects `reasoning`.

Real report: a user's model returned HTTP 400 'Unknown parameter: reasoning'
and every pilot turn hard-failed. The driver sends the OpenRouter-style
`reasoning` field by default, which many OpenAI-compatible endpoints/models do
not accept. On that specific 400 the driver now disables reasoning for the
session and retries once, so the turn succeeds.

Hermetic: monkeypatches urlopen to 400 the first (reasoning-bearing) call and
200 the retry, asserting no reasoning field on the retry.
"""
import io
import json
import urllib.error

import pytest

from pmharness.drivers.openai_compat import OpenAICompatDriver


class _Resp:
    def __init__(self, payload):
        self._data = json.dumps(payload).encode()

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _ok_payload():
    return {
        "choices": [{"message": {"content": "hello", "tool_calls": []},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2},
    }


def test_chat_retries_without_reasoning_on_400(monkeypatch):
    monkeypatch.setenv("TEST_OAI_KEY", "k")
    d = OpenAICompatDriver("m", "some-model", "http://x/v1", "TEST_OAI_KEY",
                           enable_reasoning=True)
    seen_bodies = []

    def fake_urlopen(req, timeout=None):
        body = json.loads(req.data.decode())
        seen_bodies.append(body)
        if "reasoning" in body:
            raise urllib.error.HTTPError(
                "http://x", 400,
                'Unknown parameter: reasoning', {},
                io.BytesIO(json.dumps({"error": {
                    "message": "Unknown parameter: 'reasoning'.",
                    "code": "unknown_parameter", "param": "reasoning"}}).encode()))
        return _Resp(_ok_payload())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    resp = d.chat([{"role": "user", "content": "hi"}])
    assert resp.error is None or resp.error == "", f"unexpected error: {resp.error}"
    assert resp.text == "hello"
    # First body had reasoning (rejected); retry body must NOT.
    assert "reasoning" in seen_bodies[0]
    assert "reasoning" not in seen_bodies[-1]
    # Reasoning stays off for the rest of the session.
    assert d.enable_reasoning is False


def test_non_reasoning_400_still_errors(monkeypatch):
    monkeypatch.setenv("TEST_OAI_KEY", "k")
    d = OpenAICompatDriver("m", "some-model", "http://x/v1", "TEST_OAI_KEY",
                           enable_reasoning=True)

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(
            "http://x", 400, "bad", {},
            io.BytesIO(json.dumps({"error": {"message": "context length exceeded"}}).encode()))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    resp = d.chat([{"role": "user", "content": "hi"}])
    # A non-reasoning 400 is a real error and must surface (not silently retried).
    assert resp.error and "400" in resp.error


_EMPTY_MUSE_400_STUB = {
    "id": "chatcmpl_empty",
    "object": "chatcompletion",
    "choices": [{"index": 0, "message": {"role": "assistant"},
                 "finish_reason": None}],
}


def _muse_go_driver():
    d = OpenAICompatDriver(
        "m", "muse-spark-1.2-contributor",
        "https://opencode.ai/zen/go/v1", "TEST_OAI_KEY",
    )
    d._key = lambda: "fake-key"
    return d


def _track_chat(driver):
    seen = {"n": 0}
    real_chat = driver.chat

    def tracked_chat(*a, **k):
        seen["n"] += 1
        return real_chat(*a, **k)

    driver.chat = tracked_chat
    return seen


def _fallback_ok_payload():
    return {
        "choices": [{
            "message": {
                "content": "fallback text",
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "run_command", "arguments": "{}"},
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
    }


def test_empty_400_stub_detector_is_narrow():
    cls = OpenAICompatDriver
    stub = json.dumps(_EMPTY_MUSE_400_STUB)
    dotted = json.dumps({
        "object": "chat.completion",
        "choices": [{"message": {"role": "assistant"}, "finish_reason": ""}],
    })
    assert cls._is_empty_chat_completion_400_stub(400, stub)
    assert cls._is_empty_chat_completion_400_stub(400, dotted)
    assert not cls._is_empty_chat_completion_400_stub(401, stub)
    assert not cls._is_empty_chat_completion_400_stub(
        400, json.dumps({"error": {"message": "max_tokens is too large"}}),
    )
    assert not cls._is_empty_chat_completion_400_stub(
        400,
        json.dumps({
            "object": "chat.completion",
            "choices": [{
                "message": {"role": "assistant", "content": "provider detail"},
                "finish_reason": "stop",
            }],
        }),
    )


def test_chat_stream_empty_400_falls_back_once_to_chat(monkeypatch):
    """Exact Muse empty 400 before any delta retries once via chat()."""
    monkeypatch.setenv("TEST_OAI_KEY", "k")
    d = _muse_go_driver()
    chat_calls = _track_chat(d)
    seen_bodies = []

    def fake_urlopen(req, timeout=None):
        body = json.loads(req.data.decode())
        seen_bodies.append(body)
        if body.get("stream"):
            raise urllib.error.HTTPError(
                "https://opencode.ai/zen/go/v1", 400, "bad", {},
                io.BytesIO(json.dumps(_EMPTY_MUSE_400_STUB).encode()),
            )
        return _Resp(_fallback_ok_payload())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    resp = d.chat_stream(
        [{"role": "user", "content": "hey do a swarm"}],
        tools=[{"type": "function", "function": {"name": "run_swarm",
                                                 "parameters": {}}}],
        on_delta=lambda _t: None,
    )
    assert chat_calls["n"] == 1
    assert sum(1 for b in seen_bodies if b.get("stream")) == 1
    assert not seen_bodies[-1].get("stream")
    assert "stream_options" not in seen_bodies[-1]
    assert resp.error is None or resp.error == ""
    assert resp.text == "fallback text"
    assert resp.meta["tool_calls"][0]["function"]["name"] == "run_command"


def test_chat_stream_real_400_does_not_fallback_to_chat(monkeypatch):
    monkeypatch.setenv("TEST_OAI_KEY", "k")
    d = _muse_go_driver()
    chat_calls = _track_chat(d)

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(
            "https://opencode.ai/zen/go/v1", 400, "bad", {},
            io.BytesIO(json.dumps({
                "error": {"message": "max_tokens is too large"},
            }).encode()),
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    resp = d.chat_stream(
        [{"role": "user", "content": "hey do a swarm"}],
        tools=[{"type": "function", "function": {"name": "run_swarm",
                                                 "parameters": {}}}],
        on_delta=lambda _t: None,
    )
    assert chat_calls["n"] == 0
    assert resp.text == ""
    assert resp.error and "400" in resp.error
    assert "max_tokens is too large" in resp.error


def test_chat_stream_error_after_tool_delta_does_not_fallback(monkeypatch):
    monkeypatch.setenv("TEST_OAI_KEY", "k")
    d = _muse_go_driver()
    chat_calls = _track_chat(d)

    class _ToolThenError:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def __iter__(self):
            yield (
                b'data: {"choices":[{"delta":{"tool_calls":'
                b'[{"id":"call_1","function":{"name":"run_command"}}]}}]}\n'
            )
            raise urllib.error.HTTPError(
                "https://opencode.ai/zen/go/v1", 400, "bad", {},
                io.BytesIO(json.dumps(_EMPTY_MUSE_400_STUB).encode()),
            )

    def fake_urlopen(req, timeout=None):
        return _ToolThenError()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    resp = d.chat_stream(
        [{"role": "user", "content": "hey do a swarm"}],
        tools=[{"type": "function", "function": {"name": "run_command",
                                                 "parameters": {}}}],
        on_delta=lambda _t: None,
    )
    assert chat_calls["n"] == 0
    assert resp.error and "400" in resp.error
