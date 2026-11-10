from __future__ import annotations

"""A credit-limited 402 that names an affordable output cap is sent once more with it."""

import io
import json
import urllib.error
import urllib.request

from pmharness.drivers.error_classifier import affordable_output_tokens
from pmharness.drivers.openai_compat import OpenAICompatDriver

_402 = ('{"error":{"message":"This request requires more credits, or fewer max_tokens. '
        'You requested up to 32000 tokens, but can only afford 8771.","code":402}}')


def _err(body, code=402):
    return urllib.error.HTTPError("https://x.invalid", code, "x", {}, io.BytesIO(body.encode()))


def _ok(text="done"):
    events = [
        {"choices": [{"index": 0, "delta": {"content": text}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 3, "completion_tokens": 1}},
    ]
    body = "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"
    return io.BytesIO(body.encode("utf-8"))


def _driver(max_tokens=32000):
    d = OpenAICompatDriver(
        name="openrouter:test", model="test/model", base_url="https://openrouter.ai/api/v1",
        api_key_env="TEST_OR_KEY", max_tokens=max_tokens,
    )
    d._key = lambda: "sk-or-test"
    return d


def _exchange(monkeypatch, replies):
    sent = []

    def urlopen(req, timeout=None, **_kw):
        sent.append(json.loads(req.data))
        reply = replies[len(sent) - 1]
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return sent


def test_parse_affordable_output_tokens():
    assert affordable_output_tokens(_402) == 8771
    assert affordable_output_tokens("HTTP 402: insufficient credits") is None


def test_402_is_sent_again_with_the_affordable_cap(monkeypatch):
    sent = _exchange(monkeypatch, [_err(_402), _ok()])
    resp = _driver().chat_stream([{"role": "user", "content": "hi"}], on_delta=lambda _t: None)
    assert resp.error is None and resp.text == "done"
    assert [b["max_tokens"] for b in sent] == [32000, 8771]


def test_second_402_is_returned(monkeypatch):
    sent = _exchange(monkeypatch, [_err(_402)] * 6)
    resp = _driver().chat_stream([{"role": "user", "content": "hi"}], on_delta=lambda _t: None)
    assert resp.error and "HTTP 402" in resp.error
    assert [b["max_tokens"] for b in sent] == [32000, 8771]


def test_tiny_affordable_cap_is_not_tried(monkeypatch):
    body = _402.replace("8771", "40")
    sent = _exchange(monkeypatch, [_err(body)] * 6)
    resp = _driver().chat_stream([{"role": "user", "content": "hi"}], on_delta=lambda _t: None)
    assert resp.error and "HTTP 402" in resp.error
    assert len(sent) == 1


def test_frozen_request_accepts_the_lower_cap(monkeypatch):
    from harness.request_snapshot import FrozenRequest

    sent = _exchange(monkeypatch, [_err(_402), _ok()])
    d = _driver()
    request = FrozenRequest.capture(d.chat_stream, [{"role": "user", "content": "hi"}],
                                    {"tools": [], "system": "sys"})
    messages, kwargs = request.materialize()
    resp = request.method(messages, on_delta=lambda _t: None, **kwargs)
    assert resp.error is None
    assert [b["max_tokens"] for b in sent] == [32000, 8771]
    assert request.wire_receipt()["wire_status"] == "verified"
