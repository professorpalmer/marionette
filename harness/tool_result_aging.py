"""Age old large tool results between compactions, only on a cold prompt cache.

A trim breaks the cached prompt prefix. When the cache is already cold the
next request rewrites it anyway, so the trim is free. Aged results are never
restored, so later requests send the same bytes.
"""
from __future__ import annotations

import os
import re
import time
from typing import Any, Callable, FrozenSet, List, Optional, Tuple

AGED_PREFIX = "[aged tool result: "
DEFAULT_KEEP_RECENT_ROUNDS = 6
DEFAULT_MIN_CHARS = 1500
DEFAULT_CACHE_TTL_SECONDS = 300.0

_SPILL_URI_RE = re.compile(r"spill://[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")

Recover = Callable[[str, str], Optional[str]]


def aging_enabled() -> bool:
    raw = os.environ.get("HARNESS_TOOL_RESULT_AGING", "").strip().lower()
    return raw not in ("0", "off", "false", "no")


def _tool_names(history: List[dict]) -> dict:
    names = {}
    for message in history:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            call_id = call.get("id")
            name = (call.get("function") or {}).get("name") or call.get("name")
            if call_id and name:
                names[call_id] = str(name)
    return names


def _first_kept_index(history: List[dict], keep_recent_rounds: int) -> int:
    rounds = [
        i for i, m in enumerate(history)
        if m.get("role") == "assistant" and m.get("tool_calls")
    ]
    if keep_recent_rounds <= 0:
        return len(history)
    if len(rounds) <= keep_recent_rounds:
        return 0
    return rounds[-keep_recent_rounds]


def aged_stub(tool: str, chars: int, handle: Optional[str]) -> str:
    if handle:
        return f"{AGED_PREFIX}{tool} returned {chars} chars. Full text: read_file {handle}]"
    return (
        f"{AGED_PREFIX}{tool} returned {chars} chars. "
        f"Not stored; run {tool} again if you need it.]"
    )


def age_tool_results(
    history: List[dict],
    *,
    keep_recent_rounds: int = DEFAULT_KEEP_RECENT_ROUNDS,
    min_chars: int = DEFAULT_MIN_CHARS,
    already_aged: FrozenSet[str] = frozenset(),
    recover: Optional[Recover] = None,
) -> Tuple[List[dict], FrozenSet[str]]:
    """Return ``(new_history, newly_aged_ids)``. The input list is not mutated.

    ``recover(content, tool_call_id)`` may persist the body and return a
    ``spill://`` handle. An existing ``spill://`` in the content wins.
    """
    cutoff = _first_kept_index(history, keep_recent_rounds)
    names = _tool_names(history)
    out = list(history)
    aged = set()
    for i in range(cutoff):
        message = history[i]
        if message.get("role") != "tool":
            continue
        call_id = message.get("tool_call_id") or ""
        content = message.get("content")
        if not call_id or call_id in already_aged or not isinstance(content, str):
            continue
        if len(content) < min_chars or content.startswith(AGED_PREFIX):
            continue
        match = _SPILL_URI_RE.search(content)
        handle = match.group(0) if match else None
        if handle is None and recover is not None:
            try:
                handle = recover(content, call_id)
            except Exception:
                handle = None
        tool = names.get(call_id) or message.get("name") or "the tool"
        replaced = dict(message)
        replaced["content"] = aged_stub(str(tool), len(content), handle)
        out[i] = replaced
        aged.add(call_id)
    return out, frozenset(aged)


def cache_is_cold(session: Any, now: float) -> bool:
    """Cold only when known cold. Unknown or warm returns False."""
    last = getattr(session, "_last_provider_request_at", None)
    if last is None:
        return False
    try:
        snapshot_fn = getattr(getattr(session, "pilot", None), "cache_snapshot", None)
        if callable(snapshot_fn):
            snapshot = snapshot_fn()
            if snapshot is None:
                return True
            if not snapshot.reason:
                return now >= float(snapshot.started) + float(snapshot.ttl_seconds)
        return now - float(last) >= DEFAULT_CACHE_TTL_SECONDS
    except Exception:
        return False


def maybe_age_session_tool_results(
    session: Any, *, clock: Callable[[], float] = time.monotonic,
) -> Optional[dict]:
    """Age the session history in place on a cold cache. Never raises.

    Returns the event payload when results were aged, else None.
    """
    try:
        if not aging_enabled() or not cache_is_cold(session, clock()):
            return None
        lock = session._compaction_checkpoint_lock()
        with lock:
            return _age_session_locked(session)
    except Exception:
        return None


def _age_session_locked(session: Any) -> Optional[dict]:
    history = session._history
    already = frozenset(getattr(session, "_tool_results_aged", None) or ())
    register = getattr(session, "_register_summary_spill", None)
    recover = None
    if callable(register):
        def recover(content: str, call_id: str) -> Optional[str]:
            return register(content, call_id, preview_chars=200)
    new_history, aged_ids = age_tool_results(
        history[1:], already_aged=already, recover=recover,
    )
    if not aged_ids:
        return None
    before_chars = sum(len(m.get("content") or "") for m in history[1:] if m.get("role") == "tool")
    after_chars = sum(len(m.get("content") or "") for m in new_history if m.get("role") == "tool")
    try:
        heuristic_before = session._estimate_context_tokens_for_list(history)
    except Exception:
        heuristic_before = 0
    before_tokens = int(session._estimate_context_tokens() or 0)
    history[1:] = new_history
    session._tool_results_aged = set(already) | set(aged_ids)
    session._invalidate_ctx_cache()
    try:
        delta = max(0, heuristic_before - session._estimate_context_tokens_for_list(history))
        real = int(getattr(session, "_last_prompt_tokens", 0) or 0)
        baseline = int(getattr(session, "_last_prompt_heuristic", 0) or 0)
        if real > 0:
            session._last_prompt_tokens = max(0, real - delta)
        if baseline > 0:
            session._last_prompt_heuristic = max(0, baseline - delta)
    except Exception:
        pass
    after_tokens = int(session._estimate_context_tokens() or 0)
    reclaimed = max(0, before_chars - after_chars)
    return {
        "mode": "tool_result_aging",
        "reason": "tool_results_aged",
        "summarized_messages": 0,
        "aged_results": len(aged_ids),
        "chars_reclaimed": reclaimed,
        "before_tokens": before_tokens,
        "after_tokens": after_tokens,
        "message": f"Aged {len(aged_ids)} old tool result(s) on a cold cache ({reclaimed} chars)",
    }
