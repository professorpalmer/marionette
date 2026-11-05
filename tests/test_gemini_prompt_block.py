from __future__ import annotations

"""A Gemini prompt block is a terminal error, not an empty reply."""

import io
import json
from unittest.mock import patch

import pytest

from pmharness.drivers import error_classifier
from pmharness.drivers.gemini import GeminiDriver

_BLOCKED = {
    "promptFeedback": {
        "blockReason": "PROHIBITED_CONTENT",
        "blockReasonMessage": "The prompt was blocked by policy.",
    },
    "usageMetadata": {"promptTokenCount": 12},
}


def _driver(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-gemini-key")
    return GeminiDriver("gemini-3.5-flash", "gemini-3.5-flash")


def _json(payload):
    return io.BytesIO(json.dumps(payload).encode("utf-8"))


def _sse(chunks):
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks)
    return io.BytesIO(body.encode("utf-8"))


def _assert_blocked(resp, calls):
    assert calls == 1, "a blocked prompt is not sent again"
    assert resp.text == ""
    assert "blockReason PROHIBITED_CONTENT" in resp.error
    assert "blocked by policy" in resp.error
    assert resp.meta["finish_reason"] == "prompt_blocked"
    assert error_classifier.classify(None, resp.error) == error_classifier.ErrorClass.FATAL


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda d: d.chat([{"role": "user", "content": "hi"}]), id="chat"),
        pytest.param(lambda d: d.complete("hi"), id="complete"),
    ],
)
def test_nonstream_returns_the_block_reason(monkeypatch, call):
    d = _driver(monkeypatch)
    calls = []

    def fake(req, timeout=None):
        calls.append(req)
        return _json(_BLOCKED)

    with patch("urllib.request.urlopen", side_effect=fake):
        resp = call(d)
    _assert_blocked(resp, len(calls))


def test_stream_returns_the_block_reason(monkeypatch):
    d = _driver(monkeypatch)
    calls = []

    def fake(req, timeout=None):
        calls.append(req)
        return _sse([_BLOCKED])

    with patch("urllib.request.urlopen", side_effect=fake):
        resp = d.chat_stream([{"role": "user", "content": "hi"}])
    _assert_blocked(resp, len(calls))
    assert resp.tokens_in == 12


def test_feedback_beside_candidates_is_not_a_block(monkeypatch):
    d = _driver(monkeypatch)
    answer = {
        "promptFeedback": {"safetyRatings": []},
        "candidates": [{
            "content": {"parts": [{"text": "ok"}], "role": "model"},
            "finishReason": "STOP",
        }],
    }
    with patch("urllib.request.urlopen", return_value=_json(answer)):
        resp = d.chat([{"role": "user", "content": "hi"}])
    assert resp.error is None and resp.text == "ok"
