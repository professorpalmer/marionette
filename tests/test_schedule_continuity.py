"""Cron continuity digest + failure_deliver (route | suppress).

Fresh session per fire. Continuity is injected prompt context, not a
long-lived ConversationalSession. No second scheduler.
"""
from __future__ import annotations

import subprocess
from datetime import datetime

from harness.schedule_core import (
    FAILURE_DELIVER_ROUTE,
    FAILURE_DELIVER_SUPPRESS,
    Schedule,
    fire_continuity_digest,
)
from harness.schedule_store import ScheduleStore
from harness.scheduler import Notifier, run_due


_OK_REASON = "pilot reports objective met (no further investigation)"


class _Event:
    def __init__(self, kind, data):
        self.kind = kind
        self.data = data


class _TrackSession:
    """Records the objective each fire receives."""

    def __init__(self, recorder, reason):
        self._recorder = recorder
        self._reason = reason

    def run_auto(self, objective, budget=None, **_kw):
        self._recorder.append(objective)
        yield _Event("auto_status", {
            "cycle": 1,
            "snapshot": {"tokens_used": 1, "swarms_used": 0},
        })
        yield _Event("auto_halt", {
            "reason": self._reason,
            "snapshot": {"tokens_used": 1, "swarms_used": 0},
        })


class _CountingNotifier(Notifier):
    def __init__(self):
        self.calls = []

    def notify(self, schedule, run):
        self.calls.append((schedule.id, dict(run)))


def _git_repo(tmp_path, name="repo"):
    root = tmp_path / name
    root.mkdir()
    subprocess.run(
        ["git", "init"], cwd=str(root), check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return str(root)


def _store(tmp_path):
    return ScheduleStore(str(tmp_path / "s.sqlite"))


def _budget(_sched):
    return object()


def test_continuity_digest_persists_across_two_fires(tmp_path):
    store = _store(tmp_path)
    repo = _git_repo(tmp_path)
    sched = store.add(Schedule(
        id="",
        name="watch",
        objective="check the build",
        cron="* * * * *",
        repo=repo,
        monitor_mode=True,
        notepad="watch flake rate",
    ))
    seen = []

    def factory(_sched):
        return _TrackSession(seen, _OK_REASON)

    now1 = datetime(2024, 1, 1, 12, 0)
    now2 = datetime(2024, 1, 1, 12, 1)
    expected_digest = fire_continuity_digest(_OK_REASON)

    run_due(
        store, now1, notifier=_CountingNotifier(),
        session_factory=factory, budget_factory=_budget,
    )
    after_first = store.get(sched.id)
    assert after_first is not None
    assert after_first.last_status == "ok"
    assert after_first.continuity_digest == expected_digest
    assert len(seen) == 1
    # First fire: notepad only (no prior digest).
    assert "watch flake rate" in seen[0]
    assert "last_digest:" not in seen[0]
    assert seen[0].endswith("check the build")

    run_due(
        store, now2, notifier=_CountingNotifier(),
        session_factory=factory, budget_factory=_budget,
    )
    after_second = store.get(sched.id)
    assert after_second is not None
    assert after_second.continuity_digest == expected_digest
    assert after_second.last_status == "ok"
    assert len(seen) == 2
    assert "last_digest: %s" % expected_digest in seen[1]
    assert "watch flake rate" in seen[1]
    assert seen[1].endswith("check the build")


def test_failure_deliver_suppress_skips_notify(tmp_path):
    store = _store(tmp_path)
    repo = _git_repo(tmp_path, name="repo-suppress")
    sched = store.add(Schedule(
        id="",
        name="quiet",
        objective="o",
        cron="* * * * *",
        repo=repo,
        failure_deliver=FAILURE_DELIVER_SUPPRESS,
    ))
    notifier = _CountingNotifier()

    def factory(_sched):
        raise RuntimeError("boom")

    runs = run_due(
        store, datetime(2024, 1, 1, 12, 0),
        notifier=notifier, session_factory=factory, budget_factory=_budget,
    )
    real = [r for r in runs if r.get("status") != "blocked"]
    assert len(real) == 1
    assert real[0]["status"] == "error"
    assert notifier.calls == []
    persisted = store.get(sched.id)
    assert persisted is not None
    assert persisted.last_status == "error"
    assert persisted.continuity_digest == ""


def test_failure_deliver_route_still_notifies(tmp_path):
    store = _store(tmp_path)
    repo = _git_repo(tmp_path, name="repo-route")
    sched = store.add(Schedule(
        id="",
        name="loud",
        objective="o",
        cron="* * * * *",
        repo=repo,
        failure_deliver=FAILURE_DELIVER_ROUTE,
    ))
    notifier = _CountingNotifier()

    def factory(_sched):
        raise RuntimeError("boom")

    runs = run_due(
        store, datetime(2024, 1, 1, 12, 0),
        notifier=notifier, session_factory=factory, budget_factory=_budget,
    )
    real = [r for r in runs if r.get("status") != "blocked"]
    assert len(real) == 1
    assert real[0]["status"] == "error"
    assert len(notifier.calls) == 1
    assert notifier.calls[0][0] == sched.id
    assert notifier.calls[0][1]["status"] == "error"
    persisted = store.get(sched.id)
    assert persisted is not None
    assert persisted.last_status == "error"


def test_continuity_fields_persist_on_add_and_update(tmp_path):
    store = _store(tmp_path)
    sched = store.add(Schedule(
        id="",
        name="n",
        objective="o",
        cron="0 * * * *",
        monitor_mode=True,
        notepad="keep",
        failure_deliver=FAILURE_DELIVER_SUPPRESS,
    ))
    got = store.get(sched.id)
    assert got is not None
    assert got.monitor_mode is True
    assert got.notepad == "keep"
    assert got.failure_deliver == FAILURE_DELIVER_SUPPRESS
    assert got.continuity_digest == ""

    updated = store.update_fields(
        sched.id, monitor_mode=False, notepad="", failure_deliver="route",
    )
    assert updated is not None
    assert updated.monitor_mode is False
    assert updated.notepad == ""
    assert updated.failure_deliver == FAILURE_DELIVER_ROUTE
