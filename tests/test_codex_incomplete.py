"""Codex incomplete / content_filter / reasoning-only continuation helpers."""
from __future__ import annotations

from pmharness.drivers.codex_responses import (
    _CODEX_INCOMPLETE_NUDGE,
    _codex_continuation_kind,
    _consume_codex_sse,
    _extract_text_and_tools,
    _incomplete_reason,
)


def test_content_filter_maps_to_finish_reason():
    raw = {
        "status": "incomplete",
        "incomplete_details": {"reason": "content_filter"},
        "output": [],
        "output_text": "",
    }
    text, tools, finish = _extract_text_and_tools(raw)
    assert finish == "content_filter"
    assert text == ""
    assert tools == []
    assert _codex_continuation_kind(finish, text, tools) is None


def test_reasoning_only_incomplete_needs_nudge():
    assert _codex_continuation_kind("incomplete", "", []) == "nudge"
    assert _CODEX_INCOMPLETE_NUDGE.startswith("[System:")


def test_partial_text_incomplete_needs_length_continue():
    assert _codex_continuation_kind("incomplete", "partial answer", []) == "length"


def test_incomplete_with_tools_does_not_continue():
    tools = [{"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]
    assert _codex_continuation_kind("incomplete", "", tools) is None


def test_consume_sse_captures_incomplete_details():
    lines = [
        b'data: {"type":"response.incomplete","response":{"status":"incomplete","incomplete_details":{"reason":"content_filter"},"usage":{}}}\n',
    ]
    raw = _consume_codex_sse(lines)
    assert raw["status"] == "incomplete"
    assert _incomplete_reason(raw) == "content_filter"
    _, _, finish = _extract_text_and_tools(raw)
    assert finish == "content_filter"
