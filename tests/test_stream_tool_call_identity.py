"""Streamed tool calls join by call id before the wire index."""

from __future__ import annotations

import json

from pmharness.drivers.openai_compat import _OpenAIChatSseAccumulator


def _feed(fragments: list) -> dict:
    acc = _OpenAIChatSseAccumulator()
    for tc in fragments:
        chunk = {"choices": [{"delta": {"tool_calls": [tc]}}]}
        acc.feed(("data: " + json.dumps(chunk) + "\n").encode("utf-8"))
    return {
        slot: (c["id"], c["function"]["name"], c["function"]["arguments"])
        for slot, c in sorted(acc.assembled_tool_calls.items())
    }


def _start(index, call_id, name):
    return {"index": index, "id": call_id, "type": "function",
            "function": {"name": name, "arguments": ""}}


def _args(index, text, call_id=None):
    tc = {"index": index, "function": {"arguments": text}}
    if call_id:
        tc["id"] = call_id
    return tc


def test_two_calls_at_index_zero_with_new_ids_stay_two_calls():
    calls = _feed([
        _start(0, "call_a", "read_file"),
        _args(0, '{"path": "a.py"}'),
        _start(0, "call_b", "read_file"),
        _args(0, '{"path": "b.py"}'),
    ])
    assert list(calls.values()) == [
        ("call_a", "read_file", '{"path": "a.py"}'),
        ("call_b", "read_file", '{"path": "b.py"}'),
    ]


def test_standard_indexed_parallel_calls_are_unchanged():
    calls = _feed([
        _start(0, "call_a", "read_file"),
        _start(1, "call_b", "grep"),
        _args(0, '{"path": "a.py"}'),
        _args(1, '{"pattern": "x"}'),
    ])
    assert calls == {
        0: ("call_a", "read_file", '{"path": "a.py"}'),
        1: ("call_b", "grep", '{"pattern": "x"}'),
    }


def test_a_repeated_id_on_every_fragment_is_one_call():
    calls = _feed([
        _start(0, "call_a", "read_file"),
        _args(0, '{"path": ', "call_a"),
        _args(0, '"a.py"}', "call_a"),
    ])
    assert calls == {0: ("call_a", "read_file", '{"path": "a.py"}')}


def test_an_id_that_arrives_after_the_first_fragment_joins_that_call():
    calls = _feed([
        {"index": 0, "function": {"name": "read_file", "arguments": ""}},
        _args(0, '{"path": "a.py"}', "call_a"),
    ])
    assert calls == {0: ("call_a", "read_file", '{"path": "a.py"}')}
