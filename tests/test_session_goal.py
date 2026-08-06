"""Persistent sticky session GOAL — distinct from Schedule.objective / Job.goal."""
from __future__ import annotations

import tempfile

from harness.config import HarnessConfig
from harness.schedule_core import Schedule
from harness.session_goal import SessionGoal, SessionGoalStore


def test_session_goal_sticky_across_save_load():
    state_dir = tempfile.mkdtemp()
    store = SessionGoalStore(state_dir)
    goal = SessionGoal()
    goal.set("Ship the quality gate")
    store.save(goal)

    reloaded = store.load()
    assert reloaded.text == "Ship the quality gate"
    assert reloaded.status == "active"
    assert reloaded.is_active()


def test_session_goal_complete_pause_clear():
    goal = SessionGoal()
    goal.set("Do the thing")
    assert goal.status == "active"

    goal.pause()
    assert goal.status == "paused"
    assert not goal.is_active()

    goal.resume()
    assert goal.status == "active"

    goal.complete()
    assert goal.status == "complete"
    assert not goal.is_active()

    goal2 = SessionGoal()
    goal2.set("Another")
    goal2.clear()
    assert goal2.status == "cleared"
    assert goal2.text == ""


def test_session_goal_distinct_from_schedule_objective():
    goal = SessionGoal()
    goal.set("Session sticky goal")
    schedule = Schedule(
        id="s1",
        name="nightly",
        objective="Schedule cron objective",
        cron="0 3 * * *",
    )
    assert goal.text != schedule.objective
    assert "objective" not in goal.to_dict()
    # Schedule.objective namespace must not be overloaded by SessionGoal fields.
    assert schedule.objective == "Schedule cron objective"


def test_session_goal_counters_increment():
    goal = SessionGoal()
    goal.set("Count me", token_budget=1000)
    goal.record_turn_usage(tokens=120, elapsed_seconds=1.5)
    goal.record_turn_usage(tokens=80, continuation=True)
    d = goal.to_dict()
    assert d["token_count"] == 200
    assert d["continuation_count"] == 1
    assert d["elapsed_seconds"] >= 1.5
    assert d["token_budget"] == 1000


def test_session_goal_on_conversational_session_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "harness.conversation.RuleStore",
        lambda *a, **k: __import__("harness.rule_store", fromlist=["RuleStore"]).RuleStore(
            path=str(tmp_path / "rules.json")
        ),
    )
    monkeypatch.setattr(
        "harness.memory_store.MEMORY_PATH",
        tmp_path / "mem.json",
    )
    state_dir = tempfile.mkdtemp()
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=state_dir)
    from harness.conversation import ConversationalSession

    session = ConversationalSession(cfg)
    session.set_session_goal("Persist across sessions")
    session.pause_session_goal()

    session2 = ConversationalSession(HarnessConfig(driver="stub-oracle-v2", state_dir=state_dir))
    d = session2.session_goal_dict()
    assert d["text"] == "Persist across sessions"
    assert d["status"] == "paused"


def test_session_goal_context_block_active_only():
    goal = SessionGoal()
    assert goal.context_block() == ""
    goal.set("Keep going")
    assert "SESSION GOAL" in goal.context_block()
    goal.complete()
    assert goal.context_block() == ""
