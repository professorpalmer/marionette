"""Typed provider / send-loop terminal causes.

Python 3.9-compatible vocabulary plus a pure classifier. Unknown or missing
provider reasons never become ``natural``. Loop-level closes (step cap,
turn budget, stagnation, …) are named here so finalize/receipts share one
allowlist. Stdlib only; never raises on the hot path.
"""
from __future__ import annotations

from typing import Any, Dict, FrozenSet, List, NamedTuple, Optional

# Canonical vocabulary. ``tool_calls`` is the intermediate/tool-use close;
# ``invalid_tool`` is the auto-halt invalid-tool close.
TERMINAL_NATURAL = "natural"
TERMINAL_TOOL_CALLS = "tool_calls"
TERMINAL_LENGTH = "length"
TERMINAL_INCOMPLETE = "incomplete"
TERMINAL_CONTENT_FILTER = "content_filter"
TERMINAL_PROVIDER_EOF = "provider_eof"
TERMINAL_TRANSPORT_ERROR = "transport_error"
TERMINAL_CANCELLED = "cancelled"
TERMINAL_STEP_CAP = "step_cap"
TERMINAL_TURN_BUDGET = "turn_budget"
TERMINAL_STAGNATION = "stagnation"
TERMINAL_INVALID_TOOL = "invalid_tool"
TERMINAL_EMPTY_LOOP = "empty_loop"
TERMINAL_DRIVER_SWAP = "driver_swap"

# Fail-closed label when a provider reason is missing or unrecognized.
TERMINAL_UNSPECIFIED = "unspecified"

TERMINAL_CAUSES: FrozenSet[str] = frozenset({
    TERMINAL_NATURAL,
    TERMINAL_TOOL_CALLS,
    TERMINAL_LENGTH,
    TERMINAL_INCOMPLETE,
    TERMINAL_CONTENT_FILTER,
    TERMINAL_PROVIDER_EOF,
    TERMINAL_TRANSPORT_ERROR,
    TERMINAL_CANCELLED,
    TERMINAL_STEP_CAP,
    TERMINAL_TURN_BUDGET,
    TERMINAL_STAGNATION,
    TERMINAL_INVALID_TOOL,
    TERMINAL_EMPTY_LOOP,
    TERMINAL_DRIVER_SWAP,
    TERMINAL_UNSPECIFIED,
})

# Aliases accepted when reading provider / Wave 1 labels.
_CAUSE_ALIASES = {
    "intermediate": TERMINAL_TOOL_CALLS,
    "tool_use": TERMINAL_TOOL_CALLS,
    "tool-calls": TERMINAL_TOOL_CALLS,
    "auto_halt": TERMINAL_INVALID_TOOL,
    "invalid-tool": TERMINAL_INVALID_TOOL,
    "eof": TERMINAL_PROVIDER_EOF,
    "error": TERMINAL_TRANSPORT_ERROR,
    "transport": TERMINAL_TRANSPORT_ERROR,
}

# Provider dialects that mean an affirmative natural stop (no tools).
_NATURAL_REASONS = frozenset({
    "stop",
    "end_turn",
    "end-turn",
    "completed",
    "complete",
    "stop_sequence",
    "stop-sequence",
})

# Gemini uses uppercase STOP for a clean end (or for function calls).
_GEMINI_NATURAL = frozenset({"STOP", "stop"})

_LENGTH_REASONS = frozenset({
    "length",
    "max_tokens",
    "max-tokens",
    "max_output_tokens",
    "max-output-tokens",
    "MAX_TOKENS",
})

_TOOL_REASONS = frozenset({
    "tool_calls",
    "tool_use",
    "tool-use",
    "FUNCTION_CALL",
    "function_call",
})

_FILTER_REASONS = frozenset({
    "content_filter",
    "content_filtered",
    "content-filter",
    "SAFETY",
    "RECITATION",
    "BLOCKLIST",
    "PROHIBITED_CONTENT",
    "SPII",
    "IMAGE_SAFETY",
    "LANGUAGE",
    "refusal",
    "guardrail_intervened",
    "guardrail",
})

_CANCEL_REASONS = frozenset({
    "cancelled",
    "canceled",
    "user_cancelled",
    "user_canceled",
    "interrupted",
})

_FAILED_REASONS = frozenset({
    "failed",
    "error",
    "MALFORMED_FUNCTION_CALL",
    "OTHER",
    "FINISH_REASON_UNSPECIFIED",
})

_INCOMPLETE_REASONS = frozenset({
    "incomplete",
    "empty",
})

# Missing framing / terminal-less close only. ``incomplete`` is a named
# model terminal, not an EOF synonym.
_EOF_REASONS = frozenset({
    "provider_eof",
    "eof",
})

# Causes that must never emit a clean assistant_done success.
# ``unspecified`` is fail-closed: missing/unknown provider reasons are never
# coerced to a natural stop.
BLOCK_CLEAN_FINALIZE: FrozenSet[str] = frozenset({
    TERMINAL_LENGTH,
    TERMINAL_INCOMPLETE,
    TERMINAL_CONTENT_FILTER,
    TERMINAL_PROVIDER_EOF,
    TERMINAL_TRANSPORT_ERROR,
    TERMINAL_CANCELLED,
    TERMINAL_UNSPECIFIED,
})

# Wave 1 stream_terminal values that already mean a classified close.
# ``incomplete`` is the named model terminal; only a missing framing /
# terminal-less close maps to provider_eof.
_STREAM_TERMINAL_MAP = {
    "stop": TERMINAL_NATURAL,
    "tool_calls": TERMINAL_TOOL_CALLS,
    "length": TERMINAL_LENGTH,
    "incomplete": TERMINAL_INCOMPLETE,
    "empty": TERMINAL_INCOMPLETE,
    "error": TERMINAL_TRANSPORT_ERROR,
    "content_filter": TERMINAL_CONTENT_FILTER,
    "cancelled": TERMINAL_CANCELLED,
    "canceled": TERMINAL_CANCELLED,
}


class TerminalClassification(NamedTuple):
    """Pure result of classifying a DriverResponse / error / meta blob."""

    cause: str
    finish_reason: str
    incomplete_reason: str
    stream_terminal: str
    has_provider_signal: bool
    is_affirmative_natural: bool
    is_intermediate: bool
    blocks_clean_finalize: bool
    allows_tool_execution: bool


def _as_text(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, str):
        return value.strip()
    try:
        return str(value).strip()
    except Exception:
        return ""


def _norm_reason(value: Any) -> str:
    return _as_text(value)


def canonicalize_terminal_cause(value: Any) -> str:
    """Map a label onto the vocabulary, or ``\"\"`` when it is not allowlisted."""
    raw = _as_text(value)
    if not raw:
        return ""
    if raw in TERMINAL_CAUSES:
        return raw
    aliased = _CAUSE_ALIASES.get(raw) or _CAUSE_ALIASES.get(raw.lower())
    if aliased:
        return aliased
    lower = raw.lower()
    if lower in TERMINAL_CAUSES:
        return lower
    return ""


def _meta_of(resp: Any, meta: Any) -> Dict[str, Any]:
    if isinstance(meta, dict):
        return meta
    if resp is None:
        return {}
    try:
        raw = getattr(resp, "meta", None)
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def _error_of(resp: Any, error: Any) -> str:
    if error is not None and not isinstance(error, bool):
        text = _as_text(error)
        if text:
            return text
    if resp is None:
        return ""
    try:
        return _as_text(getattr(resp, "error", None))
    except Exception:
        return ""


def _tool_arguments_are_complete(arguments: Any) -> bool:
    raw = arguments if isinstance(arguments, str) else ""
    if not raw.strip():
        return True
    try:
        import json
        json.loads(raw)
    except Exception:
        return False
    return True


def _executable_tool_calls(meta: Dict[str, Any]) -> List[Any]:
    raw = meta.get("tool_calls")
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        fn = item.get("function") if isinstance(item.get("function"), dict) else {}
        name = _as_text(fn.get("name") or item.get("name"))
        if not name:
            continue
        args = fn.get("arguments") if fn else item.get("arguments")
        if not _tool_arguments_are_complete(args):
            continue
        out.append(item)
    return out


def _has_incomplete_tools(meta: Dict[str, Any], executable: List[Any]) -> bool:
    incomplete = meta.get("incomplete_tool_calls")
    if isinstance(incomplete, list) and incomplete:
        return True
    raw = meta.get("tool_calls")
    if not isinstance(raw, list) or not raw:
        return False
    return len(executable) < len([
        item for item in raw if isinstance(item, dict)
    ])


def _looks_like_transport_error(error: str) -> bool:
    if not error:
        return False
    lowered = error.lower()
    needles = (
        "http ",
        "timeout",
        "timed out",
        "connection",
        "reset",
        "broken pipe",
        "urlopen",
        "ssl",
        "network",
        "502",
        "503",
        "504",
        "429",
    )
    return any(n in lowered for n in needles)


def _map_finish_reason(
    finish: str,
    *,
    incomplete_reason: str,
    executable_tools: List[Any],
) -> Optional[str]:
    """Map a raw provider finish/stop reason. None = unrecognized / missing."""
    if not finish:
        return None
    if finish in _LENGTH_REASONS or finish.lower() in {r.lower() for r in _LENGTH_REASONS}:
        return TERMINAL_LENGTH
    if finish in _FILTER_REASONS or finish.lower() in {r.lower() for r in _FILTER_REASONS}:
        return TERMINAL_CONTENT_FILTER
    if finish in _CANCEL_REASONS or finish.lower() in {r.lower() for r in _CANCEL_REASONS}:
        return TERMINAL_CANCELLED
    if finish in _TOOL_REASONS or finish.lower() in {r.lower() for r in _TOOL_REASONS}:
        return TERMINAL_TOOL_CALLS
    if finish in _FAILED_REASONS or finish.lower() in {r.lower() for r in _FAILED_REASONS}:
        if finish.lower() == "failed":
            return TERMINAL_TRANSPORT_ERROR
        return TERMINAL_INCOMPLETE
    if finish.lower() in _INCOMPLETE_REASONS:
        inc = incomplete_reason.lower()
        if inc in ("max_output_tokens", "length", "max_tokens"):
            return TERMINAL_LENGTH
        if inc == "content_filter":
            return TERMINAL_CONTENT_FILTER
        return TERMINAL_INCOMPLETE
    if finish.lower() in _EOF_REASONS and finish.lower() != "incomplete":
        return TERMINAL_PROVIDER_EOF
    if finish in _NATURAL_REASONS or finish.lower() in _NATURAL_REASONS:
        if executable_tools:
            return TERMINAL_TOOL_CALLS
        return TERMINAL_NATURAL
    if finish in _GEMINI_NATURAL:
        if executable_tools:
            return TERMINAL_TOOL_CALLS
        return TERMINAL_NATURAL
    return None


def _map_stream_terminal(stream_terminal: str) -> Optional[str]:
    if not stream_terminal:
        return None
    mapped = _STREAM_TERMINAL_MAP.get(stream_terminal) or _STREAM_TERMINAL_MAP.get(
        stream_terminal.lower()
    )
    return mapped


def classify_provider_terminal(
    resp: Any = None,
    *,
    error: Any = None,
    meta: Any = None,
) -> TerminalClassification:
    """Normalize DriverResponse / error / meta into one terminal cause.

    Never raises. Input usage, cache hits, and context-percent fields are
    ignored — only finish/stream/error/tool evidence counts. Missing or
    unknown provider reasons are ``unspecified`` / ``incomplete``, never
    ``natural``.
    """
    try:
        blob = _meta_of(resp, meta)
        err = _error_of(resp, error)
        finish = _norm_reason(
            blob.get("finish_reason")
            or blob.get("stop_reason")
            or blob.get("finishReason")
        )
        incomplete_reason = _norm_reason(blob.get("incomplete_reason"))
        stream_terminal = _norm_reason(blob.get("stream_terminal"))
        executable = _executable_tool_calls(blob)
        incomplete_tools = _has_incomplete_tools(blob, executable)
        stream_started = blob.get("stream_started")
        has_signal = bool(
            finish
            or stream_terminal
            or incomplete_reason
            or err
            or stream_started is True
            or executable
            or incomplete_tools
        )

        cause: Optional[str] = None

        # Wave 1 stream_terminal is already fail-closed; prefer it when present.
        mapped_stream = _map_stream_terminal(stream_terminal)
        mapped_finish = _map_finish_reason(
            finish,
            incomplete_reason=incomplete_reason,
            executable_tools=executable,
        )

        if mapped_stream is not None:
            cause = mapped_stream
            # Length / transport / filter from stream_terminal win over a
            # contradictory success finish. Tool-call finish can refine stop.
            if cause == TERMINAL_NATURAL and mapped_finish == TERMINAL_TOOL_CALLS:
                cause = TERMINAL_TOOL_CALLS
            if cause == TERMINAL_NATURAL and executable:
                cause = TERMINAL_TOOL_CALLS
            if cause in (TERMINAL_PROVIDER_EOF, TERMINAL_INCOMPLETE):
                if mapped_finish == TERMINAL_LENGTH:
                    cause = TERMINAL_LENGTH
                elif mapped_finish == TERMINAL_CONTENT_FILTER:
                    cause = TERMINAL_CONTENT_FILTER
                else:
                    inc = incomplete_reason.lower()
                    if inc in ("max_output_tokens", "length", "max_tokens"):
                        cause = TERMINAL_LENGTH
                    elif inc == "content_filter":
                        cause = TERMINAL_CONTENT_FILTER
        elif mapped_finish is not None:
            cause = mapped_finish
        elif err:
            lowered = err.lower()
            if "content_filter" in lowered or "content filter" in lowered:
                cause = TERMINAL_CONTENT_FILTER
            elif "finish_reason=length" in lowered or "max_tokens" in lowered:
                cause = TERMINAL_LENGTH
            elif "cancelled" in lowered or "canceled" in lowered or "interrupted" in lowered:
                cause = TERMINAL_CANCELLED
            elif _looks_like_transport_error(err):
                cause = TERMINAL_TRANSPORT_ERROR
            elif stream_started is True:
                cause = TERMINAL_PROVIDER_EOF
            else:
                cause = TERMINAL_TRANSPORT_ERROR if err else TERMINAL_UNSPECIFIED

        if cause is None and incomplete_reason:
            inc = incomplete_reason.lower()
            if inc in ("max_output_tokens", "length", "max_tokens"):
                cause = TERMINAL_LENGTH
            elif inc == "content_filter":
                cause = TERMINAL_CONTENT_FILTER
            else:
                cause = TERMINAL_INCOMPLETE

        if cause is None and incomplete_tools and not executable:
            cause = TERMINAL_INCOMPLETE

        if cause is None and executable and not err:
            # Tools without a recognized finish are intermediate, not natural.
            cause = TERMINAL_TOOL_CALLS

        if cause is None:
            # Missing / unknown provider reason: fail closed.
            if stream_started is True and not finish and not stream_terminal:
                cause = TERMINAL_PROVIDER_EOF
            elif has_signal and finish and mapped_finish is None:
                cause = TERMINAL_UNSPECIFIED
            elif has_signal and not finish and not stream_terminal and err:
                cause = TERMINAL_TRANSPORT_ERROR
            else:
                cause = TERMINAL_UNSPECIFIED

        if cause == TERMINAL_TOOL_CALLS and incomplete_tools and not executable:
            cause = TERMINAL_INCOMPLETE

        if cause in (TERMINAL_LENGTH, TERMINAL_INCOMPLETE, TERMINAL_PROVIDER_EOF):
            allows_tools = False
        elif cause == TERMINAL_TOOL_CALLS and executable:
            allows_tools = True
        else:
            allows_tools = False

        is_natural = cause == TERMINAL_NATURAL
        blocks = cause in BLOCK_CLEAN_FINALIZE or cause == TERMINAL_UNSPECIFIED
        return TerminalClassification(
            cause=cause,
            finish_reason=finish,
            incomplete_reason=incomplete_reason,
            stream_terminal=stream_terminal,
            has_provider_signal=has_signal,
            is_affirmative_natural=is_natural,
            is_intermediate=cause == TERMINAL_TOOL_CALLS,
            blocks_clean_finalize=blocks,
            allows_tool_execution=allows_tools,
        )
    except Exception:
        return TerminalClassification(
            cause=TERMINAL_UNSPECIFIED,
            finish_reason="",
            incomplete_reason="",
            stream_terminal="",
            has_provider_signal=False,
            is_affirmative_natural=False,
            is_intermediate=False,
            blocks_clean_finalize=True,
            allows_tool_execution=False,
        )


def provider_tools_are_executable(
    resp: Any = None,
    *,
    meta: Any = None,
    tool_calls: Any = None,
) -> bool:
    """True only when classified tools may reach ``execute_turn_actions``."""
    try:
        classified = classify_provider_terminal(resp, meta=meta)
        if not classified.allows_tool_execution:
            return False
        blob = _meta_of(resp, meta)
        if tool_calls is None:
            tool_calls = blob.get("tool_calls")
        if not isinstance(tool_calls, list) or not tool_calls:
            return False
        for item in tool_calls:
            if not isinstance(item, dict):
                return False
            fn = item.get("function") if isinstance(item.get("function"), dict) else {}
            args = fn.get("arguments") if fn else item.get("arguments")
            if not _tool_arguments_are_complete(args):
                return False
        return True
    except Exception:
        return False


def blocking_terminal_message(classified: TerminalClassification) -> str:
    """Short, truthful user-facing line for a blocked provider close."""
    cause = classified.cause
    if cause == TERMINAL_LENGTH:
        return "Provider stopped: output length limit (finish_reason=length)."
    if cause == TERMINAL_CONTENT_FILTER:
        return "Provider refused the response (content filter)."
    if cause == TERMINAL_PROVIDER_EOF:
        return "Provider stream ended without a finish_reason (incomplete)."
    if cause == TERMINAL_TRANSPORT_ERROR:
        return "Provider transport failed before a clean stop."
    if cause == TERMINAL_CANCELLED:
        return "Provider call was cancelled."
    if cause == TERMINAL_INCOMPLETE:
        return "Provider returned an incomplete response."
    return "Provider stopped without an affirmative natural finish."


def loop_exit_message(cause: str) -> str:
    """User-facing line for send-loop closes that are not provider terminals."""
    if cause == TERMINAL_EMPTY_LOOP:
        return "(No productive reply this turn — stopping.)"
    if cause == TERMINAL_DRIVER_SWAP:
        return "(Queued prompt needs a different driver — ending this turn.)"
    if cause == TERMINAL_STEP_CAP:
        return "(Reached the investigation step limit for this message.)"
    if cause == TERMINAL_TURN_BUDGET:
        return "(Reached the output token budget for this turn.)"
    if cause == TERMINAL_STAGNATION:
        return (
            "Stopped: repeated the same response and actions "
            "with no new progress (auto-halt). Tell me how to "
            "continue, or try a narrower ask."
        )
    return "(Turn ended.)"


def finalize_stop_cause(
    classified: Optional[TerminalClassification],
    *,
    loop_cause: str = "",
) -> str:
    """Pick the assistant_done stop_cause. Loop causes win when provided."""
    named = canonicalize_terminal_cause(loop_cause)
    if named:
        return named
    if classified is None:
        return TERMINAL_UNSPECIFIED
    if classified.is_affirmative_natural:
        return TERMINAL_NATURAL
    if classified.cause in TERMINAL_CAUSES:
        return classified.cause
    return TERMINAL_UNSPECIFIED


__all__ = (
    "BLOCK_CLEAN_FINALIZE",
    "TERMINAL_CAUSES",
    "TERMINAL_CANCELLED",
    "TERMINAL_CONTENT_FILTER",
    "TERMINAL_DRIVER_SWAP",
    "TERMINAL_EMPTY_LOOP",
    "TERMINAL_INCOMPLETE",
    "TERMINAL_INVALID_TOOL",
    "TERMINAL_LENGTH",
    "TERMINAL_NATURAL",
    "TERMINAL_PROVIDER_EOF",
    "TERMINAL_STAGNATION",
    "TERMINAL_STEP_CAP",
    "TERMINAL_TOOL_CALLS",
    "TERMINAL_TRANSPORT_ERROR",
    "TERMINAL_TURN_BUDGET",
    "TERMINAL_UNSPECIFIED",
    "TerminalClassification",
    "blocking_terminal_message",
    "canonicalize_terminal_cause",
    "classify_provider_terminal",
    "finalize_stop_cause",
    "loop_exit_message",
    "provider_tools_are_executable",
)
