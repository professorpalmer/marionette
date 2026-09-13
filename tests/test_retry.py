import pytest
from pmharness.drivers.base import DriverResponse
from pmharness.drivers.retry import with_retry, extract_status


def test_extract_status():
    assert extract_status("HTTP 429: Rate limit") == 429
    assert extract_status("HTTP 503: Service Unavailable") == 503
    assert extract_status("HTTP 401: Unauthorized") == 401
    assert extract_status("No status") is None


def test_with_retry_success_first_try():
    calls = []

    def fn():
        calls.append(1)
        return DriverResponse(text="ok")

    resp = with_retry(fn, sleep=lambda x: None)
    assert len(calls) == 1
    assert resp.text == "ok"
    assert resp.meta.get("retry_attempts") == 1


def test_with_retry_503_then_success():
    calls = []

    def fn():
        calls.append(1)
        if len(calls) == 1:
            return DriverResponse(text="", error="HTTP 503: overloaded")
        return DriverResponse(text="ok")

    resp = with_retry(fn, sleep=lambda x: None)
    assert len(calls) == 2
    assert resp.text == "ok"
    assert resp.meta.get("retry_attempts") == 2


def test_with_retry_401_stops_immediately():
    calls = []

    def fn():
        calls.append(1)
        return DriverResponse(text="", error="HTTP 401: Unauthorized")

    resp = with_retry(fn, sleep=lambda x: None)
    assert len(calls) == 1
    assert resp.error == "HTTP 401: Unauthorized"
    assert resp.meta.get("retry_attempts") == 1
    assert resp.meta.get("error_class") == "auth"


def test_with_retry_honors_retry_after():
    calls = []
    sleeps = []

    def fn():
        calls.append(1)
        return DriverResponse(text="", error="HTTP 429: retry after 10 seconds")

    def mock_sleep(seconds):
        sleeps.append(seconds)

    # We set max_attempts=2 so it retries once, then gives up
    resp = with_retry(fn, max_attempts=2, sleep=mock_sleep)
    assert len(calls) == 2
    assert len(sleeps) == 1
    # 10 is parsed from "retry after 10 seconds". Backoff is base_delay + jitter, max(backoff, 10) should be 10.
    assert sleeps[0] >= 10.0
    assert resp.meta.get("retry_attempts") == 2
    assert resp.meta.get("error_class") == "rate_limit"


def test_with_retry_gives_up_after_max_attempts():
    calls = []

    def fn():
        calls.append(1)
        return DriverResponse(text="", error="HTTP 503: overloaded")

    resp = with_retry(fn, max_attempts=3, sleep=lambda x: None)
    assert len(calls) == 3
    assert resp.error == "HTTP 503: overloaded"
    assert resp.meta.get("retry_attempts") == 3
    assert resp.meta.get("error_class") == "retryable"


def test_timeout_retry_preserves_explicit_length_terminal():
    responses = iter([
        DriverResponse(text="", error="TimeoutError(read timed out)"),
        DriverResponse(
            text="Part one",
            error="OpenAI chat finished with finish_reason=length",
            meta={"finish_reason": "length", "stream_terminal": "length"},
        ),
    ])
    resp = with_retry(lambda: next(responses), sleep=lambda _: None)
    assert resp.text == "Part one"
    assert resp.meta["finish_reason"] == "length"
    assert resp.meta["recovery_attempted"] is True


def test_timeout_retry_preserves_failed_retry_stream_progress():
    responses = iter([
        DriverResponse(text="", error="TimeoutError(first)"),
        DriverResponse(
            text="visible retry text", error="TimeoutError(second)",
            meta={"stream_started": True, "stream_terminal": "error"},
        ),
    ])
    resp = with_retry(lambda: next(responses), sleep=lambda _: None)
    assert resp.text == "visible retry text"
    assert resp.error == "TimeoutError(second)"
    assert resp.meta["stream_started"] is True
    assert resp.meta["stream_performance"]["recovery_failure_count"] == 1


def test_timeout_retry_preserves_actionable_http_failure():
    responses = iter([
        DriverResponse(text="", error="TimeoutError(first)"),
        DriverResponse(text="", error="HTTP 401: invalid API key"),
    ])
    resp = with_retry(lambda: next(responses), sleep=lambda _: None)
    assert resp.error == "HTTP 401: invalid API key"


def test_cancel_interrupts_long_retry_after_sleep():
    calls = []
    cancelled = [False]
    sleeps = []

    def nap(seconds):
        sleeps.append(seconds)
        cancelled[0] = True

    resp = with_retry(
        lambda: calls.append(1) or DriverResponse(
            text="", error="HTTP 429: retry after 120 seconds",
        ),
        sleep=nap,
        is_cancelled=lambda: cancelled[0],
    )
    assert calls == [1]
    assert sleeps == [0.1]
    assert resp.error
