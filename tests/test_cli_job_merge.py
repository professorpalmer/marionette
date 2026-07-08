"""CLI Puppetmaster job merge into harness job views."""
from __future__ import annotations

import json
import threading
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

from harness.cli_job_merge import (
    merge_scoped_cli_jobs,
    open_cli_durable_state,
    reset_merge_diag_for_tests,
)
from harness.job_scoping import stamp_task_payload
from harness.server import _job_swarm_accounting, _scoped_jobs_snapshot
from puppetmaster.models import Artifact, ArtifactType, Task
from puppetmaster.store_factory import create_store


def _save_task(store, job_id: str, cwd: str, session_id: str = "", model: str = ""):
    payload = stamp_task_payload({"cwd": cwd}, session_id=session_id, cwd=cwd)
    if model:
        payload["model"] = model
    task = Task(
        job_id=job_id,
        role="implement",
        instruction="do work",
        adapter="agentic",
        payload=payload,
    )
    store.save_task(task)
    return task


def _verification(job_id: str, task_id: str, model: str, tin: int, tout: int):
    return Artifact(
        job_id=job_id,
        task_id=task_id,
        type=ArtifactType.VERIFICATION,
        created_by="worker",
        payload={
            "model": model,
            "tokens_in": tin,
            "tokens_out": tout,
            "check": "usage",
            "result": "ok",
        },
        confidence=0.9,
        evidence=["usage"],
    )


def _seed_cli_store(tmp_path, repo_root: str, goal: str = "cli goal"):
    cli_dir = tmp_path / "cli-state"
    store = create_store("sqlite", str(cli_dir))
    job = store.create_job(goal)
    _save_task(store, job.id, repo_root, model="worker-model")
    store.save_artifact(_verification(job.id, "t-cli", "worker-model", 50_000, 10_000))
    return store, str(cli_dir), job.id


def test_merge_dedupes_ids_and_sets_source(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    harness_store = create_store("sqlite", str(tmp_path / "harness-state"))
    harness_job = harness_store.create_job("harness goal")
    _save_task(harness_store, harness_job.id, str(repo))

    class _FakeCliStore:
        def list_tasks_for_jobs(self, jids):
            return []

        def list_jobs(self):
            return []

    class _FakeCliState:
        store = _FakeCliStore()

        def list_jobs(self):
            return [
                {"id": harness_job.id, "goal": "cli dup", "status": "complete", "adapter": "agentic"},
                {"id": "cli-only", "goal": "cli only", "status": "complete", "adapter": "agentic"},
            ]

    monkeypatch.setattr(
        "harness.cli_job_merge.open_cli_durable_state",
        lambda workspace_root="": _FakeCliState(),
    )
    monkeypatch.setattr(
        "harness.job_scoping.filter_store_jobs",
        lambda rows, store, **kwargs: rows,
    )

    harness_rows = [
        {"id": harness_job.id, "goal": "harness goal", "status": "complete", "adapter": "agentic"},
    ]
    merged, _ = merge_scoped_cli_jobs(
        harness_rows,
        harness_store=harness_store,
        active_session_id="sess-x",
        repo_root=str(repo),
        workspace_root=str(repo),
    )
    by_id = {row["id"]: row for row in merged}
    assert by_id[harness_job.id]["source"] == "harness"
    assert by_id["cli-only"]["source"] == "cli"
    assert len(merged) == 2


def test_unreadable_cli_store_contributes_nothing(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    harness_store = create_store("sqlite", str(tmp_path / "harness-state"))
    harness_job = harness_store.create_job("harness goal")
    _save_task(harness_store, harness_job.id, str(repo))

    reset_merge_diag_for_tests()

    monkeypatch.setattr("harness.cli_job_merge.open_cli_durable_state", lambda workspace_root="": None)

    harness_rows = [
        {"id": harness_job.id, "goal": "harness goal", "status": "complete", "adapter": "agentic"},
    ]
    merged, cli_store = merge_scoped_cli_jobs(
        harness_rows,
        harness_store=harness_store,
        active_session_id="sess-x",
        repo_root=str(repo),
        workspace_root=str(repo),
    )
    assert cli_store is None
    assert len(merged) == 1
    assert merged[0]["source"] == "harness"


def test_missing_cli_store_is_silent(monkeypatch):
    monkeypatch.setattr(
        "harness.cli_job_merge.resolve_cli_state_dir",
        lambda workspace_root="": None,
    )
    assert open_cli_durable_state("/no/such/workspace") is None


def test_accounting_fields_from_cli_fixture_store(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cli_store, _cli_dir, cli_job_id = _seed_cli_store(tmp_path, str(repo))

    registry = [
        SimpleNamespace(
            id="worker-model",
            adapter_model_name="worker-model",
            input_per_mtok_usd=1.0,
            output_per_mtok_usd=2.0,
            billing="metered",
            marginal_cost_usd=lambda tin, tout: (tin / 1_000_000.0) + (tout / 1_000_000.0) * 2.0,
            estimate_cost_usd=lambda tin, tout: (tin / 1_000_000.0) + (tout / 1_000_000.0) * 2.0,
        )
    ]
    raw_arts = cli_store.list_artifacts(cli_job_id)
    tokens, cost = _job_swarm_accounting(raw_arts, registry)
    assert tokens == 60_000
    assert abs(cost - 0.07) < 1e-6


def _api_server(tmp_state_dir):
    import harness.server as srv

    srv._session.state_dir = tmp_state_dir
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, port, srv


def _api_get(port, path, token):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        headers={"X-Harness-Token": token},
        method="GET",
    )
    return urllib.request.urlopen(req, timeout=10)


def test_api_swarm_live_includes_cli_jobs_with_accounting(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    harness_dir = tmp_path / "harness-state"
    harness_store = create_store("sqlite", str(harness_dir))
    _cli_store, cli_dir, cli_job_id = _seed_cli_store(tmp_path, str(repo))

    httpd, port, srv = _api_server(str(harness_dir))
    try:
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
        monkeypatch.setattr(srv, "_swarm_registry", lambda: [
            SimpleNamespace(
                id="worker-model",
                adapter_model_name="worker-model",
                input_per_mtok_usd=1.0,
                output_per_mtok_usd=2.0,
                billing="metered",
                marginal_cost_usd=lambda tin, tout: (tin / 1_000_000.0) + (tout / 1_000_000.0) * 2.0,
                estimate_cost_usd=lambda tin, tout: (tin / 1_000_000.0) + (tout / 1_000_000.0) * 2.0,
            )
        ])
        monkeypatch.setattr(srv, "_job_savings_fields", lambda jid: {})
        monkeypatch.setattr(srv._pilot, "live_local_jobs", lambda: [])
        srv._cfg.repo = str(repo)

        scoped = urllib.parse.quote(str(repo), safe="")
        data = json.loads(
            _api_get(port, f"/api/swarm/live?repo={scoped}", srv._TOKEN).read().decode()
        )
        cli_rows = [j for j in data["jobs"] if j.get("id") == cli_job_id]
        assert len(cli_rows) == 1
        row = cli_rows[0]
        assert row["source"] == "cli"
        assert row["tokens"] == 60_000
        assert abs(row["est_cost_usd"] - 0.07) < 1e-6
        assert row["model"] == "worker-model"
    finally:
        httpd.shutdown()


def test_scoped_jobs_snapshot_merges_cli_source(tmp_path, monkeypatch):
    import harness.server as srv

    repo = tmp_path / "repo"
    repo.mkdir()
    harness_dir = tmp_path / "harness-state"
    harness_store = create_store("sqlite", str(harness_dir))
    _cli_store, cli_dir, cli_job_id = _seed_cli_store(tmp_path, str(repo))

    monkeypatch.setattr(srv, "_jobs_snapshot", lambda: [])
    monkeypatch.setattr(srv._session, "state", lambda: SimpleNamespace(store=harness_store))
    monkeypatch.setattr(
        "harness.cli_job_merge.resolve_cli_state_dir",
        lambda workspace_root="": str(cli_dir),
    )
    srv._cfg.repo = str(repo)
    monkeypatch.setattr(srv._sessions, "_active", "")
    srv._pilot.harness_session_id = ""

    rows = _scoped_jobs_snapshot(repo_root=str(repo))
    assert len(rows) == 1
    assert rows[0]["id"] == cli_job_id
    assert rows[0]["source"] == "cli"
