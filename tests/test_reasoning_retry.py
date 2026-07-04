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
