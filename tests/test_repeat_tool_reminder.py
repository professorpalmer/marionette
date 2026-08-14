from __future__ import annotations

from types import SimpleNamespace

from harness.pilot import PilotAction
from harness.repeat_tool_reminder import (
    GENTLE_REMINDER,
    PLUGIN_SOURCE,
    canonicalize_call,
    note_repeat_and_maybe_nudge,
    observe_repeat,
    reset_repeat_chain,
)


def _session():
    return SimpleNamespace(_repeat_tool_chain=None)


def _act(kind="web_search", query="foo"):
    return PilotAction(kind=kind, query=query, arguments={"query": query})


def test_canonicalize_reuses_loop_guard_fingerprint():
    a = _act("web_search", "hello")
    b = _act("web_search", "hello")
    assert canonicalize_call("web_search", a) == canonicalize_call("web_search", b)


def test_nudge_at_three_five_eight_not_two():
    session = _session()
    act = _act()
    assert observe_repeat(session, "web_search", act) is None
    assert observe_repeat(session, "web_search", act) is None
    third = observe_repeat(session, "web_search", act)
    assert third is not None
    assert PLUGIN_SOURCE in third
    assert GENTLE_REMINDER in third
    assert observe_repeat(session, "web_search", act) is None
    fifth = observe_repeat(session, "web_search", act)
    assert fifth is not None
    assert "consecutive_calls: 5" in fifth
    assert observe_repeat(session, "web_search", act) is None
    assert observe_repeat(session, "web_search", act) is None
    eighth = observe_repeat(session, "web_search", act)
    assert eighth is not None
    assert "consecutive_calls: 8" in eighth


def test_different_tracked_tool_resets_chain():
    session = _session()
    first = _act("web_search", "a")
    other = _act("web_fetch", "https://example.com")
    other.url = "https://example.com"
    observe_repeat(session, "web_search", first)
    observe_repeat(session, "web_search", first)
    observe_repeat(session, "web_fetch", other)
    assert observe_repeat(session, "web_search", first) is None
    assert observe_repeat(session, "web_search", first) is None
    assert observe_repeat(session, "web_search", first) is not None


def test_untracked_tool_neither_counts_nor_resets(monkeypatch):
    monkeypatch.setenv("HARNESS_REPEAT_TOOL_EXCLUDE", "read_file")
    session = _session()
    search = _act("web_search", "q")
    read = PilotAction(kind="read_file", path="AGENTS.md", arguments={"path": "AGENTS.md"})
    observe_repeat(session, "web_search", search)
    observe_repeat(session, "web_search", search)
    assert observe_repeat(session, "read_file", read) is None
    third = observe_repeat(session, "web_search", search)
    assert third is not None
    assert GENTLE_REMINDER in third


def test_user_turn_resets_chain():
    session = _session()
    act = _act()
    observe_repeat(session, "web_search", act)
    observe_repeat(session, "web_search", act)
    reset_repeat_chain(session)
    assert observe_repeat(session, "web_search", act) is None
    assert observe_repeat(session, "web_search", act) is None
    assert observe_repeat(session, "web_search", act) is not None


def test_suffix_does_not_veto_and_lands_on_content():
    session = _session()
    act = _act()
    note_repeat_and_maybe_nudge(session, act, "ok")
    note_repeat_and_maybe_nudge(session, act, "ok")
    third = note_repeat_and_maybe_nudge(session, act, "ok")
    assert third.startswith("ok")
    assert PLUGIN_SOURCE in third
    assert GENTLE_REMINDER in third


def test_append_action_result_suffixes_nudge_without_extra_row(tmp_path):
    from harness.config import HarnessConfig
    from harness.conversation import ConversationalSession

    cfg = HarnessConfig(state_dir=str(tmp_path))
    session = ConversationalSession(cfg)
    act = PilotAction(
        kind="web_search",
        query="foo",
        arguments={"query": "foo"},
    )
    session._append_action_result(act, "c1", "ok-1", True)
    session._append_action_result(act, "c2", "ok-2", True)
    before = len(session._history)
    session._append_action_result(act, "c3", "ok-3", True)
    assert len(session._history) == before + 1
    last = session._history[-1]
    assert last["role"] == "tool"
    assert last["tool_call_id"] == "c3"
    assert PLUGIN_SOURCE in last["content"]
    assert GENTLE_REMINDER in last["content"]


def test_reminder_does_not_replace_loop_guard_veto():
    from harness.pilot_guards import (
        LOOP_REPEAT_CAP,
        TurnGuardState,
        check_loop_guard,
        record_action_execution,
        record_successful_result,
    )

    state = TurnGuardState(user_message="x")
    act = _act()
    record_action_execution(state, "web_search", act)
    record_successful_result(state, "web_search", act, "cached")
    record_action_execution(state, "web_search", act)
    verdict = check_loop_guard(state, "web_search", act)
    assert verdict.suppress is True
    assert LOOP_REPEAT_CAP >= 2
