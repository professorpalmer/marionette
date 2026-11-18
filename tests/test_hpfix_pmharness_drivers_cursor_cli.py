from __future__ import annotations

import json

from pmharness.drivers.cursor_cli import _tool_hint_payload, consume_stream_json


def test_generic_tool_name_falls_back_to_tool_call():
    assert _tool_hint_payload("tool", {}) == {"name": "tool_call"}
    assert _tool_hint_payload("unknown", {}, status="in_progress") == {
        "name": "tool_call",
        "status": "in_progress",
    }
    assert _tool_hint_payload("function", {}) == _tool_hint_payload("", {})


def test_stream_tool_wrapper_without_nested_name_hints_tool_call():
    hints: list[dict] = []
    line = json.dumps(
        {
            "type": "tool_call",
            "subtype": "started",
            "call_id": "c1",
            "tool_call": {"tool": {"args": {"path": "a.py"}}},
        }
    )
    consume_stream_json([line], on_tool_hint=hints.append)
    assert hints == [
        {"name": "tool_call", "goal": "a.py", "id": "c1", "status": "in_progress"}
    ]


def test_real_tool_names_keep_old_result():
    assert _tool_hint_payload("readToolCall", {"path": "a.py"}) == {
        "name": "read_file",
        "goal": "a.py",
    }
    assert _tool_hint_payload("tool", {"name": "Shell", "command": "ls"}) == {
        "name": "run_command",
        "goal": "ls",
    }
    assert _tool_hint_payload("", {}) == {"name": "tool_call"}
