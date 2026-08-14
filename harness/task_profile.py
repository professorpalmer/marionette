from __future__ import annotations

"""Adaptive task-depth profiles: MICRO / STANDARD / DEEP.

Task depth is a separate classifier from repo-scale ``is_tiny_workspace``.
MICRO skips expensive orchestration (wiki auto-inject, CodeGraph auto-inject,
swarm-gate suppress) only — the safety kernel (path confinement, hash_edit,
sandbox, receipts) is never bypassed.
"""

import re
from typing import FrozenSet, Optional

MICRO = "MICRO"
STANDARD = "STANDARD"
DEEP = "DEEP"

_PROFILES = (MICRO, STANDARD, DEEP)
_OVERRIDE_ALIASES = {
    "micro": MICRO,
    "standard": STANDARD,
    "deep": DEEP,
    "auto": "auto",
}

# Broad / deep-intent language (audit, architecture, codebase-wide).
_DEEP_RE = re.compile(
    r"(?:"
    r"\baudit\b|"
    r"\bthroughout\b|"
    r"\brefactor\b|"
    r"\barchitecture\b|"
    r"\bfind\s+all\b|"
    r"\bacross\s+the\b|"
    r"\bcodebase[- ]wide\b|"
    r"\bacross\s+this\s+(?:codebase|repo|repository|project)\b"
    r")",
    re.IGNORECASE,
)

# MICRO cues: typo / rename-comment / one explicit filename with extension.
_TYPO_RE = re.compile(r"\btypo(?:s)?\b", re.IGNORECASE)
_RENAME_COMMENT_RE = re.compile(
    r"\brename\b.*\bcomment\b|\bcomment\b.*\brename\b",
    re.IGNORECASE,
)
_FILENAME_RE = re.compile(
    r"(?<![\w./\\-])"
    r"([A-Za-z0-9_.-]+\.[A-Za-z0-9]{1,12})"
    r"(?![\w./\\-])"
)

# Keep MICRO for short, local asks only.
_SHORT_MAX_CHARS = 120
_SHORT_MAX_WORDS = 16

# One-word pings / acks: never spend CodeGraph+wiki inject on "test".
_TRIVIAL_MICRO_RE = re.compile(
    r"^(?:test|testing|ok|okay|thanks|thank you|ping|pong|hi|hello|hey|yo|"
    r"sup|got it|nm|never mind)[.!?]*$",
    re.IGNORECASE,
)

_MICRO_VISIBLE: FrozenSet[str] = frozenset(
    {
        "read_file",
        "write_file",
        "edit_file",
        "hash_edit",
        "run_command",
        "run_ipython",
        "list_dir",
        "search_tools",
        "search_files",
    }
)

_RANK = {MICRO: 0, STANDARD: 1, DEEP: 2}


def normalize_profile(value: Optional[str]) -> Optional[str]:
    """Map lowercase/alias input to MICRO/STANDARD/DEEP, or 'auto', or None."""
    if value is None:
        return None
    key = str(value).strip().lower()
    if not key:
        return None
    if key in _OVERRIDE_ALIASES:
        return _OVERRIDE_ALIASES[key]
    upper = key.upper()
    if upper in _PROFILES:
        return upper
    return None


def classify_task_profile(message: str, override: Optional[str] = None) -> str:
    """Resolve MICRO / STANDARD / DEEP for a user turn.

    Explicit override wins when in {micro, standard, deep, auto}.
    ``auto`` / None falls through to deterministic heuristics.
    """
    normalized = normalize_profile(override)
    if normalized in _PROFILES:
        return normalized

    text = (message or "").strip()
    if _looks_deep(text):
        return DEEP
    if _looks_micro(text):
        return MICRO
    return STANDARD


def _looks_deep(text: str) -> bool:
    if not text:
        return False
    return bool(_DEEP_RE.search(text))


def _looks_micro(text: str) -> bool:
    if not text:
        return False
    if len(text) > _SHORT_MAX_CHARS:
        return False
    words = text.split()
    if len(words) > _SHORT_MAX_WORDS:
        return False
    if _TRIVIAL_MICRO_RE.match(text):
        return True
    if _TYPO_RE.search(text):
        return True
    if _RENAME_COMMENT_RE.search(text):
        return True
    filenames = _FILENAME_RE.findall(text)
    if len(filenames) == 1:
        return True
    return False


def maybe_escalate(
    profile: str,
    *,
    files_touched: int = 0,
    tests_failed: bool = False,
    broad_search: bool = False,
    user_wants_deep: bool = False,
) -> str:
    """Promote profile when the turn outgrows its lane. Never demotes."""
    current = normalize_profile(profile) or STANDARD
    if current not in _PROFILES:
        current = STANDARD

    nxt = current
    if nxt == MICRO:
        if files_touched >= 3 or broad_search or tests_failed:
            nxt = STANDARD
    if nxt == STANDARD:
        if user_wants_deep or files_touched >= 8:
            nxt = DEEP
    # Never demote: only move up the rank ladder.
    if _RANK.get(nxt, 0) < _RANK.get(current, 0):
        return current
    return nxt


def profile_skips_wiki(profile: Optional[str]) -> bool:
    return (normalize_profile(profile) or "") == MICRO


def profile_skips_codegraph(profile: Optional[str]) -> bool:
    return (normalize_profile(profile) or "") == MICRO


def profile_disables_swarm_gate(profile: Optional[str]) -> bool:
    return (normalize_profile(profile) or "") == MICRO


def micro_visible_tool_names() -> FrozenSet[str]:
    """Always-visible MICRO tool set (search_tools kept for lazy activation)."""
    return _MICRO_VISIBLE


def task_profile_usage_fields(
    profile: str = "",
    source: str = "",
    escalated_from: Optional[str] = None,
) -> dict:
    """Compact receipt fields for usage / context APIs (no parallel schema)."""
    resolved = normalize_profile(profile)
    if not resolved or resolved == "auto":
        raw = (profile or "").strip().upper()
        if raw not in _PROFILES:
            return {}
        resolved = raw
    out: dict = {
        "task_profile": resolved,
        "task_profile_source": (source or "").strip(),
    }
    if escalated_from:
        from_norm = normalize_profile(escalated_from) or str(escalated_from).strip().upper()
        if from_norm:
            out["escalated_from"] = from_norm
    return out
