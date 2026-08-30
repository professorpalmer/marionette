"""Dual-store cancel membership for /api/swarm/cancel.

Production cancel (``harness.api.jobs.post_swarm_cancel``) resolves jobs from
BOTH the harness session store and the per-project CLI durable store. A
single-store membership check used to 404 CLI-only jobs as "unkillable".
These tests call the production path with fakes — not a resurrected helper.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from harness.api.jobs import make_job_services, post_swarm_cancel
from harness.job_scoping import job_label_for_session, stamp_task_payload
from puppetmaster.models import Task
from puppetmaster.store_factory import create_store


def _noop(*_a, **_k):
    return None


def _job_services(*, get_pilot, get_session, cfg_repo: str = "/repo", session_id: str = ""):
    return make_job_services(
        cfg=SimpleNamespace(repo=cfg_repo),
        sessions=SimpleNamespace(active=session_id),
        get_pilot=get_pilot,
        get_session=get_session,
        diag=_noop,
    )


def test_make_job_services_fills_inert_defaults():
    """Cancel handlers must be unit-testable without a 20-field stub."""
    svc = make_job_services(
        cfg=SimpleNamespace(repo="/r"),
        get_pilot=lambda: None,
        get_session=lambda: None,
    )
    assert svc.routing_saved_usd() == 0.0
    assert svc.scoped_jobs_snapshot() == []
    assert svc.job_savings_fields("j1") == {}


class _FakeStore:
    def __init__(self, jobs, *, cancelable: bool = True):
        self._jobs = list(jobs)
        self.cancelled: list[str] = []
        self._cancelable = cancelable

    def list_jobs(self):
        return list(self._jobs)

    def list_tasks(self, job_id: str):
        return []

    def cancel_job(self, job_id: str):
        if not self._cancelable:
            raise RuntimeError("cancel_job unavailable")
        self.cancelled.append(job_id)


class _FakeState:
    def __init__(self, store: _FakeStore):
        self.store = store

    def list_jobs(self):
        return self.store.list_jobs()


class _FakeSession:
    def __init__(self, state: _FakeState):
        self._state = state

    def state(self):
        return self._state


class _FakePilot:
    def __init__(self, local_ids=None, *, session_id="", local_jobs=None, registered=None):
        self.harness_session_id = session_id
        self._session_job_ids = list(registered or [])
        self.cancelled_local: list[str] = []
        self._local_jobs: dict = {}
        for jid in local_ids or []:
            self._local_jobs[jid] = {"id": jid, "session_id": session_id}
        for job in local_jobs or []:
            self._local_jobs[job["id"]] = dict(job)

    def get_local_job(self, job_id: str):
        job = self._local_jobs.get(job_id)
        return dict(job) if job else None

    def live_local_jobs(self):
        return [dict(job) for job in self._local_jobs.values()]

    def cancel_local_job(self, job_id: str) -> bool:
        if job_id in self._local_jobs:
            self.cancelled_local.append(job_id)
            return True
        return False


def _track_request_cancel(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        "puppetmaster.cancellation.request_cancel",
        lambda job_id: calls.append(job_id),
    )
    return calls


@pytest.fixture(autouse=True)
def _silence_request_cancel(monkeypatch):
    monkeypatch.setattr(
        "puppetmaster.cancellation.request_cancel",
        lambda _job_id: None,
    )


def test_missing_job_id_is_bad_request():
    svc = _job_services(
        get_pilot=lambda: _FakePilot(),
        get_session=lambda: _FakeSession(_FakeState(_FakeStore([]))),
    )
    code, body = post_swarm_cancel({}, svc)
    assert code == 400
    assert body["ok"] is False


def test_harness_store_job_cancels_via_production_path(monkeypatch):
    label = job_label_for_session("sess-x")
    harness = _FakeStore([
        {"id": "job-a", "label": label},
        {"id": "job-b", "label": label},
    ])
    monkeypatch.setattr(
        "harness.cli_job_merge.open_cli_durable_state",
        lambda _repo="": None,
    )
    svc = _job_services(
        get_pilot=lambda: _FakePilot(),
        get_session=lambda: _FakeSession(_FakeState(harness)),
    )
    code, body = post_swarm_cancel({"job_id": "job-b"}, svc)
    assert code == 200
    assert body == {"ok": True, "job_id": "job-b", "durable": True, "marked": True}
    assert harness.cancelled == ["job-b"]


def test_cli_store_only_job_cancels_not_404(monkeypatch):
    """CLI durable jobs must resolve through the same dual-store set as reads."""
    label = job_label_for_session("sess-x")
    harness = _FakeStore([{"id": "harness-only", "label": label}])
    cli_store = _FakeStore([{"id": "cli-only", "label": label}, {}, {"goal": "x"}])
    cli_state = SimpleNamespace(store=cli_store)
    monkeypatch.setattr(
        "harness.cli_job_merge.open_cli_durable_state",
        lambda _repo="": cli_state,
    )
    svc = _job_services(
        get_pilot=lambda: _FakePilot(),
        get_session=lambda: _FakeSession(_FakeState(harness)),
    )
    code, body = post_swarm_cancel({"job_id": "cli-only"}, svc)
    assert code == 200
    assert body["ok"] is True
    assert body["job_id"] == "cli-only"
    assert body["durable"] is True
    assert body["marked"] is True
    assert cli_store.cancelled == ["cli-only"]
    assert harness.cancelled == []


def test_unknown_job_id_returns_404(monkeypatch):
    label = job_label_for_session("sess-x")
    harness = _FakeStore([{"id": "job-a", "label": label}])
    cli_store = _FakeStore([{"id": "cli-a", "label": label}])
    monkeypatch.setattr(
        "harness.cli_job_merge.open_cli_durable_state",
        lambda _repo="": SimpleNamespace(store=cli_store),
    )
    svc = _job_services(
        get_pilot=lambda: _FakePilot(),
        get_session=lambda: _FakeSession(_FakeState(harness)),
    )
    code, body = post_swarm_cancel({"job_id": "job-zzz"}, svc)
    assert code == 404
    assert body == {"ok": False, "error": "unknown job_id", "job_id": "job-zzz"}


def test_malformed_rows_do_not_match_or_raise(monkeypatch):
    harness = _FakeStore([{}, {"goal": "x"}, {"id": "real-job", "label": job_label_for_session("sess-x")}])
    monkeypatch.setattr(
        "harness.cli_job_merge.open_cli_durable_state",
        lambda _repo="": None,
    )
    svc = _job_services(
        get_pilot=lambda: _FakePilot(),
        get_session=lambda: _FakeSession(_FakeState(harness)),
    )
    code_ok, body_ok = post_swarm_cancel({"job_id": "real-job"}, svc)
    assert code_ok == 200
    assert body_ok["ok"] is True

    code_bad, body_bad = post_swarm_cancel({"job_id": ""}, svc)
    assert code_bad == 400
    assert body_bad["ok"] is False


def test_known_unowned_job_cancel_looks_unknown(monkeypatch):
    harness = _FakeStore([{"id": "foreign-cli", "goal": "unstamped leftover"}])
    monkeypatch.setattr(
        "harness.cli_job_merge.open_cli_durable_state",
        lambda _repo="": None,
    )
    svc = _job_services(
        get_pilot=lambda: _FakePilot(),
        get_session=lambda: _FakeSession(_FakeState(harness)),
    )
    code, body = post_swarm_cancel({"job_id": "foreign-cli"}, svc)
    assert code == 404
    assert body == {"ok": False, "error": "unknown job_id", "job_id": "foreign-cli"}
    assert harness.cancelled == []


def test_known_unowned_cli_job_cancel_looks_unknown(monkeypatch):
    harness = _FakeStore([])
    cli_store = _FakeStore([{"id": "cli-foreign", "goal": "unstamped leftover"}])
    monkeypatch.setattr(
        "harness.cli_job_merge.open_cli_durable_state",
        lambda _repo="": SimpleNamespace(store=cli_store),
    )
    svc = _job_services(
        get_pilot=lambda: _FakePilot(),
        get_session=lambda: _FakeSession(_FakeState(harness)),
    )
    code, body = post_swarm_cancel({"job_id": "cli-foreign"}, svc)
    assert code == 404
    assert body == {"ok": False, "error": "unknown job_id", "job_id": "cli-foreign"}
    assert cli_store.cancelled == []


def test_local_pilot_cancel_short_circuits_before_stores(monkeypatch):
    harness = _FakeStore([{"id": "local-1"}])
    calls = {"cli": 0}
    tripped = _track_request_cancel(monkeypatch)

    def _open_cli(_repo=""):
        calls["cli"] += 1
        return None

    monkeypatch.setattr("harness.cli_job_merge.open_cli_durable_state", _open_cli)
    pilot = _FakePilot(local_ids={"local-1"}, session_id="sess-x")
    svc = _job_services(
        get_pilot=lambda: pilot,
        get_session=lambda: _FakeSession(_FakeState(harness)),
        session_id="sess-x",
    )
    code, body = post_swarm_cancel({"job_id": "local-1"}, svc)
    assert code == 200
    assert body == {"ok": True, "job_id": "local-1"}
    assert pilot.cancelled_local == ["local-1"]
    assert harness.cancelled == []
    assert calls["cli"] == 0
    assert tripped == ["local-1"]


def _job_status(store, job_id: str) -> str:
    job = store.get_job(job_id)
    raw = getattr(job, "status", None)
    return str(getattr(raw, "value", raw) or "")


def _seed_sibling_store(tmp_path, *, owned: bool):
    sibling = tmp_path / "sibling-state"
    store = create_store("sqlite", str(sibling))
    job = store.create_job("sibling job")
    payload = (
        stamp_task_payload({}, session_id="sess-x", origin="marionette")
        if owned
        else {"note": "unstamped leftover"}
    )
    store.save_task(Task(
        job_id=job.id,
        role="implement",
        instruction="do work",
        adapter="agentic",
        payload=payload,
    ))
    store.update_job_status(job.id, "running")
    return store, sibling, job.id


def _sibling_cancel_svc(monkeypatch, sibling_dir, *, registered=None):
    monkeypatch.setenv("HARNESS_CLI_CROSS_PROJECT", "1")
    monkeypatch.setattr(
        "harness.cli_job_merge.open_cli_durable_state",
        lambda _repo="": None,
    )
    monkeypatch.setattr(
        "puppetmaster.state.list_project_state_dirs",
        lambda: [sibling_dir],
    )

    def _forbidden_dual(*_a, **_k):
        raise AssertionError("cancel_job_dual_store must not run after a primary miss")

    monkeypatch.setattr("harness.job_cancel.cancel_job_dual_store", _forbidden_dual)
    harness = _FakeStore([])
    pilot = _FakePilot()
    if registered is not None:
        pilot._session_job_ids = list(registered)
    return _job_services(
        get_pilot=lambda: pilot,
        get_session=lambda: _FakeSession(_FakeState(harness)),
    ), harness


def test_foreign_sibling_cancel_refused_status_unchanged(tmp_path, monkeypatch):
    store, sibling_dir, job_id = _seed_sibling_store(tmp_path, owned=False)
    svc, _harness = _sibling_cancel_svc(monkeypatch, sibling_dir, registered=[job_id])
    before = _job_status(store, job_id)
    code, body = post_swarm_cancel({"job_id": job_id}, svc)
    assert code == 404
    assert body == {"ok": False, "error": "unknown job_id", "job_id": job_id}
    assert _job_status(store, job_id) == before
    assert _job_status(store, job_id) == "running"


def test_owned_sibling_task_stamp_cancel_succeeds(tmp_path, monkeypatch):
    store, sibling_dir, job_id = _seed_sibling_store(tmp_path, owned=True)
    svc, harness = _sibling_cancel_svc(monkeypatch, sibling_dir)
    tripped = _track_request_cancel(monkeypatch)
    code, body = post_swarm_cancel({"job_id": job_id}, svc)
    assert code == 200
    assert body["ok"] is True
    assert body["job_id"] == job_id
    assert body["durable"] is True
    assert body["marked"] is True
    assert _job_status(store, job_id) == "cancelled"
    assert harness.cancelled == []
    assert tripped == [job_id]


def test_unknown_job_does_not_trip_request_cancel(monkeypatch):
    tripped = _track_request_cancel(monkeypatch)
    monkeypatch.setattr("harness.cli_job_merge.open_cli_durable_state", lambda _repo="": None)
    svc = _job_services(
        get_pilot=lambda: _FakePilot(),
        get_session=lambda: _FakeSession(_FakeState(_FakeStore([]))),
    )
    code, body = post_swarm_cancel({"job_id": "job-zzz"}, svc)
    assert code == 404
    assert body == {"ok": False, "error": "unknown job_id", "job_id": "job-zzz"}
    assert tripped == []


def test_unowned_primary_does_not_trip_request_cancel(monkeypatch):
    tripped = _track_request_cancel(monkeypatch)
    harness = _FakeStore([{"id": "foreign-cli", "goal": "unstamped leftover"}])
    monkeypatch.setattr("harness.cli_job_merge.open_cli_durable_state", lambda _repo="": None)
    svc = _job_services(
        get_pilot=lambda: _FakePilot(),
        get_session=lambda: _FakeSession(_FakeState(harness)),
    )
    code, _body = post_swarm_cancel({"job_id": "foreign-cli"}, svc)
    assert code == 404
    assert harness.cancelled == []
    assert tripped == []


def test_foreign_sibling_does_not_trip_request_cancel(tmp_path, monkeypatch):
    store, sibling_dir, job_id = _seed_sibling_store(tmp_path, owned=False)
    svc, _harness = _sibling_cancel_svc(monkeypatch, sibling_dir, registered=[job_id])
    tripped = _track_request_cancel(monkeypatch)
    code, _body = post_swarm_cancel({"job_id": job_id}, svc)
    assert code == 404
    assert tripped == []
    assert _job_status(store, job_id) == "running"


def test_foreign_local_session_does_not_trip_or_cancel(monkeypatch):
    tripped = _track_request_cancel(monkeypatch)
    harness = _FakeStore([])
    monkeypatch.setattr("harness.cli_job_merge.open_cli_durable_state", lambda _repo="": None)
    pilot = _FakePilot(
        local_jobs=[{"id": "local-other", "session_id": "sess-other"}],
        session_id="sess-x",
    )
    svc = _job_services(
        get_pilot=lambda: pilot,
        get_session=lambda: _FakeSession(_FakeState(harness)),
        session_id="sess-x",
    )
    code, body = post_swarm_cancel({"job_id": "local-other"}, svc)
    assert code == 404
    assert body == {"ok": False, "error": "unknown job_id", "job_id": "local-other"}
    assert pilot.cancelled_local == []
    assert tripped == []


def test_owned_durable_trips_request_cancel(monkeypatch):
    tripped = _track_request_cancel(monkeypatch)
    label = job_label_for_session("sess-x")
    harness = _FakeStore([{"id": "job-a", "label": label}])
    monkeypatch.setattr("harness.cli_job_merge.open_cli_durable_state", lambda _repo="": None)
    svc = _job_services(
        get_pilot=lambda: _FakePilot(),
        get_session=lambda: _FakeSession(_FakeState(harness)),
    )
    code, body = post_swarm_cancel({"job_id": "job-a"}, svc)
    assert code == 200
    assert body["ok"] is True
    assert harness.cancelled == ["job-a"]
    assert tripped == ["job-a"]
