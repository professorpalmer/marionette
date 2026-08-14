from __future__ import annotations

from harness.compaction_mixin import CompactionContextMixin
from harness.tool_pairing import (
    event_delta,
    in_progress_count,
    nearest_balanced_split,
    pairing_balanced_after,
    pairing_balanced_before,
)


def _asst(*ids):
    return {
        "role": "assistant",
        "content": "call",
        "tool_calls": [
            {"id": i, "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
            for i in ids
        ],
    }


def _tool(call_id, content="ok"):
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def test_empty_and_no_tools_are_balanced():
    history = [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]
    assert in_progress_count([]) == 0
    assert pairing_balanced_before(history, 0)
    assert pairing_balanced_before(history, 1)
    assert pairing_balanced_before(history, 2)
    assert pairing_balanced_after(history, 1)


def test_two_calls_balanced_only_after_both_results():
    history = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "q"},
        _asst("a", "b"),
        _tool("a"),
        _tool("b"),
    ]
    assert event_delta(history[2]) == 2
    assert pairing_balanced_before(history, 2)
    assert not pairing_balanced_before(history, 3)
    assert not pairing_balanced_before(history, 4)
    assert pairing_balanced_before(history, 5)
    assert pairing_balanced_after(history, 4)


def test_cut_that_keeps_result_and_summarizes_call_is_unbalanced():
    history = [
        {"role": "system", "content": "s"},
        _asst("a"),
        {"role": "user", "content": "gap"},
        _tool("a"),
    ]
    # Cut before the result: call is in the prefix, result would stay in tail.
    assert not pairing_balanced_before(history, 3)
    assert pairing_balanced_before(history, 4)


def test_extra_result_without_call_is_unbalanced_no_raise():
    history = [
        {"role": "system", "content": "s"},
        _tool("orphan"),
    ]
    assert in_progress_count(history) == -1
    assert pairing_balanced_before(history, 2) is False


def test_nearest_split_advances_past_open_pair():
    history = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "q"},
        _asst("a"),
        _tool("a"),
        {"role": "assistant", "content": "done"},
    ]
    # Preferred cut between call and result.
    assert nearest_balanced_split(history, 3) == 4


def test_nearest_split_walks_back_for_unanswered_mid_turn_call():
    history = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "old"},
        _asst("open"),
    ]
    # No result yet: advancing hits the end still unbalanced; walk back so
    # the open call stays in the tail.
    assert nearest_balanced_split(history, 3) == 3
    assert pairing_balanced_before(history, 3)


class _SplitHost(CompactionContextMixin):
    def __init__(self, history):
        self._history = history


def test_find_safe_split_refuses_unanswered_call_in_prefix():
    history = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "old"},
        _asst("open"),
    ]
    host = _SplitHost(history)
    assert host._find_safe_split(3) == 3
