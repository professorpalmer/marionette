from __future__ import annotations

"""Deterministic error classification for driver HTTP failures.

The harness drivers talk to many OpenAI-compatible and Anthropic endpoints over
plain urllib. A failure is one of a few kinds, and the right response differs
per kind: a transient 503 should be retried with backoff, a 429 should be
retried with a LONGER backoff (honoring Retry-After when present), a
context-overflow 400 should trigger history compaction then one retry, and an
auth/permission/not-found error is fatal and must stop immediately rather than
hammering a doomed request.

Pure and stdlib-only: classification is a function of (http_status, message),
so it unit-tests fast and hermetically. No network, no logging, no secrets.
"""

import re
from enum import Enum
from typing import Optional


class ErrorClass(str, Enum):
    RETRYABLE = "retryable"            # transient server/network; retry w/ backoff
    RATE_LIMIT = "rate_limit"         # 429; retry w/ longer backoff + Retry-After
    CONTEXT_OVERFLOW = "context_overflow"  # prompt too long; compact then retry
    AUTH = "auth"  # 401/403; fatal, do not retry
    FATAL = "fatal"                   # 400 (non-context)/404/other; do not retry


# Phrases that mean "the prompt exceeded the model's context window" across
# OpenAI, Anthropic, OpenRouter, z.ai, Moonshot, and most compat providers.
# Do NOT match bare ``max_tokens`` / ``exceeds the maximum``: those fire on
# completion-limit 400s (Kimi/GLM accept a large max_tokens; other models
# reject the same request). Compacting history cannot fix that, and the UI
# then lies with "overflow persists" on a 1% window.
_CONTEXT_PATTERNS = [
    "context length", "context window", "maximum context",
    "too many tokens", "reduce the length of the messages",
    "reduce the length of the prompt",
    "prompt is too long", "input is too long",
    "exceeds the maximum context", "exceeded the context",
    "context_length_exceeded", "request too large",
]

# Transient signals that appear in 5xx bodies or network exception reprs.
_RETRYABLE_PATTERNS = [
    "overloaded", "timeout", "timed out", "temporarily unavailable",
    "service unavailable", "bad gateway", "gateway timeout",
    "connection reset", "connection aborted", "connection refused",
    "econnreset", "read timed out", "remotedisconnected",
    "try again", "please retry", "internal server error",
]

# OpenCode Go (and similar relays) can return HTTP 401 AuthError when the
# upstream vendor blocks a request even though GET /models and the local key
# are fine. Treat as transient — not a dead/revoked key.
_UPSTREAM_BLOCK_PATTERNS = [
    "blocked by upstream provider",
    "request blocked by upstream",
]


def _norm(message: Optional[str]) -> str:
    return (message or "").lower()


def is_upstream_provider_block(message: Optional[str] = None) -> bool:
    """True when the body describes an upstream-vendor block, not a bad key."""
    msg = _norm(message)
    return any(p in msg for p in _UPSTREAM_BLOCK_PATTERNS)


def classify(http_status: Optional[int] = None, message: Optional[str] = None) -> ErrorClass:
    """Map an HTTP status and/or error message to an ErrorClass.

    Either argument may be None: a urllib URLError (network failure, no HTTP
    response) has status=None and is classified from the message alone; an
    HTTPError carries a status that dominates the decision.
    """
    msg = _norm(message)

    # Context-overflow can surface as a 400 OR (rarely) a 413; the body text is
    # the reliable signal, so check it before the generic 400=fatal rule.
    if any(p in msg for p in _CONTEXT_PATTERNS):
        # Guard: a 401 mentioning a context hint is still auth.
        if http_status not in (401, 403):
            return ErrorClass.CONTEXT_OVERFLOW

    if http_status is not None:
        if http_status == 429:
            return ErrorClass.RATE_LIMIT
        if http_status in (401, 403):
            # Relay 401 "blocked by upstream" is not a rejected local key.
            if is_upstream_provider_block(message):
                return ErrorClass.RETRYABLE
            return ErrorClass.AUTH
        if http_status == 413:
            return ErrorClass.CONTEXT_OVERFLOW
        if http_status >= 500:
            return ErrorClass.RETRYABLE
        if http_status == 408:
            return ErrorClass.RETRYABLE
        if http_status == 404:
            return ErrorClass.FATAL
        if http_status == 400:
            return ErrorClass.FATAL
        # Other 4xx: not retryable.
        if 400 <= http_status < 500:
            return ErrorClass.FATAL

    # No HTTP status (network-level exception) -> classify from message.
    if is_upstream_provider_block(message):
        return ErrorClass.RETRYABLE
    if any(p in msg for p in _RETRYABLE_PATTERNS):
        return ErrorClass.RETRYABLE

    # Unknown shape with no status: treat a bare network error repr as
    # retryable (urlopen raised URLError/socket.timeout), else fatal.
    if any(tok in msg for tok in ("urlerror", "timeout", "socket", "ssl", "httplib", "http.client")):
        return ErrorClass.RETRYABLE

    return ErrorClass.FATAL


def is_retryable(cls: ErrorClass) -> bool:
    """Whether the retry loop should attempt this class again (with backoff)."""
    return cls in (ErrorClass.RETRYABLE, ErrorClass.RATE_LIMIT)


def parse_retry_after(message: Optional[str]) -> Optional[float]:
    """Best-effort Retry-After seconds parsed from a 429 body/header text.

    Handles 'retry-after: 12', 'try again in 8s', 'retry after 3 seconds'.
    Returns None when no explicit delay is present.
    """
    msg = _norm(message)
    for pat in (
        r"retry[- ]after[:\s]+(\d+(?:\.\d+)?)",
        r"try again in\s+(\d+(?:\.\d+)?)\s*s",
        r"retry after\s+(\d+(?:\.\d+)?)\s*sec",
        r"in\s+(\d+(?:\.\d+)?)\s*seconds",
    ):
        m = re.search(pat, msg)
        if m:
            try:
                return float(m.group(1))
            except (ValueError, IndexError):
                pass
    return None
