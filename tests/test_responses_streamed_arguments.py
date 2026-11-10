"""A Responses function call keeps its streamed arguments when the done item has none."""

from __future__ import annotations

import json

from pmharness.drivers.codex_responses import _consume_codex_sse, _extract_text_and_tools


def _line(event):
    return ("data: " + json.dumps(event) + "\n").encode("utf-8")


def _call(item_id, out_idx, name, arguments=""):
    return {"id": item_id, "type": "function_call", "call_id": "call_" + item_id,
            "name": name, "arguments": arguments}


_DONE = {"type": "response.completed", "response": {"status": "completed", "usage": {}}}


def _tools(events):
    raw = _consume_codex_sse([_line(e) for e in events + [_DONE]])
    _, tools, _ = _extract_text_and_tools(raw)
    return raw, {t["function"]["name"]: json.loads(t["function"]["arguments"]) for t in tools}


def test_done_item_with_empty_arguments_keeps_the_deltas():
    raw, tools = _tools([
        {"type": "response.output_item.added", "output_index": 0, "item": _call("fc_1", 0, "read_file")},
        {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "output_index": 0, "delta": '{"path": "a'},
        {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "output_index": 0, "delta": '.py"}'},
        {"type": "response.output_item.done", "output_index": 0, "item": _call("fc_1", 0, "read_file")},
    ])
    assert tools == {"read_file": {"path": "a.py"}}
    assert raw["output"][0]["arguments"] == '{"path": "a.py"}'


def test_arguments_done_event_wins_over_partial_deltas():
    _, tools = _tools([
        {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "output_index": 0, "delta": '{"pa'},
        {"type": "response.function_call_arguments.done", "item_id": "fc_1", "output_index": 0, "arguments": '{"path": "b.py"}'},
        {"type": "response.output_item.done", "output_index": 0, "item": _call("fc_1", 0, "read_file")},
    ])
    assert tools == {"read_file": {"path": "b.py"}}


def test_done_item_arguments_are_kept_when_present():
    _, tools = _tools([
        {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "output_index": 0, "delta": '{"path": "stale"}'},
        {"type": "response.output_item.done", "output_index": 0,
         "item": _call("fc_1", 0, "read_file", '{"path": "c.py"}')},
    ])
    assert tools == {"read_file": {"path": "c.py"}}


def test_parallel_calls_keep_their_own_arguments():
    _, tools = _tools([
        {"type": "response.function_call_arguments.delta", "output_index": 0, "delta": '{"path": "x.py"}'},
        {"type": "response.function_call_arguments.delta", "output_index": 1, "delta": '{"command": "ls"}'},
        {"type": "response.output_item.done", "output_index": 1, "item": _call("fc_2", 1, "run_command")},
        {"type": "response.output_item.done", "output_index": 0, "item": _call("fc_1", 0, "read_file")},
    ])
    assert tools == {"read_file": {"path": "x.py"}, "run_command": {"command": "ls"}}
