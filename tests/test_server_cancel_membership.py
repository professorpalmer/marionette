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


def _noop(*_a, **_k):
    return None


def _job_services(*, get_pilot, get_session, cfg_repo: str = "/repo"):
    return make_job_services(
        cfg=SimpleNamespace(repo=cfg_repo),
        sessions=SimpleNamespace(),
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
    def __init__(self, local_ids=None):
        self._local_ids = set(local_ids or [])
        self.cancelled_local: list[str] = []

    def cancel_local_job(self, job_id: str) -> bool:
        if job_id in self._local_ids:
            self.cancelled_local.append(job_id)
            return True
        return False


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
    harness = _FakeStore([{"id": "job-a"}, {"id": "job-b"}])
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
    harness = _FakeStore([{"id": "harness-only"}])
    cli_store = _FakeStore([{"id": "cli-only"}, {}, {"goal": "x"}])
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
    harness = _FakeStore([{"id": "job-a"}])
    cli_store = _FakeStore([{"id": "cli-a"}])
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
    harness = _FakeStore([{}, {"goal": "x"}, {"id": "real-job"}])
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


def test_local_pilot_cancel_short_circuits_before_stores(monkeypatch):
    harness = _FakeStore([{"id": "local-1"}])
    calls = {"cli": 0}

    def _open_cli(_repo=""):
        calls["cli"] += 1
        return None

    monkeypatch.setattr("harness.cli_job_merge.open_cli_durable_state", _open_cli)
    pilot = _FakePilot(local_ids={"local-1"})
    svc = _job_services(
        get_pilot=lambda: pilot,
        get_session=lambda: _FakeSession(_FakeState(harness)),
    )
    code, body = post_swarm_cancel({"job_id": "local-1"}, svc)
    assert code == 200
    assert body == {"ok": True, "job_id": "local-1"}
    assert pilot.cancelled_local == ["local-1"]
    assert harness.cancelled == []
    assert calls["cli"] == 0
