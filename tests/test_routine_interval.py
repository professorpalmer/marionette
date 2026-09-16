from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest

from harness._backend_main import ManagedScheduler
from harness.api.advice import post_routine
from harness.schedule_core import Schedule, due_fire_plan, validate_recurrence


def test_interval_requires_empty_cron_and_at_least_one_minute():
    validate_recurrence("", 90 * 60)
    with pytest.raises(ValueError):
        validate_recurrence("*/5 * * * *", 300)
    with pytest.raises(ValueError):
        validate_recurrence("", 30)


def test_ninety_minute_interval_uses_elapsed_time_not_cron(tmp_path):
    start = 1_700_000_000.0
    schedule = Schedule(
        id="r1",
        name="ping",
        objective="headless check",
        cron="",
        interval_seconds=90 * 60,
        enabled=True,
        created_at=start,
        enabled_at=start,
        last_fire_at=0.0,
    )
    early = datetime.fromtimestamp(start + 89 * 60, tz=timezone.utc)
    slots, _ = due_fire_plan(schedule, early)
    assert slots == []
    due = datetime.fromtimestamp(start + 90 * 60, tz=timezone.utc)
    slots, outcome = due_fire_plan(schedule, due)
    assert len(slots) == 1
    assert outcome.slots_fired == 1
    # A later tick without last_fire_at still coalesces; it does not stampede.
    later = datetime.fromtimestamp(start + 270 * 60, tz=timezone.utc)
    slots, outcome = due_fire_plan(schedule, later)
    assert len(slots) == 1
    assert outcome.slots_considered >= 1


def test_interval_reload_does_not_replay_after_last_fire():
    start = 1_700_000_000.0
    fired = start + 90 * 60
    schedule = Schedule(
        id="r1",
        name="ping",
        objective="headless check",
        cron="",
        interval_seconds=90 * 60,
        enabled=True,
        created_at=start,
        enabled_at=start,
        last_fire_at=fired,
    )
    now = datetime.fromtimestamp(fired + 10, tz=timezone.utc)
    slots, _ = due_fire_plan(schedule, now)
    assert slots == []


def test_managed_scheduler_start_is_idempotent_and_stop_joins():
    started = []

    class FakeDaemon:
        def __init__(self, store):
            self.store = store
            self.stopped = False
            self._stop = threading.Event()

        def serve(self, tick_seconds=1):
            started.append(tick_seconds)
            self._stop.wait(2)

        def stop(self):
            self.stopped = True
            self._stop.set()

    manager = ManagedScheduler(store_factory=lambda: "store", daemon_factory=FakeDaemon)
    assert manager.start() is True
    assert manager.start() is False
    assert manager.stop() is True
    assert manager._daemon.stopped is True
    assert started == [1]


def test_post_routine_binds_the_owned_session_driver():
    pilot = type("P", (), {})()
    pilot.config = type("C", (), {"driver": "openai/gpt-5.6", "repo": "/tmp/repo", "swarm_adapter": "agentic"})()
    captured = {}

    class Svc:
        pilot_swap_lock = None

        def get_session(self, session_id):
            return type("S", (), {})()

    def fake_owner(session_id, svc):
        if session_id != "session-A":
            return None, (400, {"error": "bad session"})
        return pilot, None

    def fake_add(body):
        captured.update(body)
        return 200, {"ok": True, "schedule": body}

    import harness.api.advice as advice
    original_owner = advice._owner
    advice._owner = fake_owner
    import harness.api.schedules as schedules
    original_add = schedules.post_schedules_add
    schedules.post_schedules_add = fake_add
    try:
        status, result = post_routine({
            "session_id": "session-A",
            "prompt": "review the board",
            "every": "90m",
            "request_id": "req-1",
        }, Svc())
    finally:
        advice._owner = original_owner
        schedules.post_schedules_add = original_add
    assert status == 200
    assert captured["interval_seconds"] == 5400
    assert captured["cron"] == ""
    assert captured["driver"] == "openai/gpt-5.6"
    assert captured["repo"] == "/tmp/repo"
    assert result["ok"] is True
