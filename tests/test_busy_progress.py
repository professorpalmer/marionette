from __future__ import annotations

import tempfile
import time

from harness.config import HarnessConfig
from harness.conversation import ConvEvent, ConversationalSession


def _session():
    return ConversationalSession(HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp()))


def test_reap_uses_inactivity_not_lock_age(monkeypatch):
    session = _session()
    monkeypatch.setenv("HARNESS_TURN_DEADLINE_SECONDS", "10")
    assert session._busy.acquire(blocking=False)
    generation = session._mark_busy_acquired()
    session._busy_since = time.monotonic() - 600
    session._busy_last_progress = time.monotonic()

    assert session._reap_stuck_turn() is False
    assert session._busy.locked()
    session._release_busy(generation)


def test_reap_releases_a_truly_inactive_turn(monkeypatch):
    session = _session()
    monkeypatch.setenv("HARNESS_TURN_DEADLINE_SECONDS", "10")
    assert session._busy.acquire(blocking=False)
    session._mark_busy_acquired()
    session._busy_since = time.monotonic() - 600
    session._busy_last_progress = time.monotonic() - 11

    assert session._reap_stuck_turn() is True
    assert not session._busy.locked()
    assert session._busy_last_progress == 0.0


def test_stale_generation_cannot_refresh_new_owner_progress():
    session = _session()
    assert session._busy.acquire(blocking=False)
    old_generation = session._mark_busy_acquired()
    session._release_busy(old_generation)
    assert session._busy.acquire(blocking=False)
    new_generation = session._mark_busy_acquired()
    before = session._busy_last_progress

    assert session._note_busy_progress(old_generation) is False
    assert session._busy_last_progress == before
    assert session._note_busy_progress(new_generation) is True
    session._release_busy(new_generation)


def test_short_send_stale_window_respects_recent_progress(monkeypatch):
    session = _session()
    monkeypatch.setenv("HARNESS_SEND_STALE_SECONDS", "1")
    assert session._busy.acquire(blocking=False)
    session._mark_busy_acquired()
    session._busy_since = time.monotonic() - 600
    session._busy_last_progress = time.monotonic()
    session._state = "thinking"

    assert session._acquire_send_turn_unreserved() is None
    assert session._busy.locked()


def test_synthetic_notice_does_not_refresh_watchdog_progress():
    session = _session()
    progress_at = time.monotonic() - 30

    def notices_only(*_args, **_kwargs):
        session._busy_last_progress = progress_at
        yield ConvEvent("notice", {"kind": "wait", "message": "Still waiting"})

    session._send_locked = notices_only
    stream = session.send("hello")
    assert next(stream).kind == "notice"
    assert session._busy_last_progress == progress_at
    stream.close()
