from __future__ import annotations

"""Cooperative per-tool deadline.

A tool may declare ``timeoutMs``. This wrapper arms ``session._cancel`` for
that budget, restores the upstream event, and only *this* wrapper's expiry
becomes structured ``TOOL_TIMEOUT``. Inner command/ipython/worker timeouts
are not remapped — those kinds declare no budget here.
"""

import os
import threading
from typing import Any, Callable, Optional, Tuple

TOOL_TIMEOUT = "TOOL_TIMEOUT"

# Kinds that already honor their own deadline. Declaring a second wrapper
# would race their status and mislabel a command timeout as TOOL_TIMEOUT.
INNER_TIMEOUT_KINDS = frozenset(
    (
        "run_command",
        "run_command_batch",
        "run_ipython",
        "run_swarm",
        "run_implement",
        "run_parallel",
        "route_task",
    )
)

# Default budgets (ms) for tools that can block on network / index / MCP.
DEFAULT_TIMEOUT_MS = {
    "web_fetch": 45000,
    "web_search": 30000,
    "read_pdf": 45000,
    "search_codegraph": 30000,
    "search_files": 30000,
    "call_mcp": 45000,
    "query_wiki": 30000,
}


def declared_timeout_ms(kind: str) -> Optional[int]:
    """Return this wrapper's budget, or None when we must not arm a timer."""
    name = str(kind or "").strip()
    if not name or name in INNER_TIMEOUT_KINDS:
        return None
    env_key = "HARNESS_TOOL_TIMEOUT_%s_MS" % name.upper()
    raw = os.environ.get(env_key, "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = 0
        return value if value > 0 else None
    default = DEFAULT_TIMEOUT_MS.get(name)
    if default is None:
        return None
    return int(default)


def tool_timeout_message(timeout_ms: int) -> str:
    return "tool call timed out after %dms" % int(timeout_ms)


def tool_timeout_triple(timeout_ms: int) -> Tuple[bool, str, str]:
    return (False, TOOL_TIMEOUT, tool_timeout_message(timeout_ms))


def run_with_tool_deadline(
    session: Any,
    kind: str,
    fn: Callable[[], Any],
    timeout_ms: Optional[int] = None,
) -> Tuple[Any, Optional[int]]:
    """Run ``fn`` under a scoped cancel.

    Returns ``(result, None)`` on a normal finish (including inner timeout or
    user Stop). Returns ``(result, timeout_ms)`` only when *this* timer fired.
    Never remaps an inner status. Restores ``session._cancel`` when we set it
    and the user did not also request interrupt.
    """
    if timeout_ms is None:
        timeout_ms = declared_timeout_ms(kind)
    if not timeout_ms:
        return fn(), None

    cancel = getattr(session, "_cancel", None)
    if cancel is None or not hasattr(cancel, "is_set"):
        return fn(), None

    already = bool(cancel.is_set())
    owned = []

    def _expire() -> None:
        try:
            if cancel.is_set():
                return
            owned.append(True)
            cancel.set()
        except Exception:
            pass

    timer = threading.Timer(float(timeout_ms) / 1000.0, _expire)
    timer.daemon = True
    timer.start()
    try:
        result = fn()
    finally:
        try:
            timer.cancel()
        except Exception:
            pass
        user_stop = bool(getattr(session, "_interrupt_requested", False))
        if owned and not already and not user_stop:
            try:
                cancel.clear()
            except Exception:
                pass
    if owned:
        return result, int(timeout_ms)
    return result, None


def invoke_do(session: Any, act: Any, fn: Callable[[], Any]) -> Any:
    """Wrap a ``_do_*`` triple. Our expiry becomes ``(False, TOOL_TIMEOUT, ...)``."""
    kind = getattr(act, "kind", "") or ""
    result, timed_out = run_with_tool_deadline(session, kind, fn)
    if timed_out:
        return tool_timeout_triple(timed_out)
    return result
