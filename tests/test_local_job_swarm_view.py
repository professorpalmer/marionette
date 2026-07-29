"""Read-model projection for local jobs at /api/swarm/live merge boundary."""
from __future__ import annotations

import json
import shutil
import tempfile
import threading
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from harness.local_job_swarm_view import (
    merge_local_jobs_into_swarm_live,
    project_local_job_for_swarm_live,
)
from harness.local_jobs import LocalJobsMixin
from puppetmaster.models import Task
from puppetmaster.store_factory import create_store


_STORE_LIVE_KEYS = frozenset({
    "id",
    "goal",
    "status",
    "role",
    "adapter",
    "model",
    "created_at",
    "task_count",
    "tokens",
    "est_cost_usd",
    "cost_provenance",
    "estimated",
    "tokens_cached",
    "routing_saved_usd",
    "routing_savings_basis",
    "routing_tokens_compared",
    "routing_savings_counted",
    "delegation_saved_usd",
    "delegation_savings_basis",
    "delegation_tokens_compared",
    "delegation_savings_counted",
    "cache_saved_usd",
    "artifacts",
    "artifacts_complete",
    "tasks",
    "source",
})


def _sample_local_job(**overrides) -> dict:
    base = {
        "id": "local-abc",
        "goal": "edit files",
        "status": "running",
        "role": "implement",
        "adapter": "agentic",
        "model": "agentic/cheap-model",
        "session_id": "sess-1",
        "cwd": "/tmp/repo",
        "created_at": 1_700_000_000.0,
        "updated_at": 1_700_000_010.0,
        "task_count": 1,
        "tokens": 0,
        "est_cost_usd": 0.01,
        "routing_saved_usd": 0.04,
        "routing_savings_basis": "estimated",
        "artifacts": [{
            "type": "ROUTING",
            "headline": "Routed to cheap-model",
            "policy": "balanced",
            "est_cost_usd": 0.01,
        }],
        "tasks": [{
            "id": "local-abc-w0",
            "role": "implement (agentic)",
            "instruction": "edit files",
            "status": "running",
            "adapter": "agentic",
        }],
        "actions": [{
            "action_id": "a1",
            "kind": "read_file",
            "status": "running",
        }],
    }
    base.update(overrides)
    return base


def test_project_local_job_shape_parity():
    row = project_local_job_for_swarm_live(_sample_local_job())
    assert _STORE_LIVE_KEYS.issubset(row.keys())
    assert row["id"] == "local-abc"
    assert row["source"] == "harness"
    assert row["artifacts_complete"] is True
    assert row["routing_savings_basis"] == "estimated"
    assert row["routing_savings_counted"] is True
    assert abs(row["routing_saved_usd"] - 0.04) < 1e-9
    assert row["actions"][0]["action_id"] == "a1"


def test_project_provider_cost_not_estimated():
    row = project_local_job_for_swarm_live(_sample_local_job(
        status="completed",
        est_cost_usd=0.42,
        estimated=False,
        cost_provenance="provider",
        tokens=12_500,
    ))
    assert row["estimated"] is False
    assert row["cost_provenance"] == "provider"
    assert row["tokens"] == 12_500
    assert row["tasks"][0]["instruction"] == ""


def test_project_restart_interrupted_job():
    row = project_local_job_for_swarm_live(_sample_local_job(
        status="cancelled",
        artifacts=[
            {
                "type": "ROUTING",
                "headline": "Routed to cheap-model",
                "policy": "balanced",
            },
            {
                "type": "error",
                "headline": "Interrupted by backend restart",
            },
        ],
        tasks=[{
            "id": "local-abc-w0",
            "role": "implement (agentic)",
            "instruction": "edit files",
            "status": "cancelled",
            "adapter": "agentic",
        }],
    ))
    assert row["status"] == "cancelled"
    headlines = [a["headline"] for a in row["artifacts"]]
    assert "Interrupted by backend restart" in headlines
    assert any(a.get("type") == "ROUTING" for a in row["artifacts"])


def test_project_user_cancelled_job():
    row = project_local_job_for_swarm_live(_sample_local_job(
        status="cancelled",
        artifacts=[{"type": "error", "headline": "Cancelled by user"}],
    ))
    assert row["status"] == "cancelled"
    assert row["artifacts"][0]["headline"] == "Cancelled by user"


def test_merge_skips_duplicate_store_ids():
    store = [{"id": "local-dup", "goal": "store wins", "status": "complete"}]
    local = [_sample_local_job(id="local-dup", goal="local loses")]
    merged = merge_local_jobs_into_swarm_live(store, local)
    assert len(merged) == 1
    assert merged[0]["goal"] == "store wins"


def test_merge_appends_unique_local_rows():
    store = [{"id": "job-store", "goal": "store", "status": "complete"}]
    local = [
        _sample_local_job(id="local-one"),
        _sample_local_job(id="local-two", goal="second"),
    ]
    merged = merge_local_jobs_into_swarm_live(store, local)
    ids = [j["id"] for j in merged]
    assert ids == ["job-store", "local-one", "local-two"]
    assert all(
        j.get("artifacts_complete")
        for j in merged
        if str(j["id"]).startswith("local-")
    )


def test_stable_id_through_projection():
    row = project_local_job_for_swarm_live(_sample_local_job(id="local-stable-99"))
    again = project_local_job_for_swarm_live(_sample_local_job(id="local-stable-99"))
    assert row["id"] == "local-stable-99"
    assert again["id"] == row["id"]


def test_local_jobs_mixin_never_writes_swarm_store(monkeypatch):
    """LocalJobsMixin must not call create_store / add_job / store.write."""
    calls: list[str] = []

    def _track(name):
        def _inner(*args, **kwargs):
            calls.append(name)
            return MagicMock()
        return _inner

    monkeypatch.setattr(
        "puppetmaster.store_factory.create_store",
        _track("create_store"),
    )

    class _Pilot(LocalJobsMixin):
        def __init__(self):
            self._local_jobs = {}
            self._local_jobs_lock = threading.Lock()
            self._local_job_cancels = {}
            self._local_jobs_path = "/tmp/unused-swarm_local_jobs.json"
            self.config = SimpleNamespace(repo="/tmp", driver="stub")
            self.harness_session_id = "sess"

    pilot = _Pilot()
    pilot._register_local_job("local-x", "goal", engine="native", model="stub")
    pilot._finish_local_job("local-x", ok=True, summary="done")
    pilot._persist_local_jobs()
    assert calls == []


def test_restart_reload_heals_then_projects(tmp_path):
    from harness.config import HarnessConfig
    from harness.conversation import ConversationalSession

    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=str(tmp_path))
    first = ConversationalSession(cfg)
    first._register_local_job("local-restart", "long job")
    assert first._local_jobs["local-restart"]["status"] == "running"

    second = ConversationalSession(cfg)
    reloaded = second._local_jobs["local-restart"]
    assert reloaded["status"] == "cancelled"

    row = project_local_job_for_swarm_live(reloaded)
    assert row["status"] == "cancelled"
    assert any(
        a.get("headline") == "Interrupted by backend restart"
        for a in row["artifacts"]
    )


def _server(tmp_state_dir):
    import harness.server as srv

    srv._boot_usage_reset_for_tests()
    srv._session.state_dir = tmp_state_dir
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, port, srv


def _get(port, path, headers=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        headers=headers or {},
        method="GET",
    )
    return urllib.request.urlopen(req, timeout=10)


def test_swarm_live_merge_uses_projected_local_shape(monkeypatch):
    tmp_dir = tempfile.mkdtemp()
    try:
        httpd, port, srv = _server(tmp_dir)
        try:
            headers = {"X-Harness-Token": srv._TOKEN}
            workspace = str(tmp_dir)
            srv._cfg.repo = workspace
            if not srv._sessions.active:
                srv._sessions.create(
                    "mapper test", repo=workspace, workspace_root=workspace,
                )
            srv._sync_pilot_session_id()
            monkeypatch.setattr(
                "harness.local_job_routing.preview_agentic_route",
                lambda goal, role="implement": {
                    "model_id": "cheap",
                    "est_cost_usd": 0.01,
                    "routing_saved_usd": 0.03,
                    "routing_savings_basis": "estimated",
                    "artifact": {
                        "type": "ROUTING",
                        "headline": "Routed to cheap",
                        "policy": "balanced",
                        "est_cost_usd": 0.01,
                    },
                },
            )
            srv._pilot._register_local_job(
                "local-mapper",
                "Build mapper",
                cwd=workspace,
                engine="agentic",
                model="",
            )
            data = json.loads(
                _get(port, "/api/swarm/live", headers=headers).read().decode()
            )

            live = [j for j in data["jobs"] if j.get("id") == "local-mapper"]
            assert len(live) == 1
            job = live[0]
            assert _STORE_LIVE_KEYS.issubset(job.keys())
            assert job["artifacts_complete"] is True
            assert job["routing_savings_basis"] == "estimated"
            assert job["routing_savings_counted"] is True
            assert job["source"] == "harness"
        finally:
            httpd.shutdown()
            srv._pilot._local_jobs.clear()
    finally:
        shutil.rmtree(tmp_dir)


def test_project_preserves_accounting_fields():
    row = project_local_job_for_swarm_live(_sample_local_job(
        accounting_owned=True,
        accounting_scope="marionette",
    ))
    assert row["accounting_owned"] is True
    assert row["accounting_scope"] == "marionette"
    assert row["source"] == "harness"


def test_swarm_live_session_savings_exclude_external_cli_jobs(tmp_path, monkeypatch):
    """Visibility-only CLI rows stay visible but do not inflate session savings."""
    repo = tmp_path / "repo"
    repo.mkdir()
    harness_dir = tmp_path / "harness-state"
    harness_store = create_store("sqlite", str(harness_dir))
    cli_dir = tmp_path / "cli-external"
    cli_store = create_store("sqlite", str(cli_dir))
    cli_job = cli_store.create_job("external swarm")
    cli_store.save_task(Task(
        job_id=cli_job.id,
        role="implement",
        instruction="do work",
        adapter="agentic",
        payload={"cwd": str(repo)},
    ))

    httpd, port, srv = _server(str(harness_dir))
    try:
        monkeypatch.setenv("HARNESS_APP_RUN_ID", "run-test")
        monkeypatch.delenv("HARNESS_CLI_COST_MERGE", raising=False)
        monkeypatch.setattr(srv, "_jobs_snapshot", lambda: [])
        monkeypatch.setattr(srv._session, "state", lambda: SimpleNamespace(
            store=harness_store,
            format_artifacts=lambda arts: [],
            job_artifacts=lambda jid: [],
        ))
        monkeypatch.setattr(
            "harness.cli_job_merge.resolve_cli_state_dir",
            lambda workspace_root="": str(cli_dir),
        )
        monkeypatch.setattr(
            "harness.cli_job_merge.merge_running_cli_jobs_all_projects",
            lambda **kwargs: [],
        )
        monkeypatch.setattr(srv, "_swarm_registry", lambda: [])
        monkeypatch.setattr(srv, "_job_savings_fields", lambda jid: {})
        monkeypatch.setattr(srv._pilot, "live_local_jobs", lambda: [])
        srv._cfg.repo = str(repo)
        srv._sessions._active = "sess-live"
        srv._pilot.harness_session_id = "sess-live"

        scoped = urllib.parse.quote(str(repo), safe="")
        data = json.loads(
            _get(port, f"/api/swarm/live?repo={scoped}", {"X-Harness-Token": srv._TOKEN}).read().decode()
        )
        cli_rows = [j for j in data["jobs"] if j.get("id") == cli_job.id]
        assert len(cli_rows) == 1
        assert cli_rows[0]["accounting_owned"] is False
        assert cli_rows[0]["routing_saved_usd"] == 0.0
        assert data["session"]["routing_saved_usd"] == 0.0
    finally:
        httpd.shutdown()


def test_swarm_live_session_savings_include_local_only_jobs(monkeypatch):
    tmp_dir = tempfile.mkdtemp()
    try:
        httpd, port, srv = _server(tmp_dir)
        try:
            headers = {"X-Harness-Token": srv._TOKEN}
            workspace = str(tmp_dir)
            srv._cfg.repo = workspace
            if not srv._sessions.active:
                srv._sessions.create(
                    "local savings", repo=workspace, workspace_root=workspace,
                )
            srv._sync_pilot_session_id()
            monkeypatch.setattr(
                "harness.local_job_routing.preview_agentic_route",
                lambda goal, role="implement": {
                    "model_id": "cheap",
                    "est_cost_usd": 0.01,
                    "routing_saved_usd": 0.07,
                    "routing_savings_basis": "estimated",
                    "artifact": {
                        "type": "ROUTING",
                        "headline": "Routed to cheap",
                        "policy": "balanced",
                        "est_cost_usd": 0.01,
                    },
                },
            )
            srv._pilot._register_local_job(
                "local-savings",
                "Build savings",
                cwd=workspace,
                engine="agentic",
                model="",
            )
            data = json.loads(
                _get(port, "/api/swarm/live", headers=headers).read().decode()
            )
            assert abs(data["session"]["routing_saved_usd"] - 0.07) < 1e-9
            assert data["session"]["routing_savings_basis"] == "estimated"
        finally:
            httpd.shutdown()
            srv._pilot._local_jobs.clear()
    finally:
        shutil.rmtree(tmp_dir)


def test_session_savings_aggregate_local_only_jobs():
    jobs = merge_local_jobs_into_swarm_live(
        [],
        [_sample_local_job(
            id="local-only",
            routing_saved_usd=0.07,
            routing_savings_basis="estimated",
            routing_savings_counted=True,
            routing_tokens_compared=1000,
        )],
    )
    routing = sum(float(j.get("routing_saved_usd") or 0.0) for j in jobs)
    assert len(jobs) == 1
    assert abs(routing - 0.07) < 1e-9
    assert jobs[0]["routing_savings_counted"] is True


def test_session_savings_mixed_store_and_local_dedupes_ids():
    store = [{
        "id": "local-dup",
        "goal": "store wins",
        "status": "complete",
        "routing_saved_usd": 0.50,
        "routing_savings_basis": "estimated",
        "routing_savings_counted": True,
    }]
    local = [_sample_local_job(
        id="local-dup",
        goal="local loses",
        routing_saved_usd=0.99,
        routing_savings_basis="estimated",
        routing_savings_counted=True,
    )]
    merged = merge_local_jobs_into_swarm_live(store, local)
    routing = sum(float(j.get("routing_saved_usd") or 0.0) for j in merged)
    assert len(merged) == 1
    assert merged[0]["goal"] == "store wins"
    assert abs(routing - 0.50) < 1e-9

    mixed = merge_local_jobs_into_swarm_live(
        [{
            "id": "job-store",
            "routing_saved_usd": 0.40,
            "routing_savings_basis": "estimated",
            "routing_savings_counted": True,
        }],
        [_sample_local_job(
            id="local-extra",
            routing_saved_usd=0.03,
            routing_savings_basis="estimated",
            routing_savings_counted=True,
        )],
    )
    routing_mixed = sum(float(j.get("routing_saved_usd") or 0.0) for j in mixed)
    assert abs(routing_mixed - 0.43) < 1e-9
    assert {j["id"] for j in mixed} == {"job-store", "local-extra"}
