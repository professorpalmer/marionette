"""A chunk that carries the last reasoning token and the first answer token emits reasoning first.

Recorded from abliteration.ai GLM-5.3 (vLLM): the reasoning tail "." rode in the
same delta as the answer start. Emitted after the answer, it painted as a new
Thought row under the answer.
"""
from __future__ import annotations

import json

from pmharness.drivers.openai_compat import _OpenAIChatSseAccumulator


def _line(delta):
    return "data: " + json.dumps({"choices": [{"index": 0, "delta": delta, "finish_reason": None}]})


def test_reasoning_tail_in_answer_chunk_emits_before_the_answer():
    events = []
    acc = _OpenAIChatSseAccumulator(
        on_delta=lambda text: events.append(("answer", text)),
        on_reasoning_delta=lambda text: events.append(("reasoning", text)),
    )
    for delta in (
        {"role": "assistant", "content": ""},
        {"reasoning": "Let me craft it well"},
        {"content": "The sky is blue because air", "reasoning": "."},
        {"content": " molecules scatter blue light."},
    ):
        assert acc.feed(_line(delta))
    assert events == [
        ("reasoning", "Let me craft it well"),
        ("reasoning", "."),
        ("answer", "The sky is blue because air"),
        ("answer", " molecules scatter blue light."),
    ]
