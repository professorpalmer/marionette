"""Plain router reasoning goes back to the model on the next tool call.

OpenRouter and the HF router stream a thinking model's reasoning as
``reasoning`` (with ``reasoning_details``), not ``reasoning_content``. The
send loop replays only ``reasoning_content``, so DeepSeek V4.1 through a
router lost its plan on every tool call. Live probe 2026-11-10 (OpenRouter,
DeepInfra fp8): the reasoning sent back in any field adds its own token count
to the next prompt, so the model reads it.
"""
from __future__ import annotations

import json

from pmharness.drivers.openai_compat import _OpenAIChatSseAccumulator, reasoning_is_sealed


def _line(delta, finish=None):
    return "data: " + json.dumps({"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})


def _feed(*deltas):
    acc = _OpenAIChatSseAccumulator()
    for delta in deltas:
        acc.feed(_line(delta))
    acc.feed(_line({}, finish="stop"))
    acc.feed("data: [DONE]")
    return acc.finalize()


def _text(text):
    return {"reasoning": text, "reasoning_details": [{"type": "reasoning.text", "text": text, "format": "unknown", "index": 0}]}


def test_plain_router_reasoning_becomes_reasoning_content():
    out = _feed(_text("Plan: read the"), _text(" file first."), {"content": "ok"})
    assert out["reasoning_content"] == "Plan: read the file first."


def test_reasoning_content_from_the_endpoint_wins():
    out = _feed({"reasoning_content": "native"}, {"content": "ok"})
    assert out["reasoning_content"] == "native"


def test_signed_or_encrypted_reasoning_is_not_replayed_as_text():
    signed = {"reasoning": "x", "reasoning_details": [{"type": "reasoning.text", "text": "x", "signature": "sig"}]}
    sealed = {"reasoning": "y", "reasoning_details": [{"type": "reasoning.encrypted", "data": "opaque"}]}
    assert _feed(signed, {"content": "ok"})["reasoning_content"] is None
    assert _feed(sealed, {"content": "ok"})["reasoning_content"] is None


def test_no_reasoning_gives_no_reasoning_content():
    assert _feed({"content": "ok"})["reasoning_content"] is None


def test_reasoning_is_sealed_reads_each_detail():
    assert not reasoning_is_sealed(None)
    assert not reasoning_is_sealed([{"type": "reasoning.text", "text": "a"}])
    assert reasoning_is_sealed([{"type": "reasoning.summary"}, {"type": "reasoning.encrypted"}])
