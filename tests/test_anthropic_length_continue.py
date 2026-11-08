from __future__ import annotations

"""An Anthropic text reply cut at max_tokens is continued, not left half done."""

import io
import json

from pmharness.drivers.anthropic import AnthropicDriver
from pmharness.drivers.base import LENGTH_CONTINUE_PROMPT


def _sse(text, stop_reason, *, tool=False, out=5):
    events = [{"type": "message_start", "message": {"usage": {"input_tokens": 10}}}]
    if tool:
        events.append({"type": "content_block_start", "index": 0, "content_block": {
            "type": "tool_use", "id": "toolu_1", "name": "write_file", "input": {}}})
        events.append({"type": "content_block_delta", "index": 0, "delta": {
            "type": "input_json_delta", "partial_json": '{"path": "a.py", "con'}})
    else:
        events.append({"type": "content_block_delta", "index": 0,
                       "delta": {"type": "text_delta", "text": text}})
    events.append({"type": "message_delta", "delta": {"stop_reason": stop_reason},
                   "usage": {"output_tokens": out}})
    body = "".join(f"data: {json.dumps(e)}\n\n" for e in events)
    return io.BytesIO(body.encode("utf-8"))


def _exchange(monkeypatch, replies):
    calls = []

    def urlopen(req, timeout=None, **kwargs):
        calls.append(json.loads(req.data))
        return replies[len(calls) - 1]

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    return calls


def _driver(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    return AnthropicDriver("anthropic:claude-opus-5-5", "claude-opus-5-5")


def test_cut_text_reply_is_continued_and_joined(monkeypatch):
    d = _driver(monkeypatch)
    calls = _exchange(monkeypatch, [
        _sse("The answer is in two par", "max_tokens", out=4096),
        _sse("ts, and this is the end.", "end_turn", out=12),
    ])
    deltas = []
    resp = d.chat_stream([{"role": "user", "content": "explain"}], on_delta=deltas.append)

    assert resp.text == "The answer is in two parts, and this is the end."
    assert deltas == ["The answer is in two par", "ts, and this is the end."]
    assert resp.error is None
    assert resp.meta["length_continues"] == 1
    assert resp.meta["finish_reason"] == "end_turn"
    assert resp.tokens_out == 4096 + 12
    assert len(calls) == 2
    tail = calls[1]["messages"][-2:]
    assert tail[0]["role"] == "assistant"
    assert "The answer is in two par" in json.dumps(tail[0]["content"])
    assert tail[1]["role"] == "user"
    assert LENGTH_CONTINUE_PROMPT in json.dumps(tail[1]["content"])


def test_cut_tool_call_is_not_continued(monkeypatch):
    d = _driver(monkeypatch)
    calls = _exchange(monkeypatch, [_sse("", "max_tokens", tool=True)])
    resp = d.chat_stream([{"role": "user", "content": "write it"}])
    assert len(calls) == 1
    assert resp.meta["tool_calls"]
    assert "length_continues" not in resp.meta


def test_stop_prevents_the_continuation_request(monkeypatch):
    d = _driver(monkeypatch)
    calls = _exchange(monkeypatch, [_sse("Half an answ", "max_tokens")])
    resp = d.chat_stream([{"role": "user", "content": "explain"}], is_cancelled=lambda: True)
    assert len(calls) == 1
    assert resp.text == "Half an answ"


def test_continuations_stop_at_the_limit(monkeypatch):
    d = _driver(monkeypatch)
    calls = _exchange(monkeypatch, [_sse(f"part {i} ", "max_tokens") for i in range(10)])
    resp = d.chat_stream([{"role": "user", "content": "explain"}])
    assert len(calls) == 4
    assert resp.meta["length_continues"] == 3
    assert resp.text == "part 0 part 1 part 2 part 3"


def test_frozen_request_sends_the_continuation_body(monkeypatch):
    from harness.request_snapshot import FrozenRequest

    d = _driver(monkeypatch)
    calls = _exchange(monkeypatch, [
        _sse("Part one", "max_tokens"),
        _sse(" part two", "end_turn"),
    ])
    request = FrozenRequest.capture(
        d.chat_stream, [{"role": "user", "content": "hi"}], {"tools": [], "system": "sys"},
    )
    messages, kwargs = request.materialize()
    resp = request.method(messages, on_delta=lambda _: None, **kwargs)

    assert resp.text == "Part one part two"
    assert len(calls) == 2
    assert [m["role"] for m in calls[1]["messages"]] == ["user", "assistant", "user"]
    assert calls[1]["messages"][0]["content"][0]["text"] == "hi"
    receipt = request.wire_receipt()
    assert receipt["wire_status"] == "verified"
    assert receipt["continuation_wire"][0]["wire_status"] == "verified"
