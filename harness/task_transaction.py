from __future__ import annotations

"""Lightweight per-turn task transaction (not a swarm).

Immutable-style helpers for tracking goal, touched files, and verification
across a single user ask. Intended for steer-merge context — never raises.
"""

from dataclasses import dataclass, field, replace
from typing import Any, Dict, Iterable, List, Optional, Sequence

PHASES = ("intake", "acting", "verifying", "done")
_PHASE_RANK = {name: idx for idx, name in enumerate(PHASES)}
_PASS_RESULTS = frozenset({"pass", "ok", "skipped"})


@dataclass(frozen=True)
class TaskTransaction:
    goal: str = ""
    constraints: List[str] = field(default_factory=list)
    plan: str = ""
    files: List[str] = field(default_factory=list)
    invariants: List[str] = field(default_factory=list)
    verification: str = ""
    phase: str = "intake"


def _safe_str(value: Any) -> str:
    try:
        if value is None:
            return ""
        return str(value)
    except Exception:
        return ""


def _normalize_phase(value: Any) -> str:
    phase = _safe_str(value).strip().lower()
    if phase in _PHASE_RANK:
        return phase
    return "intake"


def _at_least_phase(current: str, minimum: str) -> str:
    cur = _normalize_phase(current)
    floor = _normalize_phase(minimum)
    if _PHASE_RANK[cur] >= _PHASE_RANK[floor]:
        return cur
    return floor


def _unique_ordered(existing: Sequence[str], incoming: Iterable[Any]) -> List[str]:
    out: List[str] = []
    seen = set()
    for item in list(existing or []) + list(incoming or []):
        path = _safe_str(item).strip()
        if not path or path in seen:
            continue
        seen.add(path)
        out.append(path)
    return out


def new_transaction(user_message: str) -> TaskTransaction:
    """Start a transaction from a user message (goal truncated to 500 chars)."""
    goal = _safe_str(user_message).strip()[:500]
    return TaskTransaction(goal=goal, phase="intake")


def note_files(
    tx: Optional[TaskTransaction],
    paths: Optional[Iterable[Any]],
) -> TaskTransaction:
    """Append unique file paths; bump phase to at least acting."""
    base = tx if isinstance(tx, TaskTransaction) else TaskTransaction()
    files = _unique_ordered(base.files, paths or [])
    phase = _at_least_phase(base.phase, "acting")
    return replace(base, files=files, phase=phase)


def note_verification(
    tx: Optional[TaskTransaction],
    result: str,
) -> TaskTransaction:
    """Record a verification result; done on pass/ok/skipped, else verifying."""
    base = tx if isinstance(tx, TaskTransaction) else TaskTransaction()
    text = _safe_str(result).strip()
    key = text.lower()
    phase = "done" if key in _PASS_RESULTS else "verifying"
    return replace(base, verification=text, phase=phase)


def as_dict(tx: Optional[TaskTransaction]) -> Dict[str, Any]:
    """Serialize, omitting empty lists and empty strings."""
    if not isinstance(tx, TaskTransaction):
        return {}
    out: Dict[str, Any] = {}
    if tx.goal:
        out["goal"] = tx.goal
    if tx.constraints:
        out["constraints"] = list(tx.constraints)
    if tx.plan:
        out["plan"] = tx.plan
    if tx.files:
        out["files"] = list(tx.files)
    if tx.invariants:
        out["invariants"] = list(tx.invariants)
    if tx.verification:
        out["verification"] = tx.verification
    if tx.phase:
        out["phase"] = tx.phase
    return out


def context_block(tx: Optional[TaskTransaction]) -> str:
    """Compact markdown for steer merge; empty when only a goal is set."""
    if not isinstance(tx, TaskTransaction):
        return ""
    has_extra = bool(
        tx.constraints
        or tx.plan
        or tx.files
        or tx.invariants
        or tx.verification
    )
    if not has_extra:
        return ""
    lines: List[str] = ["## Task transaction"]
    if tx.goal:
        lines.append("goal: %s" % tx.goal)
    if tx.phase:
        lines.append("phase: %s" % tx.phase)
    if tx.plan:
        lines.append("plan: %s" % tx.plan)
    if tx.constraints:
        lines.append("constraints:")
        lines.extend("- %s" % item for item in tx.constraints)
    if tx.files:
        lines.append("files:")
        lines.extend("- %s" % path for path in tx.files)
    if tx.invariants:
        lines.append("invariants:")
        lines.extend("- %s" % item for item in tx.invariants)
    if tx.verification:
        lines.append("verification: %s" % tx.verification)
    return "\n".join(lines)
