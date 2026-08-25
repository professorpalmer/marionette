from __future__ import annotations

"""Native-tool-turn recovery peeled from ``_send_locked_inner``.

Owns visible-name allowlist parsing, inline function-call fallback, and
synthetic tool_calls so history pairing stays valid. The send-loop kernel
still wraps unexpected exceptions as a native-parse error event and still
emits the three-strike halt after action results close pairs.
"""

import copy
import json
from typing import Any, Callable, Optional

from .pilot import (
    INVALID_ONLY_HALT_AFTER,
    PilotTurn,
    next_invalid_only_streak,
    parse_inline_tool_calls,
    parse_tool_calls,
    strip_inline_tool_calls,
    tool_names_from_schema,
)
from .tool_discovery import is_lazy_activatable_name

INVALID_ONLY_HALT_REASON = (
    "Stopped: 3 consecutive provider steps produced only "
    "invalid tool calls (auto-halt). Use a visible tool name "
    "from the current schema."
)

# Portable persisted identity for provider calls that omitted ``id``.
# Session-scoped monotonic suffix; never reuse an id already in history.
_SYNTHETIC_TOOL_CALL_PREFIX = "tc_syn_"


def _present_tool_call_id(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _history_tool_call_ids(history: Any) -> set[str]:
    used: set[str] = set()
    if not isinstance(history, list):
        return used
    for msg in history:
        if not isinstance(msg, dict):
            continue
        for tc in msg.get("tool_calls") or []:
            if isinstance(tc, dict):
                present = _present_tool_call_id(tc.get("id"))
                if present:
                    used.add(present)
        present = _present_tool_call_id(msg.get("tool_call_id"))
        if present:
            used.add(present)
    return used


def next_synthetic_tool_call_id(
    session: Any = None,
    used: Optional[set[str]] = None,
) -> str:
    """Return the next session-scoped portable synthetic tool_call id.

    Increments ``session._synthetic_tool_call_seq`` when a session is
    provided. Skips ids already present in ``used`` or session history so
    a rebuilt session cannot collide with persisted pairs.
    """
    taken = used if used is not None else set()
    n = 0
    if session is not None:
        taken.update(_history_tool_call_ids(getattr(session, "_history", None)))
        try:
            n = int(getattr(session, "_synthetic_tool_call_seq", 0) or 0)
        except (TypeError, ValueError):
            n = 0
    else:
        prefix = _SYNTHETIC_TOOL_CALL_PREFIX
        for item in taken:
            if isinstance(item, str) and item.startswith(prefix):
                tail = item[len(prefix):]
                if tail.isdigit():
                    n = max(n, int(tail))
    while True:
        n += 1
        candidate = f"{_SYNTHETIC_TOOL_CALL_PREFIX}{n}"
        if candidate not in taken:
            taken.add(candidate)
            if session is not None:
                session._synthetic_tool_call_seq = n
            return candidate


def assign_missing_native_tool_call_ids(
    tool_calls: list,
    allocate_id: Callable[[], str],
) -> list:
    """Deep-copy provider tool_calls and fill missing/empty ids.

    Existing nonempty ids are left unchanged. The input list and its
    nested dicts are never mutated (``resp.meta`` / raw payload stay intact).
    """
    copied = copy.deepcopy(tool_calls if isinstance(tool_calls, list) else [])
    for tc in copied:
        if not isinstance(tc, dict):
            continue
        if _present_tool_call_id(tc.get("id")):
            continue
        tc["id"] = allocate_id()
    return copied



def _tool_call_names(tool_calls: Any, content: str = "") -> list[str]:
    names: list[str] = []
    if isinstance(tool_calls, list):
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            raw = fn.get("name") if isinstance(fn, dict) else None
            if isinstance(raw, str) and raw.strip():
                names.append(raw.strip())
    text = content or ""
    if text:
        import re
        for match in re.finditer(
            r"<(?:function|tool_call)\s*=\s*([A-Za-z0-9_]+)", text
        ):
            names.append(match.group(1))
        for match in re.finditer(r'"name"\s*:\s*"([A-Za-z0-9_]+)"', text):
            names.append(match.group(1))
    out: list[str] = []
    seen: set[str] = set()
    for name in names:
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


def expand_allowed_names_for_lazy_activate(
    allowed_names: Optional[list],
    called_names: Any,
    catalog: Any = None,
) -> Optional[list]:
    """Allow first-call web_search / web_fetch / browser_* without schema exposure.

    Hidden tools stay out of the schema. search_tools activate is applied on
    the catalog (never cached as a no-op). ``allowed_names is None`` already
    means the full native set.
    """
    called = [str(n).strip() for n in (called_names or []) if str(n).strip()]
    lazy = [n for n in called if is_lazy_activatable_name(n)]
    if catalog is not None and lazy:
        catalog.try_lazy_activate(lazy)
    if allowed_names is None or not lazy:
        return allowed_names
    extra = [n for n in lazy if n not in allowed_names]
    if not extra:
        return allowed_names
    return list(allowed_names) + extra


def parse_native_tool_turn(
    pure_content: str,
    tool_calls: list,
    reasoning: str,
    schema: Any,
    session: Any = None,
) -> tuple[PilotTurn, list, str]:
    """Parse one native provider step into a PilotTurn.

    ``schema`` is the exact tools schema sent on this step (or None when
    unknown). Visible-name repair and invalid carriers use that allowlist.
    Inline function-call markup becomes synthetic tool_calls. Missing or
    empty provider tool_call ids receive one portable synthetic id on a
    deep copy *before* ``parse_tool_calls``, so assistant history and the
    PilotAction / tool result share that same identity. Raises on
    unexpected parse failures so the send loop can surface the same error.
    """
    allowed_names = (
        tool_names_from_schema(schema)
        if schema is not None
        else None
    )
    catalog = getattr(session, "_tool_catalog", None) if session is not None else None
    allowed_names = expand_allowed_names_for_lazy_activate(
        allowed_names,
        _tool_call_names(tool_calls, pure_content),
        catalog,
    )
    used = _history_tool_call_ids(
        getattr(session, "_history", None) if session is not None else None
    )
    if isinstance(tool_calls, list):
        for tc in tool_calls:
            if isinstance(tc, dict):
                present = _present_tool_call_id(tc.get("id"))
                if present:
                    used.add(present)

    def allocate_id() -> str:
        return next_synthetic_tool_call_id(session, used)

    if not tool_calls and pure_content:
        inline_actions = parse_inline_tool_calls(
            pure_content, allowed_names=allowed_names,
        )
        if inline_actions:
            synthetic_tool_calls = []
            for act in inline_actions:
                name = act.kind
                if act.kind == "call_mcp" and act.tool:
                    name = f"mcp_{act.tool.replace('.', '__')}"
                synthetic_tool_calls.append({
                    "id": act.tool_call_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(act.arguments),
                    },
                })
            tool_calls = assign_missing_native_tool_call_ids(
                synthetic_tool_calls, allocate_id,
            )
            id_by_index = [tc.get("id") or "" for tc in tool_calls]
            for idx, act in enumerate(inline_actions):
                if idx < len(id_by_index) and not _present_tool_call_id(
                    getattr(act, "tool_call_id", "")
                ):
                    act.tool_call_id = id_by_index[idx]
            actions = inline_actions
            pure_content = strip_inline_tool_calls(pure_content)
        else:
            actions = parse_tool_calls(
                tool_calls, allowed_names=allowed_names,
            )
    else:
        tool_calls = assign_missing_native_tool_call_ids(
            tool_calls, allocate_id,
        )
        actions = parse_tool_calls(
            tool_calls, allowed_names=allowed_names,
        )

    turn = PilotTurn(say=pure_content, thinking=reasoning, actions=actions)
    return turn, tool_calls, pure_content


def apply_invalid_only_streak(session: Any, turn: Any) -> int:
    """Advance or reset the session invalid-only streak from this step."""
    streak = next_invalid_only_streak(
        getattr(session, "_invalid_only_streak", 0),
        getattr(turn, "actions", None),
    )
    session._invalid_only_streak = streak
    return streak


def invalid_only_halt_reason(session: Any) -> Optional[str]:
    """Halt reason after action results close pairs, or None to continue."""
    if int(getattr(session, "_invalid_only_streak", 0) or 0) >= INVALID_ONLY_HALT_AFTER:
        return INVALID_ONLY_HALT_REASON
    return None
