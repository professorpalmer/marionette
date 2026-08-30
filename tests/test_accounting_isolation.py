"""Accounting isolation: visibility vs Marionette-owned economic attribution."""
from __future__ import annotations

import json
import threading
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

from harness.job_scoping import (
    ACCOUNTING_SCOPE_VISIBILITY,
    annotate_job_accounting,
    apply_job_economics_policy,
    cli_cost_merge_enabled,
    job_label_for_session,
    parse_job_origin,
    resolve_job_accounting,
    stamp_task_payload,
    zero_job_economics,
)
from harness.server import _job_swarm_accounting
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


def test_job_label_includes_marionette_provenance(monkeypatch):
    monkeypatch.setenv("HARNESS_APP_RUN_ID", "run-abc")
    label = job_label_for_session("sess-a")
    data = json.loads(label)
    assert data["session_id"] == "sess-a"
    assert data["origin"] == "marionette"
    assert data["app_run_id"] == "run-abc"
    assert parse_job_origin(label, []) == "marionette"


def test_cli_job_fail_closed_by_default():
    acct = resolve_job_accounting(
        job={"id": "cli-1", "source": "cli"},
        active_session_id="sess-a",
    )
    assert acct["accounting_owned"] is False
    assert acct["accounting_scope"] == ACCOUNTING_SCOPE_VISIBILITY


def test_cli_job_never_owned_via_cwd_only_legacy():
    acct = resolve_job_accounting(
        job={"id": "cli-1", "source": "cli", "cwd": "/work/repo"},
        active_session_id="sess-a",
        cli_cost_merge=True,
    )
    assert acct["accounting_owned"] is False


def test_cli_job_owned_only_with_merge_and_marionette_stamp(monkeypatch):
    monkeypatch.setenv("HARNESS_APP_RUN_ID", "run-abc")
    monkeypatch.setenv("HARNESS_CLI_COST_MERGE", "1")
    assert cli_cost_merge_enabled()
    label = job_label_for_session("sess-a")
    acct = resolve_job_accounting(
        job={"id": "cli-1", "source": "cli", "label": label},
        active_session_id="sess-a",
    )
    assert acct["accounting_owned"] is True


def test_cli_job_not_owned_when_current_app_run_id_missing(monkeypatch):
    monkeypatch.delenv("HARNESS_APP_RUN_ID", raising=False)
    monkeypatch.setenv("HARNESS_CLI_COST_MERGE", "1")
    label = job_label_for_session("sess-a", app_run_id="run-stamped")
    acct = resolve_job_accounting(
        job={"id": "cli-1", "source": "cli", "label": label},
        active_session_id="sess-a",
        app_run_id="",
        cli_cost_merge=True,
    )
    assert acct["accounting_owned"] is False


def test_cli_job_not_owned_when_stamped_app_run_id_missing(monkeypatch):
    monkeypatch.setenv("HARNESS_APP_RUN_ID", "run-current")
    monkeypatch.setenv("HARNESS_CLI_COST_MERGE", "1")
    label = json.dumps({"session_id": "sess-a", "origin": "marionette"})
    acct = resolve_job_accounting(
        job={"id": "cli-1", "source": "cli", "label": label},
        active_session_id="sess-a",
        cli_cost_merge=True,
    )
    assert acct["accounting_owned"] is False


def test_cli_job_not_owned_when_app_run_id_mismatch(monkeypatch):
    monkeypatch.setenv("HARNESS_APP_RUN_ID", "run-current")
    monkeypatch.setenv("HARNESS_CLI_COST_MERGE", "1")
    label = job_label_for_session("sess-a", app_run_id="run-stale")
    acct = resolve_job_accounting(
        job={"id": "cli-1", "source": "cli", "label": label},
        active_session_id="sess-a",
        cli_cost_merge=True,
    )
    assert acct["accounting_owned"] is False


def test_cli_job_owned_via_task_payload_only(monkeypatch):
    monkeypatch.setenv("HARNESS_APP_RUN_ID", "run-abc")
    monkeypatch.setenv("HARNESS_CLI_COST_MERGE", "1")
    task = Task(
        job_id="cli-task-only",
        role="implement",
        instruction="do work",
        adapter="agentic",
        payload=stamp_task_payload(
            {"cwd": "/work/repo"},
            session_id="sess-a",
            origin="marionette",
            app_run_id="run-abc",
        ),
    )
    acct = resolve_job_accounting(
        job={"id": "cli-task-only", "source": "cli"},
        tasks=[task],
        active_session_id="sess-a",
        cli_cost_merge=True,
    )
    assert acct["accounting_owned"] is True


def test_harness_session_stamp_owned_for_active_session():
    label = job_label_for_session("sess-a")
    acct = resolve_job_accounting(
        job={"id": "h-1", "source": "harness", "label": label},
        active_session_id="sess-a",
    )
    assert acct["accounting_owned"] is True


def test_legacy_unstamped_harness_job_not_owned():
    acct = resolve_job_accounting(
        job={"id": "legacy-1", "source": "harness", "cwd": "/work/repo"},
        active_session_id="sess-a",
    )
    assert acct["accounting_owned"] is False


def test_registered_job_id_owned_without_session_stamp():
    acct = resolve_job_accounting(
        job={"id": "orphan-1", "source": "harness"},
        active_session_id="sess-a",
        registered_job_ids={"orphan-1"},
    )
    assert acct["accounting_owned"] is True


def test_zero_job_economics_strips_meters():
    row = zero_job_economics({
        "tokens": 50_000,
        "est_cost_usd": 0.07,
        "routing_saved_usd": 0.04,
        "cache_saved_usd": 0.01,
        "tool_output_savings_usd": 0.002,
    })
    assert row["tokens"] == 0
    assert row["est_cost_usd"] == 0
    assert row["routing_saved_usd"] == 0
    assert row["cache_saved_usd"] == 0
    assert row["tool_output_savings_usd"] == 0


def _seed_cli_store(tmp_path, repo_root: str):
    cli_dir = tmp_path / "cli-state"
    store = create_store("sqlite", str(cli_dir))
    job = store.create_job("cli goal")
    _save_task(store, job.id, repo_root, model="worker-model")
    store.save_artifact(_verification(job.id, "t-cli", "worker-model", 50_000, 10_000))
    return store, str(cli_dir), job.id


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


def test_api_swarm_live_cli_job_absent_not_just_zeroed(tmp_path, monkeypatch):
    """Unstamped CLI jobs must not appear on /api/swarm/live."""
    repo = tmp_path / "repo"
    repo.mkdir()
    harness_dir = tmp_path / "harness-state"
    harness_store = create_store("sqlite", str(harness_dir))
    _cli_store, cli_dir, cli_job_id = _seed_cli_store(tmp_path, str(repo))

    httpd, port, srv = _api_server(str(harness_dir))
    saved_driver = srv._cfg.driver
    saved_repo = srv._cfg.repo
    try:
        monkeypatch.setenv("HARNESS_APP_RUN_ID", "epoch-test")
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
        monkeypatch.setattr(srv._pilot, "_session_job_ids", [], raising=False)
        monkeypatch.setattr(srv._pilot, "_tokens_used", 12_000, raising=False)
        monkeypatch.setattr(srv._pilot, "_tokens_cached", 4_000, raising=False)
        monkeypatch.setattr(srv._pilot, "_worker_tokens_in", 0, raising=False)
        monkeypatch.setattr(srv._pilot, "_worker_tokens_out", 0, raising=False)
        monkeypatch.setattr(srv._pilot, "_tokens_in", 8_000, raising=False)
        monkeypatch.setattr(srv._pilot, "_tokens_out", 4_000, raising=False)
        srv._cfg.repo = str(repo)
        srv._cfg.driver = "openai-codex/test"
        srv._sessions._active = "sess-live"

        data = json.loads(
            _api_get(port, "/api/swarm/live", srv._TOKEN).read().decode()
        )
        cli_rows = [j for j in data["jobs"] if j.get("id") == cli_job_id]
        assert cli_rows == []
        # Pilot/session Codex meters must survive — not replaced by CLI artifacts.
        assert data["session"]["tokens_cached"] >= 4_000
        assert data["session"]["tokens_used"] >= 12_000
    finally:
        srv._cfg.driver = saved_driver
        srv._cfg.repo = saved_repo
        httpd.shutdown()


def test_accounting_fields_from_cli_fixture_store(tmp_path):
    """Raw artifact accounting still prices CLI store rows (low-level helper)."""
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

    tagged = annotate_job_accounting(
        {"id": cli_job_id, "source": "cli"},
        active_session_id="sess-a",
    )
    stripped = apply_job_economics_policy({**tagged, "tokens": tokens, "est_cost_usd": cost})
    assert stripped["tokens"] == 0
    assert stripped["est_cost_usd"] == 0
