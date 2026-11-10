from __future__ import annotations

"""A Gemini text reply cut at MAX_TOKENS is continued, not left half done."""

import io
import json

from pmharness.drivers.base import LENGTH_CONTINUE_PROMPT
from pmharness.drivers.gemini import GeminiDriver


def _sse(text, finish, *, tool=False, out=5):
    part = {"functionCall": {"name": "read_file", "args": {}}} if tool else {"text": text}
    chunk = {
        "candidates": [{"content": {"parts": [part], "role": "model"}, "finishReason": finish}],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": out},
    }
    return io.BytesIO(f"data: {json.dumps(chunk)}\n\n".encode("utf-8"))


def _exchange(monkeypatch, replies):
    calls = []

    def urlopen(req, timeout=None, **_kw):
        calls.append(json.loads(req.data))
        return replies[len(calls) - 1]

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    return calls


def _driver(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-gemini-key")
    return GeminiDriver("gemini-3.5-flash", "gemini-3.5-flash")


def test_cut_text_reply_is_continued_and_joined(monkeypatch):
    d = _driver(monkeypatch)
    calls = _exchange(monkeypatch, [
        _sse("The answer is in two par", "MAX_TOKENS", out=8192),
        _sse("ts, and this is the end.", "STOP", out=12),
    ])
    deltas = []
    resp = d.chat_stream([{"role": "user", "content": "explain"}], on_delta=deltas.append)

    assert resp.text == "The answer is in two parts, and this is the end."
    assert deltas == ["The answer is in two par", "ts, and this is the end."]
    assert resp.error is None
    assert resp.meta["length_continues"] == 1
    assert resp.meta["finish_reason"] == "STOP"
    assert resp.tokens_out == 8192 + 12
    assert len(calls) == 2
    tail = calls[1]["contents"][-2:]
    assert tail[0]["role"] == "model"
    assert "The answer is in two par" in json.dumps(tail[0])
    assert tail[1]["role"] == "user"
    assert LENGTH_CONTINUE_PROMPT in json.dumps(tail[1])


def test_cut_tool_call_is_not_continued(monkeypatch):
    d = _driver(monkeypatch)
    calls = _exchange(monkeypatch, [_sse("", "MAX_TOKENS", tool=True)])
    resp = d.chat_stream([{"role": "user", "content": "read it"}])
    assert len(calls) == 1
    assert resp.meta["tool_calls"]
    assert "length_continues" not in resp.meta


def test_stop_prevents_the_continuation_request(monkeypatch):
    d = _driver(monkeypatch)
    calls = _exchange(monkeypatch, [_sse("Half an answ", "MAX_TOKENS")])
    resp = d.chat_stream([{"role": "user", "content": "explain"}], is_cancelled=lambda: True)
    assert len(calls) == 1
    assert resp.text == "Half an answ"
