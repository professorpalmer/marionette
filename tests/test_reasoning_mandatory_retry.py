"""One-shot reasoning-mandatory 400 retry (dev send loop, not Hermes)."""

from __future__ import annotations

from types import SimpleNamespace

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from pmharness.drivers.base import DriverResponse
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


class _ScriptedPilot:
    name = "scripted-reasoning"

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    def chat(self, messages, tools=None, system=None):
        self.calls += 1
        return self.replies.pop(0)


def _send_session(tmp_path, monkeypatch, pilot):
    monkeypatch.setattr(
        "harness.send_loop.profile_skips_auto_inject",
        lambda session, user_message="": (True, True),
    )
    session = ConversationalSession(HarnessConfig(
        driver="stub-oracle-v2", state_dir=str(tmp_path), repo=str(tmp_path),
    ))
    session.harness_session_id = "sess-reasoning"
    session.pilot = pilot
    monkeypatch.setattr(session, "_resolve_append_only", lambda: False)
    monkeypatch.setattr(session, "_get_codegraph_context", lambda msg: "")
    # Compaction frees no bytes, so an overflow cannot retry.
    monkeypatch.setattr(session, "_maybe_compact_history", lambda **k: iter(()))
    return session


def _failed(error):
    return DriverResponse(text="", error=error, meta={"finish_reason": "error"})


def _answer():
    return DriverResponse(
        text='{"say": "all done", "actions": []}',
        tokens_out=1, latency_ms=1.0, meta={"finish_reason": "stop"},
    )


def test_send_retries_once_after_reasoning_mandatory_error(tmp_path, monkeypatch):
    pilot = _ScriptedPilot([_failed("HTTP 400: reasoning is mandatory"), _answer()])
    session = _send_session(tmp_path, monkeypatch, pilot)

    events = list(session.send("hi"))

    assert pilot.calls == 2
    assert pilot._omit_reasoning_disable is True
    assert [e for e in events if e.kind == "error"] == []
    assert len([e for e in events if e.kind == "assistant_done"]) == 1


def test_send_aborts_overflow_without_a_second_call(tmp_path, monkeypatch):
    pilot = _ScriptedPilot([
        _failed("HTTP 400: maximum context length exceeded"), _answer(),
    ])
    session = _send_session(tmp_path, monkeypatch, pilot)

    events = list(session.send("hi"))

    assert pilot.calls == 1
    errors = [e for e in events if e.kind == "error"]
    assert errors
    assert "context" in errors[0].data["error"].lower()
