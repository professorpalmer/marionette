from __future__ import annotations

"""Steer-only multi-signal runaway detector.

Dest already suppress+replays exact ``(kind, args)`` via ``check_loop_guard``.
This module observes non-exact families after a tool result and may suffix
one steer per turn. It never vetoes execution. Clean-room dest-native;
not a MiniMax copy.
"""

import hashlib
import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

PLUGIN_SOURCE = "runaway-guard"

SIGNAL_EXACT_ACTION = "exact_action_repeat"
SIGNAL_EXACT_RESULT = "exact_result_repeat"
SIGNAL_SAME_ERROR = "same_error_family"
SIGNAL_ABAB = "abab_action_cycle"
SIGNAL_POLLING = "polling_repeat"
SIGNAL_UNCHANGED = "unchanged_progress_repeat"

# Dest-novel steers. exact_action stays on check_loop_guard.
REMINDER_PRIORITY = (
    SIGNAL_UNCHANGED,
    SIGNAL_SAME_ERROR,
    SIGNAL_POLLING,
)

POLLING_KINDS = frozenset({"wait"})
DETECT_KINDS = frozenset({"run_command"})
_GREP_CMD = re.compile(r"^(?:rg|grep|git\s+grep)(?:\s|$)", re.IGNORECASE)
_DO_NOT_PERSIST = (
    " This is a temporary runtime reminder for the current turn only, "
    "not a user preference or a durable rule; do not save this reminder "
    "or generalize it into Memory, Skills, or other persistent instruction "
    "files for future turns or sessions."
)
_NUM = re.compile(r"\d+")


def runaway_enabled() -> bool:
    raw = (os.environ.get("HARNESS_RUNAWAY_GUARD") or "1").strip().lower()
    return raw not in {"0", "off", "false", "no"}


def runaway_shadow() -> bool:
    raw = (os.environ.get("HARNESS_RUNAWAY_SHADOW") or "").strip().lower()
    return raw in {"1", "on", "true", "yes"}


def remind_after() -> int:
    raw = (os.environ.get("HARNESS_RUNAWAY_AFTER") or "").strip()
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return 3
    return n if n >= 2 else 3


def _state(session: Any) -> Dict[str, Any]:
    blob = getattr(session, "_runaway_guard", None)
    if not isinstance(blob, dict):
        blob = {
            "action_keys": [],
            "error_key": None,
            "error_count": 0,
            "poll_key": None,
            "poll_count": 0,
            "progress_key": None,
            "progress_count": 0,
            "result_key": None,
            "result_count": 0,
            "steer_used": False,
            "observations": [],
        }
        session._runaway_guard = blob
    return blob


def reset_runaway_state(session: Any) -> None:
    try:
        session._runaway_guard = None
    except Exception:
        pass


def _action_key(kind: str, act: Any) -> str:
    try:
        from .repeat_tool_reminder import canonicalize_call

        key_kind, canonical = canonicalize_call(kind, act)
        return json.dumps([key_kind, canonical], ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return json.dumps([str(kind or ""), ""], ensure_ascii=False, separators=(",", ":"))


def _policy(kind: str, act: Any) -> str:
    name = str(kind or "").strip()
    if name in POLLING_KINDS:
        return "polling"
    if name in DETECT_KINDS:
        command = str(getattr(act, "command", "") or "")
        if _GREP_CMD.match(command.strip()):
            return "detect"
        return "detect"
    return ""


def error_family(content: str, is_error: Optional[bool]) -> Optional[str]:
    if is_error is not True:
        text = (content or "").lstrip()
        if not (
            text.startswith("error:")
            or text.startswith("Error:")
            or text.startswith("ERROR")
            or " Traceback (most recent call last)" in (content or "")
        ):
            return None
    first = (content or "").strip().splitlines()[0] if content else "error"
    return _NUM.sub("N", first)[:200] or "error"


def progress_fingerprint(kind: str, content: str) -> Optional[str]:
    if str(kind or "") not in POLLING_KINDS:
        return None
    raw = (content or "").strip()
    if not raw:
        return None
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:16]


def _result_key(content: str) -> str:
    return hashlib.sha256((content or "").encode("utf-8", "replace")).hexdigest()[:16]


def _steer_text(kind: str, occurrences: int) -> str:
    if kind == SIGNAL_SAME_ERROR:
        body = (
            "[runaway guard] The same tool error family has now occurred %d times in "
            "a row. Do not retry the same route unchanged. Diagnose the cause, change "
            "one controlled variable or switch route. Do not infer that the entire "
            "task has failed from this signal."
        ) % occurrences
    elif kind == SIGNAL_UNCHANGED:
        body = (
            "[runaway guard] Verified progress for the same work target has remained "
            "unchanged across %d attempts. Do not continue the same route without a "
            "concrete expected state change. Inspect the current artifact or state, "
            "change strategy, or explain the blocker. Unchanged observed state is "
            "not proof that the entire task has failed."
        ) % occurrences
    elif kind == SIGNAL_POLLING:
        count = "Three" if occurrences == 3 else str(occurrences)
        body = (
            "%s wait/status reads for the same target have returned an unchanged "
            "status. Avoid repeated polling; you will be notified when the "
            "background job completes."
        ) % count
    else:
        return ""
    return "[%s] %s%s" % (PLUGIN_SOURCE, body, _DO_NOT_PERSIST)


def observe_step(
    session: Any,
    kind: str,
    act: Any,
    content: str,
    is_error: Optional[bool] = None,
) -> List[str]:
    """Record signals for this result. Return reminder-eligible kinds this step."""
    state = _state(session)
    candidates: List[str] = []
    action_key = _action_key(kind, act)
    policy = _policy(kind, act)
    after = remind_after()

    keys: List[str] = list(state.get("action_keys") or [])
    keys.append(action_key)
    if len(keys) > 4:
        keys = keys[-4:]
    state["action_keys"] = keys
    if (
        len(keys) == 4
        and keys[0] == keys[2]
        and keys[1] == keys[3]
        and keys[0] != keys[1]
    ):
        state["observations"].append({"kind": SIGNAL_ABAB, "action": "observe"})

    family = error_family(content, is_error)
    if family:
        if state.get("error_key") == family:
            state["error_count"] = int(state.get("error_count") or 0) + 1
        else:
            state["error_key"] = family
            state["error_count"] = 1
        if int(state["error_count"]) >= after:
            candidates.append(SIGNAL_SAME_ERROR)
            state["observations"].append({
                "kind": SIGNAL_SAME_ERROR,
                "occurrences": state["error_count"],
                "action": "steer",
            })
    else:
        state["error_key"] = None
        state["error_count"] = 0

    result_key = _result_key(content)
    if state.get("result_key") == result_key:
        state["result_count"] = int(state.get("result_count") or 0) + 1
        if int(state["result_count"]) >= after:
            state["observations"].append({
                "kind": SIGNAL_EXACT_RESULT,
                "occurrences": state["result_count"],
                "action": "observe",
            })
    else:
        state["result_key"] = result_key
        state["result_count"] = 1

    if policy == "polling":
        poll_key = action_key
        if state.get("poll_key") == poll_key:
            state["poll_count"] = int(state.get("poll_count") or 0) + 1
        else:
            state["poll_key"] = poll_key
            state["poll_count"] = 1
        if int(state["poll_count"]) >= after:
            candidates.append(SIGNAL_POLLING)
            state["observations"].append({
                "kind": SIGNAL_POLLING,
                "occurrences": state["poll_count"],
                "action": "steer",
            })
        progress = progress_fingerprint(kind, content)
        if progress:
            if state.get("progress_key") == progress:
                state["progress_count"] = int(state.get("progress_count") or 0) + 1
            else:
                state["progress_key"] = progress
                state["progress_count"] = 1
            if int(state["progress_count"]) >= after:
                candidates.append(SIGNAL_UNCHANGED)
                state["observations"].append({
                    "kind": SIGNAL_UNCHANGED,
                    "occurrences": state["progress_count"],
                    "action": "steer",
                })
    return candidates


def take_preferred_steer(session: Any, candidates: List[str]) -> Optional[Tuple[str, str]]:
    state = _state(session)
    if state.get("steer_used"):
        return None
    after = remind_after()
    for kind in REMINDER_PRIORITY:
        if kind not in candidates:
            continue
        text = _steer_text(kind, after)
        if not text:
            continue
        state["steer_used"] = True
        return (kind, text)
    return None


def suffix_steer(content: str, nudge: Optional[str]) -> str:
    if not nudge:
        return content
    base = content if content is not None else ""
    return base + "\n\n" + nudge


def note_runaway_and_maybe_steer(
    session: Any,
    act: Any,
    content: str,
    *,
    is_error: Optional[bool] = None,
) -> str:
    """Post-execute seam. Never raises. Never vetoes."""
    try:
        if not runaway_enabled():
            return content
        kind = getattr(act, "kind", "") or ""
        candidates = observe_step(session, kind, act, content, is_error)
        picked = take_preferred_steer(session, candidates)
        if picked is None or runaway_shadow():
            return content
        return suffix_steer(content, picked[1])
    except Exception:
        return content
