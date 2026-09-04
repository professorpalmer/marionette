"""Pure classifier table for provider / send-loop terminal causes."""
from __future__ import annotations

import pytest

from harness.terminal_cause import (
    TERMINAL_CONTENT_FILTER,
    TERMINAL_INCOMPLETE,
    TERMINAL_LENGTH,
    TERMINAL_NATURAL,
    TERMINAL_PROVIDER_EOF,
    TERMINAL_TOOL_CALLS,
    TERMINAL_TRANSPORT_ERROR,
    TERMINAL_UNSPECIFIED,
    blocking_terminal_message,
    classify_provider_terminal,
    finalize_stop_cause,
    provider_tools_are_executable,
)
from pmharness.drivers.base import DriverResponse


def _resp(text="", error=None, **meta):
    return DriverResponse(text=text, error=error, meta=meta)


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({"finish_reason": "stop"}, TERMINAL_NATURAL),
        ({"finish_reason": "tool_calls", "tool_calls": [
            {"id": "c1", "function": {"name": "read_file", "arguments": "{}"}},
        ]}, TERMINAL_TOOL_CALLS),
        ({"finish_reason": "length"}, TERMINAL_LENGTH),
        ({"finish_reason": "content_filter"}, TERMINAL_CONTENT_FILTER),
        ({"finish_reason": "end_turn"}, TERMINAL_NATURAL),
        ({"finish_reason": "max_tokens"}, TERMINAL_LENGTH),
        ({"finish_reason": "tool_use", "tool_calls": [
            {"id": "c1", "function": {"name": "read_file", "arguments": "{}"}},
        ]}, TERMINAL_TOOL_CALLS),
        ({"finish_reason": "completed"}, TERMINAL_NATURAL),
        ({"finish_reason": "incomplete", "incomplete_reason": "max_output_tokens"},
         TERMINAL_LENGTH),
        ({"finish_reason": "incomplete", "incomplete_reason": "content_filter"},
         TERMINAL_CONTENT_FILTER),
        ({"finish_reason": "incomplete"}, TERMINAL_INCOMPLETE),
        ({"finish_reason": "failed"}, TERMINAL_TRANSPORT_ERROR),
        ({"finish_reason": "error"}, TERMINAL_PROVIDER_EOF),
        ({"finish_reason": "STOP"}, TERMINAL_NATURAL),
        ({"finish_reason": "MAX_TOKENS"}, TERMINAL_LENGTH),
        ({"finish_reason": "SAFETY"}, TERMINAL_CONTENT_FILTER),
        ({"finish_reason": "RECITATION"}, TERMINAL_CONTENT_FILTER),
        ({"stream_terminal": "stop"}, TERMINAL_NATURAL),
        ({"stream_terminal": "tool_calls", "tool_calls": [
            {"id": "c1", "function": {"name": "x", "arguments": ""}},
        ]}, TERMINAL_TOOL_CALLS),
        ({"stream_terminal": "length", "finish_reason": "length"}, TERMINAL_LENGTH),
        ({"stream_terminal": "incomplete", "stream_started": True},
         TERMINAL_INCOMPLETE),
        ({"stream_terminal": "incomplete", "incomplete_reason": "max_output_tokens"},
         TERMINAL_LENGTH),
        ({"stream_terminal": "incomplete", "incomplete_reason": "length"},
         TERMINAL_LENGTH),
        ({"stream_terminal": "empty"}, TERMINAL_INCOMPLETE),
        ({"stream_terminal": "error"}, TERMINAL_TRANSPORT_ERROR),
        ({"finish_reason": "stop_sequence"}, TERMINAL_NATURAL),
        ({"finish_reason": "end_turn", "stop_reason": "end_turn"}, TERMINAL_NATURAL),
    ],
)
def test_classifier_table_known_dialects(kwargs, expected):
    classified = classify_provider_terminal(_resp("hello", **kwargs))
    assert classified.cause == expected
    if expected == TERMINAL_NATURAL:
        assert classified.is_affirmative_natural is True
        assert classified.blocks_clean_finalize is False
    else:
        assert classified.is_affirmative_natural is False


def test_missing_and_unknown_never_default_to_natural():
    missing = classify_provider_terminal(_resp("partial", stream_started=True))
    assert missing.cause == TERMINAL_PROVIDER_EOF
    assert missing.is_affirmative_natural is False
    assert missing.blocks_clean_finalize is True

    unknown = classify_provider_terminal(_resp("x", finish_reason="mystery_code"))
    assert unknown.cause == TERMINAL_UNSPECIFIED
    assert unknown.is_affirmative_natural is False
    assert unknown.blocks_clean_finalize is True

    empty = classify_provider_terminal(_resp(""))
    assert empty.cause == TERMINAL_UNSPECIFIED
    assert empty.is_affirmative_natural is False
    assert empty.blocks_clean_finalize is True

    text_only = classify_provider_terminal(_resp("hello there"))
    assert text_only.cause == TERMINAL_UNSPECIFIED
    assert text_only.is_affirmative_natural is False
    assert text_only.blocks_clean_finalize is True
    assert finalize_stop_cause(text_only) == TERMINAL_UNSPECIFIED
    assert finalize_stop_cause(empty) != TERMINAL_NATURAL
    assert finalize_stop_cause(None) == TERMINAL_UNSPECIFIED


def test_input_usage_and_context_percent_are_not_output_terminals():
    classified = classify_provider_terminal(_resp(
        "ok",
        finish_reason="stop",
        tokens_in=14000,
        context_used_pct=14,
        raw_usage={"prompt_tokens": 14000, "completion_tokens": 20},
        cache_read_tokens=9000,
    ))
    assert classified.cause == TERMINAL_NATURAL
    assert classified.is_affirmative_natural is True

    usage_only = classify_provider_terminal(_resp(
        "",
        tokens_in=14000,
        raw_usage={"prompt_tokens": 14000, "completion_tokens": 0},
        context_used_pct=14,
    ))
    assert usage_only.cause == TERMINAL_UNSPECIFIED
    assert usage_only.cause != TERMINAL_LENGTH
    assert usage_only.is_affirmative_natural is False
    assert usage_only.blocks_clean_finalize is True
    assert finalize_stop_cause(usage_only) == TERMINAL_UNSPECIFIED


def test_incomplete_tools_are_not_executable():
    resp = _resp(
        "",
        finish_reason="tool_calls",
        tool_calls=[],
        incomplete_tool_calls=[{
            "id": "c1",
            "function": {"name": "read_file", "arguments": '{"path":'},
        }],
    )
    classified = classify_provider_terminal(resp)
    assert classified.cause == TERMINAL_INCOMPLETE
    assert classified.allows_tool_execution is False
    assert provider_tools_are_executable(resp) is False


def test_truncated_args_in_tool_calls_cannot_execute():
    resp = _resp(
        "",
        finish_reason="tool_calls",
        tool_calls=[{
            "id": "c1",
            "function": {"name": "read_file", "arguments": '{"path":'},
        }],
    )
    assert provider_tools_are_executable(resp) is False
    assert classify_provider_terminal(resp).allows_tool_execution is False


def test_finish_reason_error_is_provider_eof_not_transport():
    classified = classify_provider_terminal(_resp(
        "",
        error="OpenAI chat finished with finish_reason=error",
        finish_reason="error",
        stream_terminal="incomplete",
        stream_started=True,
    ))
    assert classified.cause == TERMINAL_PROVIDER_EOF
    assert classified.finish_reason == "error"
    assert classified.blocks_clean_finalize is True
    assert classified.allows_tool_execution is False
    copy = blocking_terminal_message(classified)
    assert "connection" not in copy.lower()
    assert "lost" not in copy.lower()
    assert "Provider stream ended before a clean finish." in copy

    stream_err = classify_provider_terminal(_resp(
        "hello", error="HTTP 502: upstream", stream_terminal="error",
        stream_started=True,
    ))
    assert stream_err.cause == TERMINAL_TRANSPORT_ERROR


def test_transport_error_string_and_wave1_error_terminal():
    http = classify_provider_terminal(_resp("hello", error="HTTP 502: upstream"))
    assert http.cause == TERMINAL_TRANSPORT_ERROR
    assert http.blocks_clean_finalize is True

    stream_err = classify_provider_terminal(_resp(
        "hello", error="HTTP 502: upstream", stream_terminal="error",
        stream_started=True,
    ))
    assert stream_err.cause == TERMINAL_TRANSPORT_ERROR


def test_classifier_never_raises():
    class Boom:
        @property
        def meta(self):
            raise RuntimeError("nope")

        @property
        def error(self):
            raise RuntimeError("nope")

    classified = classify_provider_terminal(Boom())
    assert classified.cause == TERMINAL_UNSPECIFIED
    assert classified.is_affirmative_natural is False
