from __future__ import annotations

from types import SimpleNamespace

from harness.goal_mode import (
    GOAL_CONTINUE_PREFIX,
    GOAL_MODE_SOURCE,
    GoalVerdict,
    assess_session_goal_continuation,
    assess_swarm_goal,
    assess_turn_swarm_goals,
    goal_continue_note,
    maybe_enqueue_session_goal_continuation,
    stash_turn_swarm_facts,
)
from harness.session_goal import SessionGoal
from harness.swarm_run_facts import NOT_VERIFIED, VERIFIED, CriterionFact, SwarmRunFacts


def _facts(*criteria: CriterionFact) -> SwarmRunFacts:
    return SwarmRunFacts(
        job_id="job_test",
        job_status="complete",
        subject_cwd="/tmp/repo",
        state_root="/tmp/state",
        marionette_version="0.9.400",
        puppetmaster_version="1.22.43",
        artifact_total=1,
        artifact_type_counts={"finding": 1},
        non_routing_total=1,
        direct_provenance_total=1,
        criteria=criteria,
    )


def _criterion(text: str, status: str) -> CriterionFact:
    return CriterionFact(text=text, status=status, basis="test")


def test_assess_swarm_goal_empty_and_none():
    empty = assess_swarm_goal(_facts())
    assert empty.verdict == GoalVerdict.COMPLETE
    assert empty.sources == ()
    none = assess_swarm_goal(None)
    assert none.verdict == GoalVerdict.COMPLETE
    assert none.sources == ()


def test_assess_swarm_goal_all_verified():
    assessment = assess_swarm_goal(_facts(
        _criterion("tests pass", VERIFIED),
        _criterion("diff exists", VERIFIED),
    ))
    assert assessment.verdict == GoalVerdict.COMPLETE
    assert GOAL_MODE_SOURCE in assessment.sources


def test_assess_swarm_goal_mixed_not_verified():
    assessment = assess_swarm_goal(_facts(
        _criterion("tests pass", VERIFIED),
        _criterion("windows path containment", NOT_VERIFIED),
    ))
    assert assessment.verdict == GoalVerdict.CONTINUE
    assert "windows path containment" in assessment.reason
    assert GOAL_MODE_SOURCE in assessment.sources


def test_assess_turn_swarm_goals_any_continue_wins():
    assessment = assess_turn_swarm_goals([
        _facts(_criterion("a", VERIFIED)),
        _facts(_criterion("b", NOT_VERIFIED)),
    ])
    assert assessment.verdict == GoalVerdict.CONTINUE
    assert "b" in assessment.reason


def test_session_goal_legacy_drain_without_swarm_sources():
    assessment = assess_session_goal_continuation(
        goal_active=True,
        budget_exceeded=False,
        gate_blocks_idle=False,
        swarm_assessment=assess_swarm_goal(_facts()),
    )
    assert assessment.verdict == GoalVerdict.CONTINUE
    assert assessment.sources == ()


def test_session_goal_skips_enqueue_when_criteria_verified():
    assessment = assess_session_goal_continuation(
        goal_active=True,
        budget_exceeded=False,
        gate_blocks_idle=False,
        swarm_assessment=assess_swarm_goal(_facts(_criterion("tests", VERIFIED))),
    )
    assert assessment.verdict == GoalVerdict.COMPLETE
    assert GOAL_MODE_SOURCE in assessment.sources


def test_session_goal_blocked_on_budget_and_gate():
    budget = assess_session_goal_continuation(
        goal_active=True,
        budget_exceeded=True,
        gate_blocks_idle=False,
        swarm_assessment=assess_swarm_goal(_facts()),
    )
    assert budget.verdict == GoalVerdict.BLOCKED
    gate = assess_session_goal_continuation(
        goal_active=True,
        budget_exceeded=False,
        gate_blocks_idle=True,
        swarm_assessment=assess_swarm_goal(_facts()),
    )
    assert gate.verdict == GoalVerdict.BLOCKED
    idle = assess_session_goal_continuation(
        goal_active=False,
        budget_exceeded=False,
        gate_blocks_idle=False,
        swarm_assessment=assess_swarm_goal(_facts()),
    )
    assert idle.verdict == GoalVerdict.COMPLETE


def test_goal_continue_note_and_skip_flag():
    session = SimpleNamespace(
        _turn_swarm_facts=[_facts(_criterion("windows", NOT_VERIFIED))],
        _goal_mode_skip_continue=False,
    )
    note = goal_continue_note(session, iters=0, cap=2)
    assert note is not None
    assert note.startswith(GOAL_CONTINUE_PREFIX)
    assert "windows" in note
    assert goal_continue_note(session, iters=2, cap=2) is None
    session._goal_mode_skip_continue = True
    assert goal_continue_note(session, iters=0, cap=2) is None


def test_stash_continueable_swarm_clears_skip_latch():
    session = SimpleNamespace()
    stash_turn_swarm_facts(
        session, _facts(_criterion("demo", NOT_VERIFIED)), skip_continue=True,
    )
    assert session._goal_mode_skip_continue is True
    stash_turn_swarm_facts(
        session, _facts(_criterion("windows", NOT_VERIFIED)), skip_continue=False,
    )
    assert session._goal_mode_skip_continue is False
    note = goal_continue_note(session, iters=0, cap=2)
    assert note is not None
    assert "windows" in note


def test_stash_turn_swarm_facts_appends_and_skip():
    session = SimpleNamespace()
    facts = _facts(_criterion("x", NOT_VERIFIED))
    stash_turn_swarm_facts(session, facts, skip_continue=True)
    assert session._turn_swarm_facts == [facts]
    assert session._goal_mode_skip_continue is True


def test_maybe_enqueue_verified_criteria_does_not_enqueue():
    session = SimpleNamespace(
        config=SimpleNamespace(goal_auto_continue=True),
        _session_goal=SessionGoal().set("Ship it"),
        _turn_swarm_facts=[_facts(_criterion("tests", VERIFIED))],
        enqueued=0,
    )

    def _enqueue():
        session.enqueued += 1
        return {}

    session.enqueue_goal_continuation = _enqueue
    maybe_enqueue_session_goal_continuation(session, gate_blocks_idle=False)
    assert session.enqueued == 0


def test_maybe_enqueue_empty_facts_still_drains():
    session = SimpleNamespace(
        config=SimpleNamespace(goal_auto_continue=True),
        _session_goal=SessionGoal().set("Ship it"),
        _turn_swarm_facts=[],
        enqueued=0,
    )

    def _enqueue():
        session.enqueued += 1
        return {}

    session.enqueue_goal_continuation = _enqueue
    maybe_enqueue_session_goal_continuation(session, gate_blocks_idle=False)
    assert session.enqueued == 1
