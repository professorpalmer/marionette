from __future__ import annotations

from types import SimpleNamespace

from harness.terminal_empty_recovery import (
    RETRY_PROMPT,
    empty_after_tools_decision,
    inject_empty_retry,
    last_batch_had_error,
    note_tool_batch,
    reset_terminal_empty_recovery,
)


def test_empty_after_success_retries_once_then_fails():
    session = SimpleNamespace(_history=[], _last_tool_batch="none")
    reset_terminal_empty_recovery(session)
    note_tool_batch(session, had_actions=True, had_error=False)
    assert empty_after_tools_decision(session, True) == "ok"
    assert empty_after_tools_decision(session, False) == "retry"
    assert inject_empty_retry(session) == RETRY_PROMPT
    assert session._history[-1]["content"] == RETRY_PROMPT
    assert empty_after_tools_decision(session, False) == "fail"


def test_empty_without_prior_tools_counts():
    session = SimpleNamespace()
    reset_terminal_empty_recovery(session)
    assert empty_after_tools_decision(session, False) == "count"


def test_error_batch_does_not_retry():
    session = SimpleNamespace()
    note_tool_batch(session, had_actions=True, had_error=True)
    assert empty_after_tools_decision(session, False) == "count"


def test_last_batch_had_error_scans_tool_rows():
    session = SimpleNamespace(_history=[
        {"role": "assistant", "content": ""},
        {"role": "tool", "content": "ok", "is_error": False},
        {"role": "tool", "content": "boom", "is_error": True},
    ])
    assert last_batch_had_error(session, 2) is True
    session._history[-1]["is_error"] = False
    assert last_batch_had_error(session, 2) is False
    assert last_batch_had_error(session, 0) is False
