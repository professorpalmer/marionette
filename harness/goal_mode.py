from __future__ import annotations

"""Outer-loop Goal Mode: continue / complete / blocked around a fixed policy.

Semantic acceptance is separate from resource caps. No LLM judge.
"""

import os
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional, Sequence

from .swarm_run_facts import CriterionEvidence, NOT_VERIFIED, VERIFIED


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
    unresolved = [item for item in criteria if getattr(item, "status", "") != VERIFIED]
    if unresolved:
        actionable = [item for item in unresolved
                      if getattr(item, "status", "") == NOT_VERIFIED
                      and getattr(item, "evidence", None) == CriterionEvidence.FAILED]
        return GoalAssessment(
            GoalVerdict.CONTINUE if actionable else GoalVerdict.BLOCKED,
            "; ".join(str(getattr(item, "text", "") or "unverified criterion")
                      for item in unresolved),
            (GOAL_MODE_SOURCE,),
        )
    return GoalAssessment(
        GoalVerdict.COMPLETE, "acceptance criteria verified", (GOAL_MODE_SOURCE,),
    )


def _latest_job_facts(facts_list: Sequence[Any]) -> list[Any]:
    rows: list[Any] = []
    jobs: dict[str, int] = {}
    for facts in facts_list:
        job = str(getattr(facts, "job_id", "") or "").strip()
        if job and job in jobs:
            rows[jobs[job]] = facts
        else:
            if job:
                jobs[job] = len(rows)
            rows.append(facts)
    return rows


def assess_turn_swarm_goals(facts_list: Optional[Sequence[Any]] = None) -> GoalAssessment:
    assessments = [assess_swarm_goal(facts) for facts in _latest_job_facts(facts_list or ())]
    for verdict in (GoalVerdict.CONTINUE, GoalVerdict.BLOCKED):
        if any(a.verdict == verdict for a in assessments):
            return GoalAssessment(
                verdict,
                "; ".join(a.reason for a in assessments if a.verdict != GoalVerdict.COMPLETE),
                (GOAL_MODE_SOURCE,),
            )
    if any(GOAL_MODE_SOURCE in a.sources for a in assessments):
        return GoalAssessment(GoalVerdict.COMPLETE, "acceptance criteria verified", (GOAL_MODE_SOURCE,))
    return GoalAssessment(GoalVerdict.COMPLETE, "no acceptance criteria", ())


def _actionable_keys(session: Any) -> set[str]:
    return {
        " ".join(str(item.text).split()).casefold()
        for facts in _latest_job_facts(getattr(session, "_turn_swarm_facts", ()) or ())
        for item in getattr(facts, "criteria", ())
        if item.status == NOT_VERIFIED and getattr(item, "evidence", None) == CriterionEvidence.FAILED
    }


def _claim_correction(session: Any) -> set[str]:
    keys = _actionable_keys(session)
    seen = set(getattr(session, "_goal_mode_corrected", ()) or ())
    fresh = keys - seen
    session._goal_mode_corrected = seen | keys
    return fresh


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
    if swarm_assessment.verdict == GoalVerdict.BLOCKED:
        return swarm_assessment
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


_EXEC_TOOLS = frozenset({"run_command", "run_command_batch", "run_ipython"})
# History text of an envelope pilot turn that has no prose.
ACTING_PLACEHOLDER = "(acting)"


def turn_progress(history: Sequence[Any]) -> dict:
    """Read the last turn from history: rows after the last typed request."""
    from .working_set import is_user_request

    start = 0
    for index in range(len(history) - 1, -1, -1):
        if is_user_request(history[index]):
            start = index + 1
            break
    activity = succeeded = exec_failed = answered = False
    for row in history[start:]:
        if not isinstance(row, dict):
            continue
        role = row.get("role")
        if role == "assistant":
            if row.get("tool_calls"):
                activity = True
            content = row.get("content")
            if isinstance(content, str):
                answered = answered or content.strip() not in ("", ACTING_PLACEHOLDER)
            elif content:
                answered = True
        elif role == "user":
            # Envelope pilots get tool results back as user rows.
            activity = True
        elif role == "tool":
            activity = True
            if row.get("is_error") is True:
                if row.get("_spill_tool") in _EXEC_TOOLS:
                    exec_failed = True
            else:
                succeeded = True
    return {
        "tool_activity": activity,
        "tool_succeeded": succeeded,
        "exec_failed": exec_failed,
        "answered": answered,
    }


def reset_turn_goal_state(session: Any) -> int:
    """Clear per-turn Goal Mode state. Returns the continue cap."""
    try:
        session._turn_swarm_facts = []
        session._goal_mode_corrected = set()
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
        session._history.append({"role": "system", "content": note, "source": "goal_mode"})
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
        session._turn_swarm_facts = _latest_job_facts(prior)
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
    """Claim one automatic correction for newly demonstrated failed criteria."""
    if iters >= cap:
        return None
    if bool(getattr(session, "_goal_mode_skip_continue", False)):
        return None
    assessment = assess_turn_swarm_goals(
        getattr(session, "_turn_swarm_facts", None) or [],
    )
    if assessment.verdict != GoalVerdict.CONTINUE:
        return None
    fresh = _claim_correction(session)
    if not fresh:
        return None
    reason = "; ".join(sorted(fresh))
    return (
        f"{GOAL_CONTINUE_PREFIX} Current-job checks demonstrate failed criteria:\n"
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
            if GOAL_MODE_SOURCE in assessment.sources and not _claim_correction(session):
                return GoalAssessment(GoalVerdict.BLOCKED, "unchanged failed criteria already nudged", assessment.sources)
            session.enqueue_goal_continuation()
        return assessment
    except Exception:
        return None
