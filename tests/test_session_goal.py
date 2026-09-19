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


def test_session_goal_output_token_cap_roundtrip():
    state_dir = tempfile.mkdtemp()
    store = SessionGoalStore(state_dir)
    goal = SessionGoal()
    goal.set("Cap the child", output_token_cap=512)
    assert goal.output_token_cap == 512
    store.save(goal)
    reloaded = store.load()
    assert reloaded.output_token_cap == 512
    assert "Output token cap: 512" in reloaded.context_block()
    goal.set("No cap", output_token_cap=0)
    assert goal.output_token_cap is None
    goal.set("Clear me", output_token_cap=128)
    goal.clear()
    assert goal.output_token_cap is None


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


def test_replacement_resets_accounting_but_resume_retains(monkeypatch):
    monkeypatch.setattr('harness.session_goal.time.time', lambda: 10.0)
    goal = SessionGoal().set('old', token_budget=100)
    goal.record_turn_usage(tokens=100, elapsed_seconds=3, continuation=True)
    assert goal.budget_exceeded
    goal.resume()
    assert (goal.token_count, goal.continuation_count, goal.elapsed_seconds) == (100, 1, 3)
    monkeypatch.setattr('harness.session_goal.time.time', lambda: 20.0)
    goal.set('replacement', token_budget=200)
    assert (goal.token_count, goal.continuation_count, goal.elapsed_seconds) == (0, 0, 0)
    assert goal.created_at == goal._active_since == 20.0
    assert not goal.budget_exceeded
    assert goal.token_budget == 200


def boot_goal_migration(tmp_path, active):
    import json
    from harness.sessions import SessionStore
    path = tmp_path / 'harness_sessions.json'
    path.write_text(json.dumps({'active': active, 'sessions': [{'id': 'A'}, {'id': 'B'}]}))
    sessions = SessionStore(str(path))
    SessionGoalStore.migrate_legacy(str(tmp_path), sessions)


def test_boot_migrates_legacy_once_to_durable_active(tmp_path):
    import json
    legacy = SessionGoalStore(str(tmp_path))
    legacy.save(SessionGoal().set('legacy goal'))
    original = json.loads((tmp_path / 'session_goal.json').read_text())
    boot_goal_migration(tmp_path, 'A')
    assert SessionGoalStore(str(tmp_path), session_id='A').load().text == 'legacy goal'
    boot_goal_migration(tmp_path, 'B')
    assert SessionGoalStore(str(tmp_path), session_id='B').load().text == ''
    assert SessionGoalStore(str(tmp_path), session_id='A').load().text == 'legacy goal'
    stamped = json.loads((tmp_path / 'session_goal.json').read_text())
    assert stamped.pop('session_id') == 'A'
    assert stamped == original


def test_boot_migration_existing_scoped_goal_wins(tmp_path):
    legacy = SessionGoalStore(str(tmp_path))
    legacy.save(SessionGoal().set('old goal'))
    scoped = SessionGoalStore(str(tmp_path), session_id='A')
    scoped.save(SessionGoal().set('new goal'))
    from pathlib import Path
    original = Path(scoped.path).read_bytes()
    boot_goal_migration(tmp_path, 'A')
    assert Path(scoped.path).read_bytes() == original
    boot_goal_migration(tmp_path, 'B')
    assert SessionGoalStore(str(tmp_path), session_id='B').load().text == ''


def test_boot_migration_unknown_active_preserves_original(tmp_path):
    legacy = SessionGoalStore(str(tmp_path))
    legacy.save(SessionGoal().set('unclaimed'))
    path = tmp_path / 'session_goal.json'
    original = path.read_bytes()
    for active in (None, '', 'missing'):
        boot_goal_migration(tmp_path, active)
        assert path.read_bytes() == original
        assert not (tmp_path / 'session_goals').exists()


def test_interrupted_migration_retries_original_owner(tmp_path, monkeypatch):
    import json
    import pytest
    legacy = SessionGoalStore(str(tmp_path))
    legacy.save(SessionGoal().set('alpha'))
    with monkeypatch.context() as patch:
        def fail_copy(*args):
            raise OSError('interrupted scoped copy')
        patch.setattr('harness.session_goal.os.link', fail_copy)
        with pytest.raises(OSError, match='interrupted scoped copy'):
            boot_goal_migration(tmp_path, 'A')
    assert json.loads((tmp_path / 'session_goal.json').read_text())['session_id'] == 'A'
    boot_goal_migration(tmp_path, 'B')
    assert SessionGoalStore(str(tmp_path), session_id='A').load().text == 'alpha'
    assert SessionGoalStore(str(tmp_path), session_id='B').load().text == ''
    assert not list(tmp_path.rglob('.session-goal-*'))


def test_ambiguous_active_row_does_not_claim_legacy(tmp_path):
    from types import SimpleNamespace
    legacy = SessionGoalStore(str(tmp_path))
    legacy.save(SessionGoal().set('unknown owner'))
    path = tmp_path / 'session_goal.json'
    original = path.read_bytes()
    sessions = SimpleNamespace(active='A', rows=lambda: [{'id': 'A'}, {'id': 'A'}])
    SessionGoalStore.migrate_legacy(str(tmp_path), sessions)
    assert path.read_bytes() == original
    assert not (tmp_path / 'session_goals').exists()
