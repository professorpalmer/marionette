"""One-shot reasoning-mandatory 400 retry (dest send loop, not Hermes)."""

from __future__ import annotations

from types import SimpleNamespace

from harness.send_loop import maybe_retry_reasoning_mandatory


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
