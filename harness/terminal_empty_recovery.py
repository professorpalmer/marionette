from __future__ import annotations

"""One-shot recovery after a successful tool batch returns an empty assistant."""

import re
from typing import Any, Optional

RETRY_PROMPT = (
    "The tools completed, but your response was empty. Provide a concise "
    "final response summarizing the result for the user."
)


# A turn that ends on an announced next action and no tool call is a stall.
STALL_PROMPT = (
    "You said what you will do next, but you sent no tool call. Make that "
    "tool call now. If the work is done, give the final answer instead."
)
STALL_NUDGE_LIMIT = 2
_STALL_REPLY_MAX_CHARS = 400
# Visible prose: only an explicit "now" / "next" announcement at the end.
_SAY_ANNOUNCES = re.compile(
    r"(?:\blet me now\b|\bi(?:'|\u2019)?ll now\b|\bi will now\b"
    r"|\bnow i(?:'|\u2019)?ll\b|\bnow i will\b|\bnext[,:] i\b)"
    r"[^.!?\n]{0,100}[.:\u2026]?\s*$",
    re.IGNORECASE,
)
# Promoted reasoning: any first-person plan as the last sentence.
_REASONING_ANNOUNCES = re.compile(
    r"(?:^|[.!?:\n]\s*)"
    r"(?:let me(?! know)|let(?:'|\u2019)s|i(?:'|\u2019)ll|i will|i need to|i(?:'|\u2019)m going to"
    r"|i am going to|next[,:]? i\b|now i(?:'|\u2019)ll|now i will|first[,:]? i(?:'|\u2019)ll)\b"
    r"[^.!?\n]{0,160}[.:\u2026]*\s*$",
    re.IGNORECASE,
)


def announces_next_action(say: str, promoted: str = "") -> bool:
    """Whether a reply with no tool call ends on a plan to act."""
    text = (say or "").strip()
    if text and len(text) <= _STALL_REPLY_MAX_CHARS and _SAY_ANNOUNCES.search(text[-160:]):
        return True
    thought = (promoted or "").strip()
    return bool(thought and _REASONING_ANNOUNCES.search(thought[-240:]))


def stall_nudge_due(session: Any, *, say: str, promoted: str = "") -> bool:
    """True at most STALL_NUDGE_LIMIT times per turn for an announced stall."""
    if not announces_next_action(say, promoted):
        return False
    used = int(getattr(session, "_stall_nudges", 0) or 0)
    if used >= STALL_NUDGE_LIMIT:
        return False
    session._stall_nudges = used + 1
    session._history.append({"role": "user", "content": STALL_PROMPT})
    return True


def reset_terminal_empty_recovery(session: Any) -> None:
    try:
        session._last_tool_batch = "none"
        session._empty_after_tools_retried = False
        session._stall_nudges = 0
    except Exception:
        pass


def note_tool_batch(session: Any, *, had_actions: bool, had_error: bool) -> None:
    try:
        if not had_actions:
            session._last_tool_batch = "none"
        elif had_error:
            session._last_tool_batch = "error"
        else:
            session._last_tool_batch = "success"
    except Exception:
        pass


def empty_after_tools_decision(session: Any, productive: bool) -> str:
    """Return ok, retry, fail, or count."""
    if productive:
        return "ok"
    if getattr(session, "_last_tool_batch", "none") != "success":
        return "count"
    if getattr(session, "_empty_after_tools_retried", False):
        return "fail"
    try:
        session._empty_after_tools_retried = True
    except Exception:
        return "count"
    return "retry"


def step_recovery(session: Any, productive: bool, turn: Any, say: str, promoted: str) -> str:
    """ok, fail, count, or retry. On retry a prompt is already in history."""
    decision = empty_after_tools_decision(session, productive)
    if decision == "retry":
        inject_empty_retry(session)
    elif decision == "ok" and not turn.actions and stall_nudge_due(
        session, say=say or "", promoted=promoted or "",
    ):
        return "retry"
    return decision


def inject_empty_retry(session: Any) -> Optional[str]:
    try:
        session._history.append({"role": "user", "content": RETRY_PROMPT})
        return RETRY_PROMPT
    except Exception:
        return None


def last_batch_had_error(session: Any, n_actions: int) -> bool:
    if n_actions <= 0:
        return False
    seen = 0
    try:
        for msg in reversed(getattr(session, "_history", None) or []):
            if msg.get("role") != "tool":
                continue
            if msg.get("is_error") is True:
                return True
            seen += 1
            if seen >= n_actions:
                break
    except Exception:
        return False
    return False
