"""Hermetic HTTP schedule control-plane handlers (tmp HARNESS_STATE_DIR)."""
from __future__ import annotations

import pytest

from harness.api import schedules as sched_api
from harness.schedule_store import ScheduleStore, default_db_path


@pytest.fixture
def sched_state(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    # Re-bind default path after env is set.
    assert default_db_path() == state / "schedules.sqlite"
    return state


def test_get_schedules_empty(sched_state):
    status, body = sched_api.get_schedules()
    assert status == 200
    assert body == {"schedules": []}


def test_add_list_enable_disable_history(sched_state):
    status, added = sched_api.post_schedules_add({
        "name": "nightly",
        "objective": "audit",
        "cron": "0 2 * * *",
    })
    assert status == 200
    assert added["timezone"] == ""
    assert added["timezone_mode"] == "host_local"
    assert added["display_status"]
    assert isinstance(added["next_fires"], list)
    sid = added["id"]

    status, listed = sched_api.get_schedules()
    assert status == 200
    assert len(listed["schedules"]) == 1
    assert listed["schedules"][0]["id"] == sid

    status, disabled = sched_api.post_schedules_disable({"id": sid})
    assert status == 200
    assert disabled["enabled"] is False

    status, enabled = sched_api.post_schedules_enable({"id": sid})
    assert status == 200
    assert enabled["enabled"] is True

    store = ScheduleStore(str(default_db_path()))
    try:
        store.record_run(sid, 1.0, 2.0, "ok", halt_reason="objective met")
    finally:
        store.close()

    status, hist = sched_api.get_schedules_history(sid, "10")
    assert status == 200
    assert hist["id"] == sid
    assert hist["runs"][0]["status"] == "ok"


def test_add_rejects_nonempty_timezone_iana_deferred(sched_state):
    status, body = sched_api.post_schedules_add({
        "name": "bad",
        "objective": "o",
        "cron": "0 0 * * *",
        "timezone": "America/New_York",
    })
    assert status == 400
    assert "IANA" in body["error"]
    assert "deferred" in body["error"].lower()


def test_update_rejects_nonempty_timezone_iana_deferred(sched_state):
    status, added = sched_api.post_schedules_add({
        "name": "x", "objective": "o", "cron": "0 0 * * *",
    })
    assert status == 200
    status, body = sched_api.post_schedules_update({
        "id": added["id"], "timezone": "UTC",
    })
    assert status == 400
    assert "IANA" in body["error"]


def test_add_allows_empty_timezone_host_local(sched_state):
    status, added = sched_api.post_schedules_add({
        "name": "local", "objective": "o", "cron": "0 0 * * *",
        "timezone": "",
    })
    assert status == 200
    assert added["timezone"] == ""
    assert added["timezone_mode"] == "host_local"


def test_remove_and_missing(sched_state):
    status, added = sched_api.post_schedules_add({
        "name": "gone", "objective": "o", "cron": "* * * * *",
    })
    sid = added["id"]
    status, body = sched_api.post_schedules_remove({"id": sid})
    assert status == 200
    assert body["ok"] is True
    status, body = sched_api.post_schedules_remove({"id": sid})
    assert status == 404


def test_run_now_unknown(sched_state):
    status, body = sched_api.post_schedules_run_now({"id": "missing"})
    assert status == 404


def test_run_now_ok(sched_state, tmp_path, monkeypatch):
    import subprocess

    import harness.scheduler as sched_mod

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init"], cwd=str(repo), check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    status, added = sched_api.post_schedules_add({
        "name": "now",
        "objective": "o",
        "cron": "0 0 * * *",
        "repo": str(repo),
    })
    assert status == 200

    def ok_session(sched):
        class S:
            def run_auto(self, objective, budget=None, *, require_codegraph=True):
                class E:
                    kind = "auto_halt"
                    data = {
                        "reason": "objective met and verified",
                        "snapshot": {},
                    }
                yield E()

            def cancel(self):
                pass
        return S()

    monkeypatch.setattr(sched_mod, "_default_session_factory", ok_session)
    monkeypatch.setattr(sched_mod, "_default_budget_factory", lambda s: object())
    status, body = sched_api.post_schedules_run_now({"id": added["id"]})
    assert status == 200
    assert body["ok"] is True
    assert body["run"]["status"] == "ok"


def test_history_requires_id(sched_state):
    status, body = sched_api.get_schedules_history("", "5")
    assert status == 400
