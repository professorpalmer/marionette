from __future__ import annotations

"""Turn judgment. Fail-open, never raise, never log keys."""

import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import client as jev_client
from .questions import (
    DESC_CHARS,
    DEPTH_CRITERIA,
    DEPTH_INSTRUCTIONS,
    EXCERPT_CHARS,
    FITS_THRESHOLD,
    GATE_ACTS,
    GATE_ONELINER,
    GATE_PROCEDURE,
    GATE_PROSE,
    GATE_THRESHOLD,
    LANE_CRITERIA,
    LANE_INSTRUCTIONS,
    PLAYBOOK_INSTRUCTIONS,
    PRODUCT_PLAYBOOKS,
    RERANK_INSTRUCTIONS,
    SHORTLIST,
    SILLY_HUMANS,
    WHICH_INSTRUCTIONS,
    gate_score,
)
from .roster import RosterSkill, as_roster, skill_name

_TRIVIAL_ACK = re.compile(
    r"^(?:test|testing|ok|okay|thanks|thank you|ping|pong|hi|hello|hey|yo|"
    r"sup|got it|nm|never mind)[.!?]*$",
    re.IGNORECASE,
)


@dataclass
class Judgment:
    skill: Optional[str] = None
    skill_fits: float = 0.0
    playbook: str = ""
    depth: str = ""
    lane: str = ""
    silly_humans: float = 0.0
    gate: float = 0.0
    oneliner: float = 0.0
    wide_top: List[Tuple[str, float]] = field(default_factory=list)


_CACHE: Dict[Tuple[str, Tuple[str, ...]], Judgment] = {}

_OPT_IN = frozenset(("1", "true", "on", "yes"))


def opted_in() -> bool:
    """User asked for Jev. Empty, auto, and off stay off. Key is not enough."""
    raw = (os.environ.get("HARNESS_JEV") or "").strip().lower()
    return raw in _OPT_IN


def enabled() -> bool:
    """Opt-in plus a resolvable OpenRouter key. Never required for the harness."""
    if not opted_in():
        return False
    try:
        from harness.keys import inspect_mode

        if inspect_mode():
            return False
    except Exception:
        pass
    return bool(jev_client.resolve_openrouter_key())


def prefetch_turn_judgment(session: Any, user_message: str) -> Optional[Judgment]:
    """Judge once per turn and stash on the session. Fail-open."""
    try:
        if not enabled():
            setattr(session, "_jev_judgment", None)
            return None
        roster = getattr(session, "_retrievable_skills", None) or []
        judged = judge_turn(user_message, roster)
        setattr(session, "_jev_judgment", judged)
        return judged
    except Exception:
        try:
            setattr(session, "_jev_judgment", None)
        except Exception:
            pass
        return None


def suggestion_block(judgment: Optional[Judgment]) -> str:
    if judgment is None:
        return ""
    lines = []
    if judgment.skill:
        lines.append(
            "Relevant to the current request: %s. Ignore this if it does "
            "not fit what the user actually asked for." % judgment.skill
        )
    extras = []
    if judgment.lane and judgment.lane != "inline":
        extras.append("lane %s" % judgment.lane)
    playbook = (judgment.playbook or "").strip()
    if (
        playbook
        and playbook != "none"
        and judgment.depth != "MICRO"
        and judgment.oneliner < 0.5
    ):
        extras.append("playbook %s" % playbook)
    if judgment.depth == "DEEP":
        extras.append("depth DEEP")
    if extras:
        lines.append("Turn judgment: %s." % ", ".join(extras))
    return "\n".join(lines)


def _noul(answers: dict, key: str) -> float:
    row = answers.get(key) or {}
    if not isinstance(row, dict):
        return 0.0
    try:
        return float(row.get("noul") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _choice(answers: dict, key: str) -> str:
    row = answers.get(key) or {}
    if not isinstance(row, dict):
        return ""
    return str(row.get("choice") or "").strip()


def _probs(answers: dict, key: str) -> List[Tuple[str, float]]:
    row = answers.get(key) or {}
    if not isinstance(row, dict):
        return []
    raw = row.get("probabilities") or {}
    if not isinstance(raw, dict):
        return []
    ranked = []
    for name, value in raw.items():
        try:
            ranked.append((str(name), float(value)))
        except (TypeError, ValueError):
            continue
    ranked.sort(key=lambda item: -item[1])
    return ranked


def _skip_network(request: str) -> bool:
    text = (request or "").strip()
    if not text:
        return True
    if _TRIVIAL_ACK.match(text):
        return True
    try:
        from harness.task_profile import is_conversational_followup

        if is_conversational_followup(text):
            return True
    except Exception:
        pass
    return False


def _wide_questions(roster: List[RosterSkill]) -> dict:
    criteria = {}
    for skill in roster:
        desc = (skill.description or skill.name)[:DESC_CHARS]
        criteria[skill.name] = desc or skill.name
    if not criteria:
        criteria = {"none": "No skills are available"}
    return {
        "which": {
            "type": "choice",
            "instructions": WHICH_INSTRUCTIONS,
            "criteria": criteria,
        },
        "gate_acts": {"type": "noul", "instructions": GATE_ACTS},
        "gate_procedure": {"type": "noul", "instructions": GATE_PROCEDURE},
        "gate_prose": {"type": "noul", "instructions": GATE_PROSE},
        "gate_oneliner": {"type": "noul", "instructions": GATE_ONELINER},
        "playbook": {
            "type": "choice",
            "instructions": PLAYBOOK_INSTRUCTIONS,
            "criteria": dict(PRODUCT_PLAYBOOKS),
        },
        "depth": {
            "type": "choice",
            "instructions": DEPTH_INSTRUCTIONS,
            "criteria": dict(DEPTH_CRITERIA),
        },
        "silly_humans": {"type": "noul", "instructions": SILLY_HUMANS},
        "lane": {
            "type": "choice",
            "instructions": LANE_INSTRUCTIONS,
            "criteria": dict(LANE_CRITERIA),
        },
    }


def _rerank_questions(roster: List[RosterSkill], names: List[str]) -> dict:
    by_name = {skill.name: skill for skill in roster}
    criteria = {}
    questions = {}
    for name in names:
        skill = by_name.get(name)
        if skill is None:
            continue
        excerpt = "%s — %s" % (
            skill.description,
            (skill.body or "")[:EXCERPT_CHARS],
        )
        criteria[name] = excerpt.strip(" —")
        questions["fits_%s" % name] = {
            "type": "noul",
            "instructions": (
                "Does the skill '%s' do the specific thing `request` asks "
                "for? It is described as: %s" % (name, skill.description)
            ),
        }
    if not criteria:
        return {}
    questions["which"] = {
        "type": "choice",
        "instructions": RERANK_INSTRUCTIONS,
        "criteria": criteria,
    }
    return questions


def judge_turn(
    request: str,
    roster: Iterable[Any] = (),
    *,
    decide=None,
) -> Judgment:
    """Two-call skill suggestion plus lane/depth/playbook. Empty on skip/fail."""
    text = (request or "").strip()
    rows = as_roster(roster)
    names = tuple(skill.name for skill in rows)
    cache_key = (text, names)
    if cache_key in _CACHE:
        return _CACHE[cache_key]
    empty = Judgment()
    if not text or _skip_network(text):
        _CACHE[cache_key] = empty
        return empty
    if decide is None and not enabled():
        _CACHE[cache_key] = empty
        return empty
    poster = decide or jev_client.decide
    state = {"request": text, "recent_context": ""}
    wide = poster(state, _wide_questions(rows))
    if not wide:
        return empty
    answers = wide.get("answers") or {}
    ranked = _probs(answers, "which")
    oneliner = _noul(answers, "gate_oneliner")
    gate = gate_score(
        _noul(answers, "gate_acts"),
        _noul(answers, "gate_procedure"),
        _noul(answers, "gate_prose"),
        oneliner,
    )
    judgment = Judgment(
        playbook=_choice(answers, "playbook"),
        depth=_choice(answers, "depth"),
        lane=_choice(answers, "lane"),
        silly_humans=_noul(answers, "silly_humans"),
        gate=gate,
        oneliner=oneliner,
        wide_top=ranked[:5],
    )
    skip_skill = judgment.depth == "MICRO" or oneliner >= 0.5
    if gate >= GATE_THRESHOLD and ranked and not skip_skill:
        top = [name for name, _p in ranked[:SHORTLIST]]
        rerank_q = _rerank_questions(rows, top)
        if rerank_q:
            second = poster(state, rerank_q)
            if second:
                answers2 = second.get("answers") or {}
                fits = {name: _noul(answers2, "fits_%s" % name) for name in top}
                winner = _choice(answers2, "which") or (top[0] if top else "")
                best_name = max(fits, key=fits.get) if fits else winner
                best_fit = fits.get(best_name, 0.0)
                if best_fit >= FITS_THRESHOLD and best_name in {skill_name(s) for s in rows} | set(top):
                    judgment.skill = best_name
                    judgment.skill_fits = best_fit
    _CACHE[cache_key] = judgment
    return judgment


def peek_cached(request: str, roster: Iterable[Any] = ()) -> Optional[Judgment]:
    rows = as_roster(roster)
    return _CACHE.get(((request or "").strip(), tuple(s.name for s in rows)))


def clear_cache() -> None:
    _CACHE.clear()
