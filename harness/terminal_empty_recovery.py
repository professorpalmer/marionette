from __future__ import annotations

"""One-shot recovery after a successful tool batch returns an empty assistant."""

from typing import Any, Optional

RETRY_PROMPT = (
    "The tools completed, but your response was empty. Provide a concise "
    "final response summarizing the result for the user."
)


def reset_terminal_empty_recovery(session: Any) -> None:
    try:
        session._last_tool_batch = "none"
        session._empty_after_tools_retried = False
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
