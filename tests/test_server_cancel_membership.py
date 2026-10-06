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
from tests.test_scoped_job_artifacts import stores
from tests.test_scoped_job_cancel import selection, store_dump
from puppetmaster.state import state_identity
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
    assert svc.tool_output_savings_fields() == {}


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
        self._local_metadata = SimpleNamespace(incarnation="local-fixture-incarnation")
        self.harness_session_id = session_id
        self._session_job_ids = list(registered or [])
        self.cancelled_local: list[str] = []
        self._local_jobs: dict = {}
        for jid in local_ids or []:
            self._local_jobs[jid] = {"id": jid, "session_id": session_id, "cwd": "/repo"}
        for job in local_jobs or []:
            self._local_jobs[job["id"]] = dict(job)

    def get_local_job(self, job_id: str):
        job = self._local_jobs.get(job_id)
        return dict(job) if job else None

    def live_local_jobs(self):
        return [dict(job) for job in self._local_jobs.values()]

    def cancel_local_job(self, job_id: str, *, incarnation: str) -> bool:
        if incarnation == self._local_metadata.incarnation and job_id in self._local_jobs:
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


@pytest.mark.parametrize('source', ['harness', 'cli'])
def test_owned_store_job_requires_scoped_kernel_stop(stores, source, monkeypatch):
    primary, cli, svc, qs = stores
    tripped = _track_request_cancel(monkeypatch)
    chosen = primary if source == 'harness' else cli
    jid = qs['job_id'][0]
    chosen.store.update_job_status(jid, 'running')
    before = [store_dump(state.store) for state in (primary, cli)]
    code, body = post_swarm_cancel({'selection': selection(qs,
        source=source, state_id=state_identity(chosen.store.root))}, svc)
    assert code == 409
    assert body['ok'] is False
    assert body['code'] == 'scoped_kernel_cancellation_required'
    assert [store_dump(state.store) for state in (primary, cli)] == before
    assert tripped == []


def test_unknown_job_id_is_unavailable(monkeypatch):
    tripped = _track_request_cancel(monkeypatch)
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
    assert code == 409
    assert body["ok"] is False
    assert body["code"] == "job_cancel_unavailable"
    assert tripped == []


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
    assert code_ok == 409
    assert body_ok["ok"] is False

    code_bad, body_bad = post_swarm_cancel({"job_id": ""}, svc)
    assert code_bad == 400
    assert body_bad["ok"] is False


def test_known_unowned_job_cancel_looks_unknown(monkeypatch):
    tripped = _track_request_cancel(monkeypatch)
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
    assert code == 409
    assert body["ok"] is False
    assert body["code"] == "job_cancel_unavailable"
    assert harness.cancelled == []
    assert tripped == []


def test_known_unowned_cli_job_cancel_looks_unknown(monkeypatch):
    tripped = _track_request_cancel(monkeypatch)
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
    assert code == 409
    assert body["ok"] is False
    assert body["code"] == "job_cancel_unavailable"
    assert cli_store.cancelled == []
    assert tripped == []


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
    code, body = post_swarm_cancel({"selection": {"version": 1, "source": "local",
        "repo": "/repo", "session_id": "sess-x",
        "local_incarnation": pilot._local_metadata.incarnation,
        "job_ref": {"job_id": "local-1", "state_id": None}}}, svc)
    assert code == 200
    assert body["ok"] is True
    assert body["job_id"] == "local-1"
    assert body["cancellation"] == "local_event"
    assert pilot.cancelled_local == ["local-1"]
    assert harness.cancelled == []
    assert calls["cli"] == 0
    assert tripped == []


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
    tripped = _track_request_cancel(monkeypatch)
    before = _job_status(store, job_id)
    snapshot = store_dump(store)
    code, body = post_swarm_cancel({"job_id": job_id}, svc)
    assert store_dump(store) == snapshot
    assert code == 409
    assert body["ok"] is False
    assert body["code"] == "job_cancel_unavailable"
    assert tripped == []
    assert _job_status(store, job_id) == before
    assert _job_status(store, job_id) == "running"


def test_owned_sibling_task_stamp_cancel_refuses(tmp_path, monkeypatch):
    store, sibling_dir, job_id = _seed_sibling_store(tmp_path, owned=True)
    svc, harness = _sibling_cancel_svc(monkeypatch, sibling_dir)
    tripped = _track_request_cancel(monkeypatch)
    snapshot = store_dump(store)
    code, body = post_swarm_cancel({"job_id": job_id}, svc)
    assert store_dump(store) == snapshot
    assert code == 409
    assert body["ok"] is False
    assert _job_status(store, job_id) == "running"
    assert harness.cancelled == []
    assert tripped == []


def test_foreign_local_session_does_not_trip_or_cancel(monkeypatch):
    tripped = _track_request_cancel(monkeypatch)
    harness = _FakeStore([])
    monkeypatch.setattr("harness.cli_job_merge.open_cli_durable_state", lambda _repo="": None)
    pilot = _FakePilot(
        local_jobs=[{"id": "local-other", "session_id": "sess-other", "cwd": "/repo"}],
        session_id="sess-x",
    )
    svc = _job_services(
        get_pilot=lambda: pilot,
        get_session=lambda: _FakeSession(_FakeState(harness)),
        session_id="sess-x",
    )
    code, body = post_swarm_cancel({"selection": {"version": 1, "source": "local",
        "repo": "/repo", "session_id": "sess-x",
        "local_incarnation": pilot._local_metadata.incarnation,
        "job_ref": {"job_id": "local-other", "state_id": None}}}, svc)
    assert code == 409
    assert body["ok"] is False
    assert body["code"] == "job_cancel_unavailable"
    assert pilot.cancelled_local == []
    assert tripped == []
