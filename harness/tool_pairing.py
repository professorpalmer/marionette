from __future__ import annotations

"""Pairing-balanced compaction cuts.

A cut immediately before ``history[index]`` is safe only when the in-progress
tool-call count over ``history[:index]`` is 0. This is the DeepSeek
``toolPairingBalancedBefore`` contract, adapted to Marionette history dicts.

Distinct from ``ConversationalSession._sanitize_tool_pairs``, which heals
outbound wire-format 400s. This module only answers "may we cut here?"

Corrupt surfaces (a tool result with no open call) are unbalanced. Live
history must never raise.
"""

from typing import Any, List, Mapping, Sequence


def event_delta(msg: Mapping[str, Any]) -> int:
    """How one history message changes the in-progress tool-call count."""
    if not isinstance(msg, Mapping):
        return 0
    role = msg.get("role")
    if role == "assistant":
        calls = msg.get("tool_calls") or ()
        if isinstance(calls, (list, tuple)):
            return len(calls)
        return 0
    if role == "tool":
        return -1
    return 0


def in_progress_count(history_prefix: Sequence[Mapping[str, Any]]) -> int:
    """In-progress tool-call count after folding ``history_prefix``.

    Returns -1 when a tool result arrives with no matching open call
    (corrupt / unbalanced). Never raises.
    """
    count = 0
    for msg in history_prefix:
        count += event_delta(msg)
        if count < 0:
            return -1
    return count


def pairing_balanced_before(history: Sequence[Mapping[str, Any]], index: int) -> bool:
    """True when the cut immediately before ``history[index]`` is balanced."""
    if index < 0 or index > len(history):
        return False
    return in_progress_count(history[:index]) == 0


def pairing_balanced_after(history: Sequence[Mapping[str, Any]], index: int) -> bool:
    """True when the cut immediately after ``history[index]`` is balanced."""
    return pairing_balanced_before(history, index + 1)


def nearest_balanced_split(
    history: Sequence[Mapping[str, Any]],
    start_idx: int,
    *,
    min_idx: int = 2,
    orphan_in_kept=None,
) -> int:
    """Walk from ``start_idx`` to a pairing-balanced cut.

    Prefer advancing (keep an in-progress pair entirely in the tail). If the
    surface ends still unbalanced (mid-turn unanswered calls), walk backward
    so those open calls stay in the tail instead of being summarized.

    ``orphan_in_kept(idx)`` is an optional extra predicate (existing id-based
    orphan check). When provided, a candidate must also make it false.
    """
    n = len(history)
    if n < 2:
        return n
    if min_idx < 2:
        min_idx = 2
    split_idx = start_idx
    if split_idx < min_idx:
        split_idx = min_idx
    if split_idx > n:
        split_idx = n

    def _ok(idx: int) -> bool:
        if not pairing_balanced_before(history, idx):
            return False
        if orphan_in_kept is not None:
            try:
                if orphan_in_kept(idx):
                    return False
            except Exception:
                return False
        return True

    while split_idx < n and not _ok(split_idx):
        split_idx += 1
    if _ok(split_idx):
        return split_idx

    candidate = min(split_idx, n)
    while candidate > min_idx:
        candidate -= 1
        if _ok(candidate):
            return candidate
    if _ok(min_idx):
        return min_idx
    return split_idx


def orphan_tool_result_in_kept(
    history: List[Mapping[str, Any]],
    split_idx: int,
) -> bool:
    """True when a kept-tail tool result references a call still in the prefix.

    This is the existing ``_find_safe_split`` id check, extracted so pairing
    walk and the mixin share one definition.
    """
    middle_ids = set()
    for msg in history[1:split_idx]:
        for tc in msg.get("tool_calls") or ():
            if isinstance(tc, Mapping) and tc.get("id"):
                middle_ids.add(tc["id"])
    for msg in history[split_idx:]:
        if msg.get("role") == "tool" and msg.get("tool_call_id") in middle_ids:
            return True
    return False
