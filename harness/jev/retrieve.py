from __future__ import annotations

"""Per-turn skill body selection: Jev first, overlap fallback."""

from typing import Any, Iterable, List, Optional, Tuple

from ..skill_retrieve import (
    DEFAULT_MAX_COUNT,
    DEFAULT_TOKEN_BUDGET,
    _estimate_tokens,
    select_skill_bodies,
)
from .judge import Judgment, enabled, judge_turn
from .roster import skill_name


def _within_budget(skill: Any, budget: int) -> bool:
    body = str(getattr(skill, "body", "") or "")
    if not body.strip():
        return False
    cost = _estimate_tokens(body) or 1
    return cost <= budget


def _named(skills: Iterable[Any], name: str) -> Optional[Any]:
    want = (name or "").strip().lower()
    if not want:
        return None
    for skill in skills or ():
        if skill_name(skill).lower() == want:
            return skill
    return None


def select_turn_skills(
    query: str,
    skills: Iterable[Any],
    *,
    max_count: int = DEFAULT_MAX_COUNT,
    token_budget: int = DEFAULT_TOKEN_BUDGET,
    judgment: Optional[Judgment] = None,
    decide=None,
) -> Tuple[List[Any], Judgment]:
    """Return (bodies, judgment). Never raises.

    Disabled or Jev error → token overlap. Jev none → empty list (no overlap).
    Jev skill → that body if it fits the budget.
    """
    roster = list(skills or ())
    empty = Judgment()
    try:
        if not enabled():
            return select_skill_bodies(
                query, roster, max_count=max_count, token_budget=token_budget
            ), empty
        judged = judgment
        if judged is None:
            judged = judge_turn(query, roster, decide=decide)
        if judged.skill:
            hit = _named(roster, judged.skill)
            if hit is not None and _within_budget(hit, token_budget):
                return [hit], judged
            return [], judged
        if judged.gate or judged.depth or judged.lane or judged.playbook:
            return [], judged
        return select_skill_bodies(
            query, roster, max_count=max_count, token_budget=token_budget
        ), judged
    except Exception:
        try:
            return select_skill_bodies(
                query, roster, max_count=max_count, token_budget=token_budget
            ), empty
        except Exception:
            return [], empty
