from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

import random
import re
import time
from typing import Callable, Optional

from .base import DriverResponse
from . import error_classifier


def extract_status(error_str: Optional[str]) -> Optional[int]:
    """Extract integer HTTP status from an error string like 'HTTP 429: body' -> 429, else None."""
    if not error_str:
        return None
    m = re.search(r"\bHTTP\s+(\d+)\b", error_str, re.IGNORECASE)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    return None


_RETRY_LIMIT: ContextVar[Optional[int]] = ContextVar("provider_retry_limit", default=None)


@contextmanager
def single_attempt():
    """Keep a recovery request from expanding nested driver retry loops."""
    token = _RETRY_LIMIT.set(1)
    try:
        yield
    finally:
        _RETRY_LIMIT.reset(token)


def with_retry(
    fn: Callable[[], DriverResponse],
    *,
    max_attempts: int = 4,
    base_delay: float = 0.5,
    max_delay: float = 20.0,
    sleep: Callable[[float], None] = time.sleep,
    is_cancelled: Optional[Callable[[], bool]] = None,
) -> DriverResponse:
    """Retry a driver call fn() up to max_attempts on retryable errors."""
    attempts = 0
    timeout_first = None
    limit = _RETRY_LIMIT.get()
    if limit is not None:
        max_attempts = min(max_attempts, limit)
    while True:
        if is_cancelled is not None and is_cancelled():
            return DriverResponse(text="", error="cancelled before provider retry")
        resp = fn()
        attempts += 1

        if resp.meta is None:
            resp.meta = {}
        else:
            resp.meta = dict(resp.meta)
        reported_attempts = resp.meta.get("retry_attempts", 0)
        if not isinstance(reported_attempts, int) or isinstance(reported_attempts, bool):
            reported_attempts = 0
        total_attempts = max(attempts, reported_attempts)

        if timeout_first is not None:
            resp = merge_recovery_response(
                timeout_first, resp,
                recovered=not resp.error,
                prefer_retry=recovery_response_is_authoritative(resp),
            )
            resp.meta["retry_attempts"] = max(total_attempts, attempts)
            return resp

        if not resp.error:
            resp.meta["retry_attempts"] = total_attempts
            return resp

        if resp.meta.get("recovery_attempted"):
            resp.meta["retry_attempts"] = total_attempts
            return resp

        status = extract_status(resp.error)
        err_class = error_classifier.classify(status, resp.error)

        if resp.meta.get("stream_started"):
            resp.meta["retry_attempts"] = total_attempts
            resp.meta["error_class"] = err_class.value
            return resp

        if not error_classifier.is_retryable(err_class):
            resp.meta["retry_attempts"] = total_attempts
            resp.meta["error_class"] = err_class.value
            return resp

        timeout = is_transport_timeout(resp)
        if attempts >= max_attempts or (timeout and attempts >= 2):
            resp.meta["retry_attempts"] = attempts
            resp.meta["error_class"] = err_class.value
            return resp
        if timeout:
            timeout_first = resp

        if is_cancelled is not None and is_cancelled():
            return resp

        attempt_idx = attempts - 1
        backoff = min(base_delay * (2 ** attempt_idx), max_delay) + random.uniform(0.0, 0.5)

        if err_class == error_classifier.ErrorClass.RATE_LIMIT:
            retry_after = error_classifier.parse_retry_after(resp.error)
            if retry_after is not None:
                backoff = max(backoff, retry_after)

        if is_cancelled is None:
            sleep(backoff)
        else:
            remaining = backoff
            while remaining > 0 and not is_cancelled():
                interval = min(remaining, 0.1)
                sleep(interval)
                remaining -= interval
        if is_cancelled is not None and is_cancelled():
            return resp


def recovery_response_is_authoritative(resp: DriverResponse) -> bool:
    """A retry with an explicit provider terminal supersedes a transport error."""
    if not resp.error:
        return True
    meta = resp.meta or {}
    if (resp.text or meta.get("reasoning") or meta.get("tool_calls")
            or meta.get("incomplete_tool_calls") or meta.get("stream_started")):
        return True
    if extract_status(resp.error) is not None:
        return True
    if meta.get("finish_reason") or meta.get("incomplete_reason"):
        return True
    return meta.get("stream_terminal") not in (None, "", "error", "transport_error")


def is_transport_timeout(resp: DriverResponse) -> bool:
    """Only explicit network timeouts qualify for silent request recovery."""
    error = getattr(resp, "error", None)
    if not isinstance(error, str) or not error:
        return False
    meta = getattr(resp, "meta", None) or {}
    if not isinstance(meta, dict):
        return False
    if meta.get("recovery_attempted") or meta.get("incomplete_retry_attempted"):
        return False
    if meta.get("retry_attempts", 1) != 1:
        return False
    if meta.get("finish_reason") or meta.get("incomplete_reason"):
        return False
    if meta.get("tool_calls") or meta.get("incomplete_tool_calls"):
        return False
    if meta.get("stream_terminal") not in (None, "", "error", "transport_error"):
        return False
    status = extract_status(error)
    if status is not None:
        return status == 408
    return bool(re.search(
        r"TimeoutError\b|socket\.timeout\b|ReadTimeout\b|read (?:operation )?timed out",
        error, re.IGNORECASE,
    ))


def merge_recovery_response(
    first: DriverResponse, retry: DriverResponse, *, recovered: bool,
    prefer_retry: bool | None = None,
) -> DriverResponse:
    """Keep terminal provenance, bill both attempts, and retain visible prose."""
    use_retry = recovered if prefer_retry is None else prefer_retry
    chosen = retry if use_retry else first
    meta = dict(chosen.meta or {})
    first_meta = first.meta or {}
    retry_meta = retry.meta or {}
    for key in (
        "cache_read_tokens", "cache_write_tokens",
        "cache_write_5m_tokens", "cache_write_1h_tokens",
    ):
        values = (first_meta.get(key), retry_meta.get(key))
        if any(isinstance(value, int) and not isinstance(value, bool) for value in values):
            meta[key] = sum(
                value for value in values
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0
            )
    costs = (first_meta.get("provider_cost_usd"), retry_meta.get("provider_cost_usd"))
    numeric_costs = [
        float(value) for value in costs
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        and float(value) >= 0.0
    ]
    if numeric_costs:
        meta["provider_cost_usd"] = sum(numeric_costs)
    ttl_bases = {
        str(value).strip().lower() for value in (
            first_meta.get("cache_write_ttl_basis"),
            retry_meta.get("cache_write_ttl_basis"),
        ) if str(value or "").strip()
    }
    if len(ttl_bases) > 1:
        meta["cache_write_ttl_basis"] = "inferred"
    request_counts = []
    for source in (first_meta, retry_meta):
        value = source.get("retry_attempts", 1)
        request_counts.append(
            value if isinstance(value, int) and not isinstance(value, bool) and value > 0
            else 1
        )
    meta["retry_attempts"] = sum(request_counts)
    meta["recovery_attempted"] = True
    perf = dict((first.meta or {}).get("stream_performance") or {})
    perf.update({
        "recovery_attempt_count": 1,
        "recovery_success_count": int(recovered),
        "recovery_failure_count": int(not recovered),
    })
    meta["stream_performance"] = perf
    text = chosen.text
    if use_retry and first.text and not text.startswith(first.text):
        text = first.text + ("\n\n" + text if text else "")
    return DriverResponse(
        text=text,
        tokens_in=first.tokens_in + retry.tokens_in,
        tokens_out=first.tokens_out + retry.tokens_out,
        latency_ms=first.latency_ms + retry.latency_ms,
        model=chosen.model or first.model,
        error=chosen.error,
        meta=meta,
    )
