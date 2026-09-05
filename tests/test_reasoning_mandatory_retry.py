"""One-shot reasoning-mandatory 400 retry (dest send loop, not Hermes)."""

from __future__ import annotations

import inspect
from types import SimpleNamespace

from harness.conversation import ConversationalSession
from harness.send_loop import (
    OVERFLOW_RETRY_MAX_ATTEMPTS,
    PROVIDER_ATTEMPT_ABORT,
    PROVIDER_ATTEMPT_RETRY,
    PROVIDER_ATTEMPT_SETTLE,
    iter_provider_attempt_recovery,
    maybe_retry_reasoning_mandatory,
)


def _drain(gen):
    events = []
    try:
        while True:
            events.append(next(gen))
    except StopIteration as stop:
        return stop.value, events


def test_maybe_retry_reasoning_mandatory_one_shot():
    session = SimpleNamespace(pilot=SimpleNamespace())
    resp = SimpleNamespace(error="HTTP 400: reasoning is mandatory")

    assert maybe_retry_reasoning_mandatory(session, resp) is True
    assert session._reasoning_disable_rejected is True
    assert session.pilot._omit_reasoning_disable is True

    assert maybe_retry_reasoning_mandatory(session, resp) is False
    assert session._reasoning_disable_rejected is True
    assert session.pilot._omit_reasoning_disable is True


def test_maybe_retry_reasoning_mandatory_missing_error():
    session = SimpleNamespace(pilot=SimpleNamespace())
    resp = SimpleNamespace()

    assert maybe_retry_reasoning_mandatory(session, resp) is False
    assert getattr(session, "_reasoning_disable_rejected", False) is False
    assert getattr(session.pilot, "_omit_reasoning_disable", False) is False


def test_maybe_retry_reasoning_mandatory_unrelated_400():
    session = SimpleNamespace(pilot=SimpleNamespace())
    resp = SimpleNamespace(error="HTTP 400: invalid_request — unknown parameter foo")

    assert maybe_retry_reasoning_mandatory(session, resp) is False
    assert getattr(session, "_reasoning_disable_rejected", False) is False
    assert getattr(session.pilot, "_omit_reasoning_disable", False) is False


def test_maybe_retry_reasoning_mandatory_missing_pilot():
    session = SimpleNamespace()
    resp = SimpleNamespace(error="HTTP 400: reasoning is mandatory")

    assert maybe_retry_reasoning_mandatory(session, resp) is True
    assert session._reasoning_disable_rejected is True
    assert maybe_retry_reasoning_mandatory(session, resp) is False


def test_iter_provider_attempt_recovery_reasoning_mandatory_retries():
    session = SimpleNamespace(pilot=SimpleNamespace())
    resp = SimpleNamespace(error="HTTP 400: reasoning is mandatory")

    action, events = _drain(iter_provider_attempt_recovery(session, resp, 0, {}))
    assert action == PROVIDER_ATTEMPT_RETRY
    assert events == []
    assert session.pilot._omit_reasoning_disable is True

    action, events = _drain(iter_provider_attempt_recovery(session, resp, 1, {}))
    assert action == PROVIDER_ATTEMPT_SETTLE
    assert events == []


def test_iter_provider_attempt_recovery_unrelated_error_settles():
    session = SimpleNamespace(pilot=SimpleNamespace())
    resp = SimpleNamespace(error="HTTP 400: invalid_request — unknown parameter foo")

    action, events = _drain(iter_provider_attempt_recovery(session, resp, 0, {}))
    assert action == PROVIDER_ATTEMPT_SETTLE
    assert events == []


def test_iter_provider_attempt_recovery_missing_error_settles():
    session = SimpleNamespace(pilot=SimpleNamespace())
    resp = SimpleNamespace()

    action, events = _drain(iter_provider_attempt_recovery(session, resp, 0, {}))
    assert action == PROVIDER_ATTEMPT_SETTLE
    assert events == []


def test_iter_provider_attempt_recovery_overflow_last_attempt_aborts():
    session = SimpleNamespace(
        _estimate_context_tokens=lambda: 80_000,
        _humanize_pilot_error=lambda text: text,
    )
    resp = SimpleNamespace(error="HTTP 400: maximum context length exceeded")

    action, events = _drain(
        iter_provider_attempt_recovery(
            session, resp, OVERFLOW_RETRY_MAX_ATTEMPTS - 1, {},
        )
    )
    assert action == PROVIDER_ATTEMPT_ABORT
    assert events


def test_send_locked_inner_honors_provider_attempt_recovery_actions():
    src = inspect.getsource(ConversationalSession._send_locked_inner)
    retry_at = src.find("PROVIDER_ATTEMPT_RETRY")
    abort_at = src.find("PROVIDER_ATTEMPT_ABORT")
    helper_at = src.find("iter_provider_attempt_recovery")
    assert helper_at != -1
    assert retry_at != -1
    assert abort_at != -1
    assert helper_at < retry_at < abort_at
    assert "continue" in src[retry_at:retry_at + 80]
    assert "return" in src[abort_at:abort_at + 80]
