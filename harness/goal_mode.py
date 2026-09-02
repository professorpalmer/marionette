from __future__ import annotations

"""Outer-loop Goal Mode: continue / complete / blocked around a fixed policy.

Semantic acceptance is separate from resource caps. No LLM judge.
"""

import os
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional, Sequence

from .swarm_run_facts import NOT_VERIFIED, VERIFIED


class GoalVerdict(str, Enum):
    CONTINUE = "continue"
    COMPLETE = "complete"
    BLOCKED = "blocked"


GOAL_MODE_SOURCE = "swarm_criteria"
GOAL_CONTINUE_PREFIX = "[goal-mode]"


@dataclass(frozen=True)
class GoalAssessment:
    verdict: GoalVerdict
    reason: str
    sources: tuple[str, ...] = ()


def goal_mode_continue_cap() -> int:
    try:
        return int(os.environ.get("HARNESS_GOAL_MODE_CONTINUE_MAX", "2"))
    except ValueError:
        return 2


def assess_swarm_goal(facts: Any) -> GoalAssessment:
    if facts is None:
        return GoalAssessment(
            GoalVerdict.COMPLETE, "no acceptance criteria", (),
        )
    criteria = tuple(getattr(facts, "criteria", None) or ())
    if not criteria:
        return GoalAssessment(
            GoalVerdict.COMPLETE, "no acceptance criteria", (),
        )
    unverified = []
    for item in criteria:
        status = str(getattr(item, "status", "") or "").strip().lower()
        if status == NOT_VERIFIED:
            text = str(getattr(item, "text", "") or "").strip()
            unverified.append(text or status)
    if unverified:
        return GoalAssessment(
            GoalVerdict.CONTINUE,
            "; ".join(unverified),
            (GOAL_MODE_SOURCE,),
        )
    if all(
        str(getattr(item, "status", "") or "").strip().lower() == VERIFIED
        for item in criteria
    ):
        return GoalAssessment(
            GoalVerdict.COMPLETE,
            "acceptance criteria verified",
            (GOAL_MODE_SOURCE,),
        )
    return GoalAssessment(
        GoalVerdict.COMPLETE, "no acceptance criteria", (),
    )


def assess_turn_swarm_goals(facts_list: Optional[Sequence[Any]] = None) -> GoalAssessment:
    rows = list(facts_list or ())
    if not rows:
        return GoalAssessment(
            GoalVerdict.COMPLETE, "no acceptance criteria", (),
        )
    continue_reasons = []
    saw_verified_criteria = False
    for facts in rows:
        assessment = assess_swarm_goal(facts)
        if assessment.verdict == GoalVerdict.CONTINUE:
            if assessment.reason:
                continue_reasons.append(assessment.reason)
        elif GOAL_MODE_SOURCE in assessment.sources:
            saw_verified_criteria = True
    if continue_reasons:
        return GoalAssessment(
            GoalVerdict.CONTINUE,
            "; ".join(continue_reasons),
            (GOAL_MODE_SOURCE,),
        )
    if saw_verified_criteria:
        return GoalAssessment(
            GoalVerdict.COMPLETE,
            "acceptance criteria verified",
            (GOAL_MODE_SOURCE,),
        )
    return GoalAssessment(
        GoalVerdict.COMPLETE, "no acceptance criteria", (),
    )


def assess_session_goal_continuation(
    *,
    goal_active: bool,
    budget_exceeded: bool,
    gate_blocks_idle: bool,
    swarm_assessment: GoalAssessment,
) -> GoalAssessment:
    """Whether sticky-goal auto-continue may enqueue after assistant_done."""
    if not goal_active:
        return GoalAssessment(
            GoalVerdict.COMPLETE, "session goal inactive", (),
        )
    if budget_exceeded:
        return GoalAssessment(
            GoalVerdict.BLOCKED, "token_budget", ("cap",),
        )
    if gate_blocks_idle:
        return GoalAssessment(
            GoalVerdict.BLOCKED, "quality_gate", ("quality_gate",),
        )
    if swarm_assessment.verdict == GoalVerdict.CONTINUE:
        return GoalAssessment(
            GoalVerdict.CONTINUE,
            swarm_assessment.reason,
            swarm_assessment.sources or (GOAL_MODE_SOURCE,),
        )
    if (
        swarm_assessment.verdict == GoalVerdict.COMPLETE
        and GOAL_MODE_SOURCE in swarm_assessment.sources
    ):
        return GoalAssessment(
            GoalVerdict.COMPLETE,
            swarm_assessment.reason or "acceptance criteria verified",
            swarm_assessment.sources,
        )
    return GoalAssessment(
        GoalVerdict.CONTINUE, "legacy session-goal drain", (),
    )


def reset_turn_goal_state(session: Any) -> int:
    """Clear per-turn Goal Mode state. Returns the continue cap."""
    try:
        session._turn_swarm_facts = []
        session._goal_mode_skip_continue = False
    except Exception:
        pass
    return goal_mode_continue_cap()


def maybe_inject_goal_continue(session: Any, *, iters: int, cap: int) -> bool:
    """Append a continue note when a prose-only step would otherwise finalize."""
    note = goal_continue_note(session, iters=iters, cap=cap)
    if not note:
        return False
    try:
        session._history.append({"role": "user", "content": note})
    except Exception:
        return False
    return True


def stash_turn_swarm_facts(
    session: Any,
    facts: Any,
    *,
    skip_continue: bool = False,
) -> None:
    """Best-effort stash; never raises onto the send path."""
    try:
        prior = list(getattr(session, "_turn_swarm_facts", None) or [])
        prior.append(facts)
        session._turn_swarm_facts = prior
        # Any continueable swarm in the turn wins over a prior skip-class row.
        if not skip_continue:
            session._goal_mode_skip_continue = False
        elif getattr(session, "_goal_mode_skip_continue", None) is not False:
            session._goal_mode_skip_continue = True
    except Exception:
        pass


def goal_continue_note(
    session: Any,
    *,
    iters: int,
    cap: int,
) -> Optional[str]:
    """User-role inject when a prose-only step would otherwise finalize."""
    if iters >= cap:
        return None
    if bool(getattr(session, "_goal_mode_skip_continue", False)):
        return None
    assessment = assess_turn_swarm_goals(
        getattr(session, "_turn_swarm_facts", None) or [],
    )
    if assessment.verdict != GoalVerdict.CONTINUE:
        return None
    reason = assessment.reason or "unverified acceptance criteria"
    return (
        f"{GOAL_CONTINUE_PREFIX} Acceptance criteria are not verified:\n"
        f"{reason}\n"
        "Keep working toward those criteria. "
        "Do not re-dispatch an identical swarm."
    )


def maybe_enqueue_session_goal_continuation(
    session: Any,
    *,
    gate_blocks_idle: bool,
) -> Optional[GoalAssessment]:
    """Enqueue sticky-goal continuation only on CONTINUE. Never raises."""
    try:
        if not bool(getattr(session.config, "goal_auto_continue", False)):
            return None
        goal = getattr(session, "_session_goal", None)
        assessment = assess_session_goal_continuation(
            goal_active=bool(goal is not None and goal.is_active()),
            budget_exceeded=bool(
                getattr(goal, "budget_exceeded", False)
            ) if goal is not None else False,
            gate_blocks_idle=bool(gate_blocks_idle),
            swarm_assessment=assess_turn_swarm_goals(
                getattr(session, "_turn_swarm_facts", None) or [],
            ),
        )
        if (
            assessment.verdict == GoalVerdict.CONTINUE
            and hasattr(session, "enqueue_goal_continuation")
        ):
            session.enqueue_goal_continuation()
        return assessment
    except Exception:
        return None
