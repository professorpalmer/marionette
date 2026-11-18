from __future__ import annotations

from types import SimpleNamespace

from harness.pilot import PilotAction
from harness.runaway_guard import SIGNAL_UNCHANGED, observe_step


def _session():
    return SimpleNamespace(_runaway_guard=None)


def _wait(job):
    return PilotAction(kind="wait", arguments={"job_id": job})


def test_waits_on_different_jobs_do_not_share_unchanged_progress():
    session = _session()
    body = "(wait 30s) Jobs still running..."
    results = [
        observe_step(session, "wait", _wait(job), body, False)
        for job in ("swarm_a", "swarm_b", "swarm_c")
    ]
    assert all(SIGNAL_UNCHANGED not in r for r in results)


def test_waits_on_same_job_still_report_unchanged_progress():
    session = _session()
    body = "(wait 30s) Jobs still running..."
    results = [observe_step(session, "wait", _wait("swarm_a"), body, False) for _ in range(3)]
    assert results[0] == []
    assert results[1] == []
    assert SIGNAL_UNCHANGED in results[2]
