"""Stub wording for unanswered tool calls.

A call that never ran is safe to run again. A call that started and lost its
result may have side effects, so the model must check state first. Without a
start record the stub keeps the old wording.
"""
from __future__ import annotations

import tempfile

import pytest

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession, _is_stub_tool_result
from harness.pilot import PilotAction


def _session(repo: str = "") -> ConversationalSession:
    state = tempfile.mkdtemp()
    cfg = HarnessConfig(driver="stub-oracle-v2", repo=repo, state_dir=state)
    return ConversationalSession(cfg)


def _dangling(tcid: str = "toolu_A") -> list:
    return [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "do it"},
        {"role": "assistant", "content": "acting", "tool_calls": [
            {"id": tcid, "type": "function",
             "function": {"name": "run_command", "arguments": "{}"}},
        ]},
    ]


def _results(s: ConversationalSession, tcid: str) -> list:
    return [
        m for m in s._history
        if m.get("role") == "tool" and m.get("tool_call_id") == tcid
    ]


def _stub_for(started) -> str:
    s = _session()
    s._history = _dangling()
    s._note_tool_calls_issued(["toolu_A"])
    if started:
        s._note_tool_call_started("toolu_A")
    s._sanitize_tool_pairs(reason="cancelled by the user (Stop)")
    return s._history[-1]["content"]


def test_not_started_call_says_rerun_is_safe():
    stub = _stub_for(started=False)
    assert stub.startswith("(no result:")
    assert "not started" in stub
    assert "did not run" in stub
    assert "safe to call it again" in stub
    assert "cancelled by the user (Stop)" in stub
    assert "outcome unknown" not in stub


def test_started_call_says_outcome_unknown_and_check_state():
    stub = _stub_for(started=True)
    assert stub.startswith("(no result:")
    assert "outcome unknown" in stub
    assert "Side effects may have happened" in stub
    assert "Check the current state before you call it again" in stub
    assert "cancelled by the user (Stop)" in stub
    assert "safe to call it again" not in stub


def test_unknown_start_state_keeps_old_wording():
    # No start record (old session or a restart): text is unchanged.
    s = _session()
    s._history = _dangling()
    s._sanitize_tool_pairs(reason="provider connection lost")
    assert s._history[-1]["content"] == (
        "(no result: interrupted — provider connection lost)"
    )
    s2 = _session()
    s2._history = _dangling()
    s2._sanitize_tool_pairs()
    assert s2._history[-1]["content"].startswith("(no result: interrupted — no recorded cause")


def test_record_for_another_id_does_not_change_wording():
    s = _session()
    s._history = _dangling("toolu_A")
    s._note_tool_calls_issued(["toolu_Z"])
    s._sanitize_tool_pairs(reason="x")
    assert s._history[-1]["content"] == "(no result: interrupted — x)"


@pytest.mark.parametrize("make_stub", [
    pytest.param(lambda: _stub_for(started=False), id="not_started"),
    pytest.param(lambda: _stub_for(started=True), id="outcome_unknown"),
    pytest.param(
        lambda: ConversationalSession._interruption_stub("toolu_A", "x")["content"],
        id="unknown_state",
    ),
])
def test_real_result_replaces_stub(make_stub):
    stub = {"role": "tool", "tool_call_id": "toolu_A", "content": make_stub()}
    assert _is_stub_tool_result(stub)

    # Append path: the real result replaces the stub in place.
    s = _session()
    s._history = _dangling() + [dict(stub)]
    act = PilotAction(
        kind="run_command", tool="run_command", tool_call_id="toolu_A", arguments={},
    )
    s._append_action_result(act, "a1", "real command output", is_native=True)
    got = _results(s, "toolu_A")
    assert [m["content"] for m in got] == ["real command output"]

    # Pairing path: a real result after the stub wins, and only one stays.
    s2 = _session()
    s2._history = _dangling() + [
        dict(stub),
        {"role": "tool", "tool_call_id": "toolu_A", "content": "late real"},
    ]
    s2._sanitize_tool_pairs()
    got2 = _results(s2, "toolu_A")
    assert [m["content"] for m in got2] == ["late real"]
