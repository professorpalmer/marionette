from __future__ import annotations

"""Stop during the compaction summarizer ends the wait and keeps history.

The summarizer thread was joined for the whole compaction timeout with no
Stop check. Stop reports the session idle at once, so a late summary could
still rewrite history after the user had moved on.
"""

import threading

import pytest

from test_compaction_timeout import _OK_SUMMARY, _fat_session


@pytest.fixture(autouse=True)
def _allow_small_fixture_compaction(monkeypatch):
    monkeypatch.setattr("harness.compaction_mixin.MIN_COMPACTABLE_TOKENS", 0)
    monkeypatch.setenv("HARNESS_COMPACTION_RESIDUAL", "summary")


class _BlockedPilot:
    name = "blocked-pilot"
    base_url = "http://localhost:11434/v1"

    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def fork_for_compaction(self, *, model):
        return self

    def complete(self, prompt, *, system=None):
        from pmharness.drivers.openai_compat import DriverResponse

        self.started.set()
        self.release.wait(20)
        return DriverResponse(text=_OK_SUMMARY, tokens_out=5, latency_ms=1.0)


def test_stop_ends_the_summarizer_wait_and_keeps_history(monkeypatch):
    from harness.compaction_mixin import REASON_STOPPED

    pilot = _BlockedPilot()
    session, _ = _fat_session(monkeypatch, pilot, timeout_s="8")
    before = [dict(m) for m in session._history]
    out = {}
    worker = threading.Thread(
        target=lambda: out.update(events=list(session._maybe_compact_history(force=True))),
        daemon=True,
    )
    worker.start()
    try:
        assert pilot.started.wait(5)
        session._cancel.set()
        worker.join(3)
        assert not worker.is_alive()
        assert session._history == before
        assert session._last_compaction_attempt.get("reason") == REASON_STOPPED
        assert any(e.data.get("reason") == REASON_STOPPED for e in out["events"])
    finally:
        pilot.release.set()
    worker.join(5)
    assert session._history == before


def test_stop_flag_from_before_the_compaction_does_not_abort_it(monkeypatch):
    pilot = _BlockedPilot()
    pilot.release.set()
    session, _ = _fat_session(monkeypatch, pilot, timeout_s="8")
    session._cancel.set()
    list(session._maybe_compact_history(force=True))
    assert any(m.get("_compressed_summary") for m in session._history)
