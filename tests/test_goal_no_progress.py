from __future__ import annotations

"""A sticky goal pauses after three turns in a row without progress.

Without a token budget, auto-continue enqueued a continuation after every
turn. A goal whose turns gave no answer and no tool call, or whose commands
failed each turn, looped until the user stopped it.
"""

from types import SimpleNamespace

from harness.goal_mode import maybe_enqueue_session_goal_continuation, turn_progress
from harness.session_goal import NO_PROGRESS_LIMIT, SessionGoal

_EMPTY = dict(tool_activity=False, tool_succeeded=False, exec_failed=False, answered=False)
_EXEC_FAIL = dict(tool_activity=True, tool_succeeded=False, exec_failed=True, answered=True)


def _goal():
    return SessionGoal().set("Ship it")


def test_empty_automatic_turns_pause_the_goal():
    goal = _goal()
    reasons = [goal.record_turn_outcome(automatic=True, **_EMPTY) for _ in range(NO_PROGRESS_LIMIT)]
    assert reasons[-1] == "empty_response"
    assert goal.status == "paused" and goal.blocked_reason == "empty_response"


def test_typed_turn_and_tool_activity_reset_the_empty_count():
    goal = _goal()
    goal.record_turn_outcome(automatic=True, **_EMPTY)
    goal.record_turn_outcome(automatic=True, **_EMPTY)
    goal.record_turn_outcome(automatic=False, **_EMPTY)
    goal.record_turn_outcome(automatic=True, **_EMPTY)
    goal.record_turn_outcome(automatic=True, **dict(_EMPTY, tool_activity=True))
    goal.record_turn_outcome(automatic=True, **_EMPTY)
    assert goal.status == "active"


def test_failed_commands_pause_unless_a_tool_succeeds():
    goal = _goal()
    goal.record_turn_outcome(automatic=True, **_EXEC_FAIL)
    goal.record_turn_outcome(automatic=True, **_EXEC_FAIL)
    goal.record_turn_outcome(automatic=True, **dict(_EXEC_FAIL, tool_succeeded=True))
    goal.record_turn_outcome(automatic=True, **_EXEC_FAIL)
    goal.record_turn_outcome(automatic=True, **_EXEC_FAIL)
    assert goal.status == "active"
    assert goal.record_turn_outcome(automatic=True, **_EXEC_FAIL) == "execution_failed"
    assert goal.status == "paused"


def test_resume_clears_the_counts_and_they_persist():
    goal = _goal()
    for _ in range(NO_PROGRESS_LIMIT):
        goal.record_turn_outcome(automatic=True, **_EMPTY)
    loaded = SessionGoal.from_dict(goal.to_dict())
    assert loaded.blocked_reason == "empty_response" and loaded.idle_turns == NO_PROGRESS_LIMIT
    loaded.resume()
    assert loaded.status == "active" and loaded.idle_turns == 0 and loaded.blocked_reason == ""


def test_paused_goal_does_not_enqueue_a_continuation():
    goal = _goal()
    for _ in range(NO_PROGRESS_LIMIT):
        goal.record_turn_outcome(automatic=True, **_EMPTY)
    session = SimpleNamespace(
        config=SimpleNamespace(goal_auto_continue=True),
        _session_goal=goal, _turn_swarm_facts=[], enqueued=0,
    )

    def _enqueue():
        session.enqueued += 1

    session.enqueue_goal_continuation = _enqueue
    maybe_enqueue_session_goal_continuation(session, gate_blocks_idle=False)
    assert session.enqueued == 0


def test_turn_progress_reads_only_the_last_turn():
    history = [
        {"role": "user", "content": "first", "_user_request": True},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "a"}]},
        {"role": "tool", "tool_call_id": "a", "content": "ok", "is_error": False},
        {"role": "user", "content": "Continue working toward the session goal", "_user_request": True},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "b"}]},
        {"role": "tool", "tool_call_id": "b", "content": "{}", "is_error": True,
         "_spill_tool": "run_command"},
        {"role": "assistant", "content": ""},
    ]
    assert turn_progress(history) == {
        "tool_activity": True, "tool_succeeded": False, "exec_failed": True, "answered": False,
    }
    assert turn_progress(history[:4] + [{"role": "assistant", "content": "  "}]) == _EMPTY


def test_send_pauses_a_goal_after_empty_continuations():
    import tempfile

    from harness.config import HarnessConfig
    from harness.conversation import ConversationalSession
    from test_post_swarm_synthesis import _SequencePilot, _pilot_envelope

    config = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    config.goal_auto_continue = True
    session = ConversationalSession(config)
    session.pilot = _SequencePilot([_pilot_envelope() for _ in range(NO_PROGRESS_LIMIT)])
    session._build_visible_tools_schema = lambda: []
    session._maybe_compact_history = lambda *a, **k: iter(())
    session._submit_housekeeping = lambda *a, **k: None
    session._session_goal = SessionGoal().set("Ship it")
    reasons = []
    for _ in range(NO_PROGRESS_LIMIT):
        prompt = session._session_goal.continuation_prompt()
        for item in session.list_prompts():
            session.remove_prompt(item["id"])
        events = list(session.send(prompt))
        reasons += [e.data.get("reason") for e in events if e.kind == "notice"]
    assert session._session_goal.status == "paused"
    assert "goal_no_progress" in reasons
    assert session.list_prompts() == []
