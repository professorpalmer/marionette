from __future__ import annotations

"""Advisory consecutive-identical tool-call reminder.

DeepSeek ``repeat-tool-reminder`` adapted to Marionette: canonicalize
(tool, args), count consecutive identical attempts (including loop-guard
replay/suppress), and suffix the tool-result content at 3/5/8. Never vetoes.

A separate history row would break native tool pairing. The nudge rides on
the result already being appended.
"""

import json
import os
import re
from typing import Any, Optional, Sequence, Tuple

PLUGIN_SOURCE = "repeat-tool-reminder"

DEFAULT_THRESHOLDS = (3, 5, 8)
DEFAULT_ARGUMENTS_PREVIEW_CHARS = 500

GENTLE_REMINDER = (
    "You are repeating the exact same tool call with identical arguments. "
    "Carefully analyze the previous result before calling again: if the task is "
    "not complete, try a different approach or different arguments instead of "
    "repeating the call."
)


def _sort_json_value(value: Any) -> Any:
    if isinstance(value, list):
        return [_sort_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _sort_json_value(value[key]) for key in sorted(value)}
    return value


def canonicalize_arguments(arguments_value: Any) -> str:
    """Deep key-sort, then JSON. Detection always uses the full string."""
    try:
        return json.dumps(_sort_json_value(arguments_value), ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return json.dumps(str(arguments_value), ensure_ascii=False)


def canonicalize_call(kind: str, act: Any) -> Tuple[str, str]:
    """Return ``(kind, canonical_args)`` using the loop-guard fingerprint."""
    try:
        from .pilot_guards import normalize_action_args

        return (str(kind or ""), normalize_action_args(kind, act))
    except Exception:
        args = getattr(act, "arguments", None)
        return (str(kind or ""), canonicalize_arguments(args))


def _wildcard_to_regex(pattern: str) -> re.Pattern:
    escaped = re.escape(pattern).replace(r"\*", ".*")
    return re.compile(r"^" + escaped + r"$")


def _csv_patterns(env_name: str) -> Tuple[re.Pattern, ...]:
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return ()
    return tuple(_wildcard_to_regex(part.strip()) for part in raw.split(",") if part.strip())


def is_tracked(tool_name: str) -> bool:
    """Untracked tools are transparent: they neither count nor reset the chain."""
    include = _csv_patterns("HARNESS_REPEAT_TOOL_INCLUDE")
    exclude = _csv_patterns("HARNESS_REPEAT_TOOL_EXCLUDE")
    if include and not any(pat.search(tool_name) for pat in include):
        return False
    if any(pat.search(tool_name) for pat in exclude):
        return False
    return True


def thresholds() -> Tuple[int, ...]:
    raw = os.environ.get("HARNESS_REPEAT_TOOL_THRESHOLDS", "").strip()
    if not raw:
        return DEFAULT_THRESHOLDS
    try:
        values = tuple(int(part.strip()) for part in raw.split(",") if part.strip())
    except ValueError:
        return DEFAULT_THRESHOLDS
    cleaned = tuple(sorted({v for v in values if v >= 2}))
    return cleaned or DEFAULT_THRESHOLDS


def preview_arguments(canonical: str, cap: int = DEFAULT_ARGUMENTS_PREVIEW_CHARS) -> str:
    if len(canonical) <= cap:
        return canonical
    return "%s... (+%d more chars)" % (canonical[:cap], len(canonical) - cap)


def detailed_reminder(tool_name: str, count: int, canonical_arguments: str) -> str:
    return (
        "Repeated tool call detected:\n"
        "- tool: %s\n"
        "- consecutive_calls: %d\n"
        "- arguments: %s\n"
        "The repeated calls are not making progress. Do not call this tool with "
        "these exact arguments again. Inspect the latest result and choose a "
        "different action, different arguments, or finish the task if enough "
        "evidence has been gathered."
        % (tool_name, count, preview_arguments(canonical_arguments))
    )


def format_nudge(tool_name: str, count: int, canonical_arguments: str, *, gentle_at: int) -> str:
    body = (
        GENTLE_REMINDER
        if count == gentle_at
        else detailed_reminder(tool_name, count, canonical_arguments)
    )
    return "[%s] %s" % (PLUGIN_SOURCE, body)


def observe_repeat(session: Any, kind: str, act: Any) -> Optional[str]:
    """Advance the session chain. Return a nudge string at 3/5/8, else None."""
    tool_name = str(kind or "").strip()
    if not tool_name or not is_tracked(tool_name):
        return None
    key_kind, canonical = canonicalize_call(tool_name, act)
    key = json.dumps([key_kind, canonical], ensure_ascii=False, separators=(",", ":"))
    chain = getattr(session, "_repeat_tool_chain", None)
    if isinstance(chain, dict) and chain.get("key") == key:
        count = int(chain.get("count") or 0) + 1
    else:
        count = 1
    session._repeat_tool_chain = {"key": key, "count": count}
    marks = thresholds()
    if count not in marks:
        return None
    return format_nudge(tool_name, count, canonical, gentle_at=marks[0])


def reset_repeat_chain(session: Any) -> None:
    """User interjection resets the chain. Repetition across a turn is not a loop."""
    try:
        session._repeat_tool_chain = None
    except Exception:
        pass


def suffix_repeat_nudge(content: str, nudge: Optional[str]) -> str:
    if not nudge:
        return content
    base = content if content is not None else ""
    return base + "\n\n" + nudge


def note_repeat_and_maybe_nudge(session: Any, act: Any, content: str) -> str:
    """Post-execute seam: count this attempt and suffix a nudge when due."""
    try:
        kind = getattr(act, "kind", "") or ""
        nudge = observe_repeat(session, kind, act)
        return suffix_repeat_nudge(content, nudge)
    except Exception:
        return content
