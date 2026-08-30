"""Status-bar /api/usage must bill CLI-store swarm jobs and surface savings.

Covers the three root causes behind SwarmPane showing ~$0.70 while StatusBar
billed $0:

* RC1 store asymmetry -- /api/usage must price the same merged workspace-
  scoped set as /api/swarm/live (harness + CLI stores).
* RC2 stamp gap -- session_total must include task-payload-stamped jobs and
  workspace-visible unstamped jobs (not label-only stamps).
* Savings -- routing_saved_usd / cache_saved_usd_swarm fold into the existing
  savings surface without double-billing persisted meters.
"""
from __future__ import annotations

import json
import threading
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import harness.server as server
from harness.job_scoping import job_label_for_session, stamp_task_payload
from harness.server import (
    _cache_saved_usd_swarm,
    _routing_saved_usd,
    _tokens_cached_swarm,
)
from harness.api.swarm_cost import _cache_saved_usd_swarm_detail
from puppetmaster.models import Artifact, ArtifactType, Task
from puppetmaster.store_factory import create_store


def _registry_spec(
    spec_id: str,
    *,
    input_per_mtok_usd: float = 1.0,
    output_per_mtok_usd: float = 2.0,
):
    return SimpleNamespace(
        id=spec_id,
        adapter_model_name=spec_id,
        input_per_mtok_usd=input_per_mtok_usd,
        output_per_mtok_usd=output_per_mtok_usd,
        billing="metered",
        marginal_cost_usd=lambda tin, tout: (
            (tin / 1_000_000.0) * input_per_mtok_usd
            + (tout / 1_000_000.0) * output_per_mtok_usd
        ),
        estimate_cost_usd=lambda tin, tout: (
            (tin / 1_000_000.0) * input_per_mtok_usd
            + (tout / 1_000_000.0) * output_per_mtok_usd
        ),
    )


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


def _verification(
    job_id: str,
    task_id: str,
    model: str,
    tin: int,
    tout: int,
    *,
    tokens_cached: int = 0,
    real_cost_usd: float = 0.0,
):
    payload = {
        "model": model,
        "tokens_in": tin,
        "tokens_out": tout,
        "check": "usage",
        "result": "ok",
    }
    if tokens_cached:
        payload["tokens_cached"] = tokens_cached
    if real_cost_usd:
        payload["real_cost_usd"] = real_cost_usd
    return Artifact(
        job_id=job_id,
        task_id=task_id,
        type=ArtifactType.VERIFICATION,
        created_by="worker",
        payload=payload,
        confidence=0.9,
        evidence=["usage"],
    )


def _routing(
    job_id: str,
    task_id: str,
    *,
    policy: str,
    baseline: float,
    estimated: float,
    model_id: str = "cheap-model",
    baseline_model_id: str = "",
):
    payload = {
        "model_id": model_id,
        "adapter": "agentic",
        "policy": policy,
        "baseline_cost_usd": baseline,
        "estimated_cost_usd": estimated,
    }
    if baseline_model_id:
        payload["baseline_model_id"] = baseline_model_id
    return Artifact(
        job_id=job_id,
        task_id=task_id,
        type=ArtifactType.ROUTING,
        created_by="router",
        payload=payload,
        confidence=1.0,
        evidence=["route"],
    )


def _seed_cli_store(tmp_path, repo_root: str, *, session_id: str = ""):
    cli_dir = tmp_path / "cli-state"
    store = create_store("sqlite", str(cli_dir))
    job = store.create_job("cli goal")
    _save_task(store, job.id, repo_root, session_id=session_id, model="worker-model")
    store.save_artifact(_verification(job.id, "t-cli", "worker-model", 50_000, 10_000))
    return store, str(cli_dir), job.id


def _api_server(tmp_state_dir):
    server._boot_usage_reset_for_tests()
    server._session.state_dir = tmp_state_dir
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, port


def _api_get(port, path, token):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        headers={"X-Harness-Token": token},
        method="GET",
    )
    return urllib.request.urlopen(req, timeout=10)


def test_routing_saved_usd_balanced_vs_quality():
    """balanced baseline 0.50 / estimated 0.10 -> 0.40; quality contributes 0."""
    arts = [
        _routing("j1", "t1", policy="balanced", baseline=0.50, estimated=0.10),
        _routing("j1", "t2", policy="quality", baseline=0.50, estimated=0.10),
    ]
    assert abs(_routing_saved_usd(arts) - 0.40) < 1e-9


def test_routing_saved_usd_cheap_policy_counts():
    arts = [
        _routing("j1", "t1", policy="cheap", baseline=1.0, estimated=0.25),
    ]
    assert abs(_routing_saved_usd(arts) - 0.75) < 1e-9


def test_routing_saved_usd_zero_baseline_skipped():
    arts = [
        _routing("j1", "t1", policy="balanced", baseline=0.0, estimated=0.10),
    ]
    assert _routing_saved_usd(arts) == 0.0


def test_cache_saved_usd_swarm_credits_real_cost_tasks():
    """real_cost_usd is spend; it must not suppress cache-savings display."""
    registry = [_registry_spec("worker-model", input_per_mtok_usd=3.0)]
    # 100k + 40k cached @ $3/MTok * 0.9 = 0.378 (both tasks contribute).
    arts = [
        _verification(
            "j1", "t1", "worker-model", 200_000, 10_000, tokens_cached=100_000
        ),
        _verification(
            "j1",
            "t2",
            "worker-model",
            50_000,
            5_000,
            tokens_cached=40_000,
            real_cost_usd=0.12,
        ),
    ]
    assert abs(_cache_saved_usd_swarm(arts, registry) - 0.378) < 1e-9


def test_api_usage_excludes_unowned_cli_store_jobs(tmp_path, monkeypatch):
    """Unstamped CLI-store jobs must not bill Marionette /api/usage."""
    repo = tmp_path / "repo"
    repo.mkdir()
    harness_dir = tmp_path / "harness-state"
    harness_store = create_store("sqlite", str(harness_dir))
    _cli_store, cli_dir, cli_job_id = _seed_cli_store(tmp_path, str(repo))

    httpd, port = _api_server(str(harness_dir))
    try:
        monkeypatch.delenv("HARNESS_CLI_COST_MERGE", raising=False)
        monkeypatch.setattr(server, "_jobs_snapshot", lambda: [])
        monkeypatch.setattr(
            server._session,
            "state",
            lambda: SimpleNamespace(store=harness_store),
        )
        monkeypatch.setattr(
            "harness.cli_job_merge.resolve_cli_state_dir",
            lambda workspace_root="": str(cli_dir),
        )
        monkeypatch.setattr(
            server,
            "_swarm_registry",
            lambda: [_registry_spec("worker-model")],
        )
        monkeypatch.setattr(server, "_job_savings_fields", lambda jid: {})
        monkeypatch.setattr(server, "_job_in_cost_window", lambda created_at: True)
        server._cfg.repo = str(repo)

        scoped = urllib.parse.quote(str(repo), safe="")
        usage = json.loads(
            _api_get(port, f"/api/usage?repo={scoped}", server._TOKEN).read().decode()
        )
        job_rows = [j for j in usage["jobs"] if j.get("job_id") == cli_job_id]
        assert len(job_rows) == 0
        assert usage["session"]["est_cost_usd"] == 0.0
    finally:
        httpd.shutdown()


def test_session_total_includes_task_stamp_not_unstamped_legacy(
    tmp_path, monkeypatch
):
    """Task-payload stamp counts; cwd-only legacy unstamped jobs do not bill."""
    from harness.sessions import SessionStore

    repo = tmp_path / "repo"
    repo.mkdir()
    harness_dir = tmp_path / "harness-state"
    harness_store = create_store("sqlite", str(harness_dir))

    sess_store = SessionStore(str(tmp_path / "harness_sessions.json"))
    row = sess_store.create(title="stamp test", repo=str(repo), workspace_root=str(repo))
    sid = row["id"]
    monkeypatch.setattr(server, "_sessions", sess_store)

    # Label-less job stamped only via task payload.
    stamped = harness_store.create_job("stamped goal")
    _save_task(harness_store, stamped.id, str(repo), session_id=sid, model="worker-model")
    harness_store.save_artifact(
        _verification(stamped.id, "t-stamp", "worker-model", 10_000, 2_000)
    )

    # Unstamped job whose cwd lies under the workspace (tracker-visible).
    unstamped = harness_store.create_job("unstamped goal")
    _save_task(harness_store, unstamped.id, str(repo), model="worker-model")
    harness_store.save_artifact(
        _verification(unstamped.id, "t-un", "worker-model", 20_000, 4_000)
    )

    httpd, port = _api_server(str(harness_dir))
    try:
        monkeypatch.setattr(
            server,
            "_jobs_snapshot",
            lambda: [
                {
                    "id": stamped.id,
                    "goal": "stamped goal",
                    "status": "complete",
                    "adapter": "agentic",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
                {
                    "id": unstamped.id,
                    "goal": "unstamped goal",
                    "status": "complete",
                    "adapter": "agentic",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
            ],
        )
        monkeypatch.setattr(
            server._session,
            "state",
            lambda: SimpleNamespace(store=harness_store),
        )
        monkeypatch.setattr(
            "harness.cli_job_merge.resolve_cli_state_dir",
            lambda workspace_root="": None,
        )
        monkeypatch.setattr(
            server,
            "_swarm_registry",
            lambda: [_registry_spec("worker-model")],
        )
        monkeypatch.setattr(server, "_job_savings_fields", lambda jid: {})
        monkeypatch.setattr(server, "_job_in_cost_window", lambda created_at: True)
        server._cfg.repo = str(repo)

        scoped = urllib.parse.quote(str(repo), safe="")
        usage = json.loads(
            _api_get(port, f"/api/usage?repo={scoped}", server._TOKEN).read().decode()
        )
        total = usage["session_total"]
        assert total is not None
        # Stamped only: 10k*1 + 2k*2 = 0.014 — unstamped legacy cwd match excluded.
        assert abs(total["est_cost_usd"] - 0.014) < 1e-6
    finally:
        httpd.shutdown()


def test_duplicate_job_id_across_stores_counted_once(tmp_path, monkeypatch):
    """Same job id in harness + CLI stores must not double-bill session_total."""
    from harness.sessions import SessionStore

    repo = tmp_path / "repo"
    repo.mkdir()
    harness_dir = tmp_path / "harness-state"
    harness_store = create_store("sqlite", str(harness_dir))

    sess_store = SessionStore(str(tmp_path / "harness_sessions.json"))
    row = sess_store.create(title="dedupe", repo=str(repo), workspace_root=str(repo))
    sid = row["id"]
    monkeypatch.setattr(server, "_sessions", sess_store)

    shared = harness_store.create_job("shared goal", label=job_label_for_session(sid))
    _save_task(harness_store, shared.id, str(repo), session_id=sid, model="worker-model")
    harness_store.save_artifact(
        _verification(shared.id, "t-h", "worker-model", 50_000, 10_000)
    )

    # CLI store with the SAME job id (merge must keep harness, drop CLI dup).
    cli_dir = tmp_path / "cli-state"
    cli_store = create_store("sqlite", str(cli_dir))
    # Can't create_job with a fixed id easily -- seed via merge fake instead.
    class _FakeCliState:
        store = cli_store

        def list_jobs(self):
            return [
                {
                    "id": shared.id,
                    "goal": "cli dup",
                    "status": "complete",
                    "adapter": "agentic",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            ]

    httpd, port = _api_server(str(harness_dir))
    try:
        monkeypatch.setattr(
            server,
            "_jobs_snapshot",
            lambda: [
                {
                    "id": shared.id,
                    "goal": "shared goal",
                    "status": "complete",
                    "adapter": "agentic",
                    "label": job_label_for_session(sid),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            ],
        )
        monkeypatch.setattr(
            server._session,
            "state",
            lambda: SimpleNamespace(store=harness_store),
        )
        monkeypatch.setattr(
            "harness.cli_job_merge.open_cli_durable_state",
            lambda workspace_root="": _FakeCliState(),
        )
        monkeypatch.setattr(
            "harness.job_scoping.filter_store_jobs",
            lambda rows, store, **kwargs: rows,
        )
        monkeypatch.setattr(
            server,
            "_swarm_registry",
            lambda: [_registry_spec("worker-model")],
        )
        monkeypatch.setattr(server, "_job_savings_fields", lambda jid: {})
        monkeypatch.setattr(server, "_job_in_cost_window", lambda created_at: True)
        # Price once at a known figure so double-count is obvious.
        monkeypatch.setattr(
            server, "_job_swarm_accounting", lambda arts, registry: (60_000, 0.70)
        )
        server._cfg.repo = str(repo)

        scoped = urllib.parse.quote(str(repo), safe="")
        usage = json.loads(
            _api_get(port, f"/api/usage?repo={scoped}", server._TOKEN).read().decode()
        )
        assert abs(usage["session"]["est_cost_usd"] - 0.70) < 1e-6
        assert abs(usage["session_total"]["est_cost_usd"] - 0.70) < 1e-6
        assert len(usage["jobs"]) == 1
    finally:
        httpd.shutdown()


def test_api_usage_routing_saved_usd_in_response(tmp_path, monkeypatch):
    """routing_saved_usd: balanced 0.50-0.10=0.40; quality contributes 0."""
    from harness.sessions import SessionStore

    repo = tmp_path / "repo"
    repo.mkdir()
    harness_dir = tmp_path / "harness-state"
    harness_store = create_store("sqlite", str(harness_dir))

    sess_store = SessionStore(str(tmp_path / "harness_sessions.json"))
    row = sess_store.create(title="routing", repo=str(repo), workspace_root=str(repo))
    sid = row["id"]
    monkeypatch.setattr(server, "_sessions", sess_store)

    job = harness_store.create_job("route goal", label=job_label_for_session(sid))
    _save_task(harness_store, job.id, str(repo), session_id=sid, model="cheap-model")
    harness_store.save_artifact(
        _routing(job.id, "t1", policy="balanced", baseline=0.50, estimated=0.10)
    )
    harness_store.save_artifact(
        _routing(job.id, "t2", policy="quality", baseline=0.50, estimated=0.10)
    )
    # Usage on the quality task so the job prices > 0 without forcing the
    # balanced route onto the actual-usage path (needs baseline_model_id).
    harness_store.save_artifact(
        _verification(job.id, "t2", "cheap-model", 1_000, 500)
    )

    httpd, port = _api_server(str(harness_dir))
    try:
        monkeypatch.setattr(
            server,
            "_jobs_snapshot",
            lambda: [
                {
                    "id": job.id,
                    "goal": "route goal",
                    "status": "complete",
                    "adapter": "agentic",
                    "label": job_label_for_session(sid),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            ],
        )
        monkeypatch.setattr(
            server._session,
            "state",
            lambda: SimpleNamespace(store=harness_store),
        )
        monkeypatch.setattr(
            "harness.cli_job_merge.resolve_cli_state_dir",
            lambda workspace_root="": None,
        )
        monkeypatch.setattr(
            server,
            "_swarm_registry",
            lambda: [_registry_spec("cheap-model")],
        )
        monkeypatch.setattr(server, "_job_savings_fields", lambda jid: {})
        monkeypatch.setattr(server, "_job_in_cost_window", lambda created_at: True)
        server._cfg.repo = str(repo)

        scoped = urllib.parse.quote(str(repo), safe="")
        usage = json.loads(
            _api_get(port, f"/api/usage?repo={scoped}", server._TOKEN).read().decode()
        )
        assert abs(usage["session"]["routing_saved_usd"] - 0.40) < 1e-9
    finally:
        httpd.shutdown()


def test_tokens_cached_swarm_dedupes_per_task():
    arts = [
        _verification("j1", "t1", "worker-model", 200_000, 10_000, tokens_cached=100_000),
        _verification("j1", "t1", "worker-model", 200_000, 10_000, tokens_cached=100_000),
        _verification("j1", "t2", "worker-model", 50_000, 5_000, tokens_cached=40_000),
    ]
    assert _tokens_cached_swarm(arts) == 140_000


def test_api_swarm_live_job_rows_carry_routing_and_cache_savings(tmp_path, monkeypatch):
    """Mid-run /api/swarm/live job cards need per-job savings, not just spend."""
    from harness.sessions import SessionStore

    repo = tmp_path / "repo"
    repo.mkdir()
    harness_dir = tmp_path / "harness-state"
    harness_store = create_store("sqlite", str(harness_dir))

    sess_store = SessionStore(str(tmp_path / "harness_sessions.json"))
    row = sess_store.create(title="live-savings", repo=str(repo), workspace_root=str(repo))
    sid = row["id"]
    monkeypatch.setattr(server, "_sessions", sess_store)

    job = harness_store.create_job("live savings", label=job_label_for_session(sid))
    _save_task(harness_store, job.id, str(repo), session_id=sid, model="worker-model")
    harness_store.save_artifact(
        _routing(
            job.id,
            "t1",
            policy="balanced",
            baseline=0.50,
            estimated=0.10,
            model_id="worker-model",
            baseline_model_id="expensive-model",
        )
    )
    harness_store.save_artifact(
        _verification(
            job.id, "t1", "worker-model", 200_000, 10_000, tokens_cached=100_000
        )
    )

    httpd, port = _api_server(str(harness_dir))
    try:
        monkeypatch.setattr(
            server,
            "_jobs_snapshot",
            lambda: [
                {
                    "id": job.id,
                    "goal": "live savings",
                    "status": "running",
                    "adapter": "agentic",
                    "label": job_label_for_session(sid),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            ],
        )
        monkeypatch.setattr(
            server._session,
            "state",
            lambda: SimpleNamespace(store=harness_store, format_artifacts=lambda arts: []),
        )
        monkeypatch.setattr(
            "harness.cli_job_merge.resolve_cli_state_dir",
            lambda workspace_root="": None,
        )
        monkeypatch.setattr(
            server,
            "_swarm_registry",
            lambda: [
                _registry_spec("worker-model", input_per_mtok_usd=3.0),
                _registry_spec(
                    "expensive-model",
                    input_per_mtok_usd=10.0,
                    output_per_mtok_usd=30.0,
                ),
            ],
        )
        monkeypatch.setattr(
            server,
            "_job_savings_fields",
            lambda jid: {
                "tool_output_tokens_saved": 1200,
                "tool_output_savings_usd": 0.0036,
                "tool_output_compactions": 1,
            },
        )
        monkeypatch.setattr(server, "_job_in_cost_window", lambda created_at: True)
        server._cfg.repo = str(repo)

        scoped = urllib.parse.quote(str(repo), safe="")
        live = json.loads(
            _api_get(port, f"/api/swarm/live?repo={scoped}", server._TOKEN).read().decode()
        )
        assert len(live["jobs"]) == 1
        row = live["jobs"][0]
        # Actual-usage counterfactual: 200k/10k with 100k cached @
        # expensive 10/30 vs worker 3/2 → $1.05 list-price value.
        assert abs(row["routing_saved_usd"] - 1.05) < 1e-9
        assert row["routing_savings_basis"] == "actual_usage"
        assert row["routing_tokens_compared"] == 210_000
        # 100k cached @ $3/MTok * 0.9 = 0.27
        assert abs(row["cache_saved_usd"] - 0.27) < 1e-9
        assert row["tokens_cached"] == 100_000
        assert row["tool_output_tokens_saved"] == 1200
        assert abs(row["tool_output_savings_usd"] - 0.0036) < 1e-9
        assert abs(live["session"]["routing_saved_usd"] - 1.05) < 1e-9
        assert live["session"]["routing_savings_basis"] == "actual_usage"
        assert live["session"]["routing_tokens_compared"] == 210_000
        assert abs(live["session"]["cache_saved_usd_swarm"] - 0.27) < 1e-9
    finally:
        httpd.shutdown()


def test_api_swarm_live_tasks_carry_per_task_tokens_and_cost(tmp_path, monkeypatch):
    """Worker rows on /api/swarm/live must include per-task tokens/cost from usage."""
    from harness.sessions import SessionStore

    repo = tmp_path / "repo"
    repo.mkdir()
    harness_dir = tmp_path / "harness-state"
    harness_store = create_store("sqlite", str(harness_dir))

    sess_store = SessionStore(str(tmp_path / "harness_sessions.json"))
    row = sess_store.create(title="task-meters", repo=str(repo), workspace_root=str(repo))
    sid = row["id"]
    monkeypatch.setattr(server, "_sessions", sess_store)

    job = harness_store.create_job("task meters", label=job_label_for_session(sid))
    payload = stamp_task_payload({"cwd": str(repo)}, session_id=sid, cwd=str(repo))
    payload["model"] = "worker-model"
    task = Task(
        id="task-worker-1",
        job_id=job.id,
        role="implement",
        instruction="do work",
        adapter="agentic",
        payload=payload,
    )
    harness_store.save_task(task)
    harness_store.save_artifact(
        _routing(
            job.id,
            "task-worker-1",
            policy="balanced",
            baseline=0.50,
            estimated=0.05,
            model_id="worker-model",
        )
    )
    harness_store.save_artifact(
        _verification(job.id, "task-worker-1", "worker-model", 100_000, 20_000)
    )

    httpd, port = _api_server(str(harness_dir))
    try:
        monkeypatch.setattr(
            server,
            "_jobs_snapshot",
            lambda: [
                {
                    "id": job.id,
                    "goal": "task meters",
                    "status": "running",
                    "adapter": "agentic",
                    "label": job_label_for_session(sid),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            ],
        )
        monkeypatch.setattr(
            server._session,
            "state",
            lambda: SimpleNamespace(store=harness_store, format_artifacts=lambda arts: []),
        )
        monkeypatch.setattr(
            "harness.cli_job_merge.resolve_cli_state_dir",
            lambda workspace_root="": None,
        )
        monkeypatch.setattr(
            server,
            "_swarm_registry",
            lambda: [_registry_spec("worker-model")],
        )
        monkeypatch.setattr(server, "_job_savings_fields", lambda jid: {})
        monkeypatch.setattr(server, "_job_in_cost_window", lambda created_at: True)
        server._cfg.repo = str(repo)

        scoped = urllib.parse.quote(str(repo), safe="")
        live = json.loads(
            _api_get(port, f"/api/swarm/live?repo={scoped}", server._TOKEN).read().decode()
        )
        assert len(live["jobs"]) == 1
        row = live["jobs"][0]
        assert row["tokens"] == 120_000
        assert abs(row["est_cost_usd"] - 0.14) < 1e-6
        assert len(row["tasks"]) == 1
        worker = row["tasks"][0]
        assert worker["id"] == "task-worker-1"
        assert worker["tokens"] == 120_000
        assert abs(worker["est_cost_usd"] - 0.14) < 1e-6
    finally:
        httpd.shutdown()


def test_api_usage_tokens_used_is_pilot_only_plus_job_tokens(tmp_path, monkeypatch):
    """Boot pill tokens_used must match pilot-only + store job tokens (not undercount)."""
    from harness.sessions import SessionStore

    repo = tmp_path / "repo"
    repo.mkdir()
    harness_dir = tmp_path / "harness-state"
    harness_store = create_store("sqlite", str(harness_dir))

    sess_store = SessionStore(str(tmp_path / "harness_sessions.json"))
    row = sess_store.create(title="token parity", repo=str(repo), workspace_root=str(repo))
    sid = row["id"]
    monkeypatch.setattr(server, "_sessions", sess_store)

    job = harness_store.create_job("token parity", label=job_label_for_session(sid))
    _save_task(harness_store, job.id, str(repo), session_id=sid, model="worker-model")
    harness_store.save_artifact(
        _verification(
            job.id, "t1", "worker-model", 200_000, 10_000, tokens_cached=100_000
        )
    )

    # Pilot meters: 15k used of which 5k in + 2k out are worker-attributed.
    # Pilot has 40k independent cache; swarm store has 100k independent cache.
    # Source-owned lanes must NOT zero pilot when swarm exceeds the pilot meter.
    old_pilot = server._pilot
    pilot = SimpleNamespace(
        _tokens_used=15_000,
        _tokens_in=10_000,
        _tokens_out=5_000,
        _tokens_cached=40_000,
        _worker_tokens_in=5_000,
        _worker_tokens_out=2_000,
        _worker_tokens_cached=0,
        _worker_cost_usd=0.0,
        state_dir=str(harness_dir),
        harness_session_id=sid,
        live_local_jobs=lambda: [],
    )
    monkeypatch.setattr(server, "_pilot", pilot)
    monkeypatch.setattr(
        server,
        "_boot_usage_meters",
        lambda: {
            "_tokens_used": 15_000,
            "_tokens_in": 10_000,
            "_tokens_out": 5_000,
            "_tokens_cached": 40_000,
            "_worker_tokens_in": 5_000,
            "_worker_tokens_out": 2_000,
            "_worker_tokens_cached": 0,
            "_worker_cost_usd": 0.0,
        },
    )
    monkeypatch.setattr(server, "_boot_session_cost", lambda price_in, price_out: 0.01)
    monkeypatch.setattr(
        "pmharness.registry.resolve_price",
        lambda driver: (3.0, 15.0),
    )

    httpd, port = _api_server(str(harness_dir))
    try:
        monkeypatch.setattr(
            server,
            "_jobs_snapshot",
            lambda: [
                {
                    "id": job.id,
                    "goal": "token parity",
                    "status": "complete",
                    "adapter": "agentic",
                    "label": job_label_for_session(sid),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            ],
        )
        monkeypatch.setattr(
            server._session,
            "state",
            lambda: SimpleNamespace(
                store=harness_store,
                format_artifacts=lambda arts: [],
                job_artifacts=lambda jid: [],
            ),
        )
        monkeypatch.setattr(
            "harness.cli_job_merge.resolve_cli_state_dir",
            lambda workspace_root="": None,
        )
        monkeypatch.setattr(
            server,
            "_swarm_registry",
            lambda: [_registry_spec("worker-model", input_per_mtok_usd=3.0)],
        )
        monkeypatch.setattr(server, "_job_savings_fields", lambda jid: {})
        monkeypatch.setattr(server, "_job_in_cost_window", lambda created_at: True)
        server._cfg.repo = str(repo)

        scoped = urllib.parse.quote(str(repo), safe="")
        usage = json.loads(
            _api_get(port, f"/api/usage?repo={scoped}", server._TOKEN).read().decode()
        )
        job_rows = usage["jobs"]
        assert len(job_rows) == 1
        job_tokens = int(job_rows[0]["tokens"] or 0)
        assert job_tokens == 210_000  # 200k in + 10k out
        pilot_only = max(0, 15_000 - 5_000 - 2_000)
        assert usage["session"]["tokens_used"] == pilot_only + job_tokens

        # Independent slices: pilot 40k + swarm 100k (no worker-fold overlap).
        assert usage["session"]["pilot_cache_read_tokens"] == 40_000
        assert usage["session"]["swarm_cache_read_tokens"] == 100_000
        assert usage["session"]["tokens_cached"] == 140_000
        # 40k pilot @ $3/MTok * 0.9 = 0.108
        assert abs(usage["session"]["cache_savings_usd"] - 0.108) < 1e-9
        # 100k cached @ $3/MTok * 0.9 = 0.27
        assert abs(usage["session"]["cache_saved_usd_swarm"] - 0.27) < 1e-9
        # Pilot input = 10k - 5k worker = 5k; cache 40k > input → ratio null
        # (never clamp impossible provider/meter skew to a perfect 100% hit).
        assert usage["session"]["pilot_input_tokens"] == 5_000
        assert usage["session"]["pilot_cache_hit_ratio"] is None
        assert usage["session"]["prompt_cache_hit_ratio"] is None
        # Absolute reads still surface when the ratio is refused.
        assert usage["session"]["prompt_cache_read_tokens"] == 140_000
        # Swarm input = 200k; ratio = 100k/200k = 0.5
        assert usage["session"]["swarm_input_tokens"] == 200_000
        assert abs(usage["session"]["swarm_cache_hit_ratio"] - 0.5) < 1e-9

        live = json.loads(
            _api_get(port, f"/api/swarm/live?repo={scoped}", server._TOKEN).read().decode()
        )
        # Repo-scoped live excludes the active pilot's process meters; only
        # that repo's stamped session spend + store jobs (here: jobs only).
        assert live["session"]["tokens_used"] == job_tokens
        assert live["session"]["tokens_cached"] == 100_000
        assert live["session"]["pilot_cache_read_tokens"] == 0
        assert live["session"]["swarm_cache_read_tokens"] == 100_000
        assert abs(live["session"]["cache_savings_usd"] - 0.0) < 1e-9
        assert abs(live["session"]["cache_saved_usd_swarm"] - 0.27) < 1e-9
    finally:
        httpd.shutdown()
        server._pilot = old_pilot


def test_api_usage_combined_cache_keeps_pilot_only_when_disjoint(tmp_path, monkeypatch):
    """Independent pilot + swarm cache slices add; no cross-lane subtraction."""
    from harness.sessions import SessionStore

    repo = tmp_path / "repo"
    repo.mkdir()
    harness_dir = tmp_path / "harness-state"
    harness_store = create_store("sqlite", str(harness_dir))

    sess_store = SessionStore(str(tmp_path / "harness_sessions.json"))
    row = sess_store.create(title="cache combine", repo=str(repo), workspace_root=str(repo))
    sid = row["id"]
    monkeypatch.setattr(server, "_sessions", sess_store)

    job = harness_store.create_job("cache combine", label=job_label_for_session(sid))
    _save_task(harness_store, job.id, str(repo), session_id=sid, model="worker-model")
    harness_store.save_artifact(
        _verification(
            job.id, "t1", "worker-model", 50_000, 5_000, tokens_cached=20_000
        )
    )

    old_pilot = server._pilot
    monkeypatch.setattr(
        server,
        "_boot_usage_meters",
        lambda: {
            "_tokens_used": 8_000,
            "_tokens_in": 6_000,
            "_tokens_out": 2_000,
            "_tokens_cached": 50_000,
            "_worker_tokens_in": 0,
            "_worker_tokens_out": 0,
            "_worker_tokens_cached": 0,
            "_worker_cost_usd": 0.0,
        },
    )
    monkeypatch.setattr(server, "_boot_session_cost", lambda price_in, price_out: 0.01)
    monkeypatch.setattr(
        "pmharness.registry.resolve_price",
        lambda driver: (3.0, 15.0),
    )

    httpd, port = _api_server(str(harness_dir))
    try:
        monkeypatch.setattr(
            server,
            "_jobs_snapshot",
            lambda: [
                {
                    "id": job.id,
                    "goal": "cache combine",
                    "status": "complete",
                    "adapter": "agentic",
                    "label": job_label_for_session(sid),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            ],
        )
        monkeypatch.setattr(
            server._session,
            "state",
            lambda: SimpleNamespace(store=harness_store),
        )
        monkeypatch.setattr(
            "harness.cli_job_merge.resolve_cli_state_dir",
            lambda workspace_root="": None,
        )
        monkeypatch.setattr(
            server,
            "_swarm_registry",
            lambda: [_registry_spec("worker-model", input_per_mtok_usd=3.0)],
        )
        monkeypatch.setattr(server, "_job_savings_fields", lambda jid: {})
        monkeypatch.setattr(server, "_job_in_cost_window", lambda created_at: True)
        server._cfg.repo = str(repo)

        scoped = urllib.parse.quote(str(repo), safe="")
        usage = json.loads(
            _api_get(port, f"/api/usage?repo={scoped}", server._TOKEN).read().decode()
        )
        # Independent: pilot 50k + swarm 20k = 70k (no worker-fold leakage).
        assert usage["session"]["pilot_cache_read_tokens"] == 50_000
        assert usage["session"]["swarm_cache_read_tokens"] == 20_000
        assert usage["session"]["tokens_cached"] == 70_000
        # 50k @ $3/MTok * 0.9 = 0.135
        assert abs(usage["session"]["cache_savings_usd"] - 0.135) < 1e-9
        # 20k @ $3/MTok * 0.9 = 0.054
        assert abs(usage["session"]["cache_saved_usd_swarm"] - 0.054) < 1e-9
        job_tokens = int(usage["jobs"][0]["tokens"] or 0)
        assert usage["session"]["tokens_used"] == 8_000 + job_tokens
    finally:
        httpd.shutdown()
        server._pilot = old_pilot


def test_cache_saved_usd_swarm_openrouter_slug_via_registry_prefix():
    """Usage OpenRouter slug resolves against agentic-prefixed registry row."""
    registry = [
        _registry_spec(
            "agentic/deepseek/deepseek-v4-pro",
            input_per_mtok_usd=0.435,
            output_per_mtok_usd=0.87,
        ),
    ]
    tokens_cached = 290_816
    arts = [
        _verification(
            "j1",
            "t1",
            "deepseek/deepseek-v4-pro",
            300_000,
            10_000,
            tokens_cached=tokens_cached,
        ),
    ]
    expected = (tokens_cached / 1_000_000.0) * 0.435 * 0.9
    detail = _cache_saved_usd_swarm_detail(arts, registry)
    assert detail["swarm_cache_read_tokens"] == tokens_cached
    assert detail["swarm_cache_unpriced_tokens"] == 0
    assert detail["swarm_cache_savings_basis"] == "actual_usage"
    assert abs(detail["cache_saved_usd_swarm"] - expected) < 1e-6
    assert abs(_cache_saved_usd_swarm(arts, registry) - expected) < 1e-6


def test_cache_saved_usd_swarm_codex_dotted_id_matches_dashed_registry():
    """Prefixed/dotted Codex ids price via fuzzy match to dashed registry ids."""
    registry = [
        _registry_spec(
            "gpt-5-3-codex",
            input_per_mtok_usd=2.5,
            output_per_mtok_usd=10.0,
        ),
    ]
    arts = [
        _verification(
            "j1",
            "t1",
            "codex/gpt.5.3.codex",
            100_000,
            5_000,
            tokens_cached=50_000,
        ),
    ]
    expected = (50_000 / 1_000_000.0) * 2.5 * 0.9
    detail = _cache_saved_usd_swarm_detail(arts, registry)
    assert detail["swarm_cache_read_tokens"] == 50_000
    assert detail["swarm_cache_unpriced_tokens"] == 0
    assert abs(detail["cache_saved_usd_swarm"] - expected) < 1e-9


def test_cache_saved_usd_swarm_unpriceable_model_keeps_tokens_honest_basis(
    monkeypatch,
):
    """Unknown worker models keep cache tokens but contribute zero USD."""
    monkeypatch.setattr(
        "harness.api.routing_savings._pmharness_positive_rates",
        lambda _mid: (0.0, 0.0),
    )
    registry = [_registry_spec("priced-model", input_per_mtok_usd=3.0)]
    arts = [
        _verification(
            "j1",
            "t1",
            "totally-unknown-worker",
            100_000,
            5_000,
            tokens_cached=80_000,
        ),
    ]
    detail = _cache_saved_usd_swarm_detail(arts, registry)
    assert _tokens_cached_swarm(arts) == 80_000
    assert detail["swarm_cache_read_tokens"] == 80_000
    assert detail["cache_saved_usd_swarm"] == 0.0
    assert detail["swarm_cache_unpriced_tokens"] == 80_000
    assert detail["swarm_cache_savings_basis"] == "unknown"
    assert _cache_saved_usd_swarm(arts, registry) == 0.0


def test_cache_saved_usd_swarm_pmharness_fallback_when_registry_misses(
    monkeypatch,
):
    """OpenRouter slug falls through registry miss to pmharness catalog rates."""
    monkeypatch.setattr(
        "harness.api.routing_savings._pmharness_positive_rates",
        lambda _mid: (0.435, 0.87),
    )
    arts = [
        _verification(
            "j1",
            "t1",
            "deepseek/deepseek-v4-pro",
            200_000,
            10_000,
            tokens_cached=100_000,
        ),
    ]
    expected = (100_000 / 1_000_000.0) * 0.435 * 0.9
    assert abs(_cache_saved_usd_swarm(arts, []) - expected) < 1e-9


def test_api_usage_exposes_cache_token_split_reconcilable_with_usd(
    tmp_path, monkeypatch
):
    """Pilot vs swarm cache read tokens reconcile to aggregate tokens_cached + USD."""
    from harness.sessions import SessionStore

    repo = tmp_path / "repo"
    repo.mkdir()
    harness_dir = tmp_path / "harness-state"
    harness_store = create_store("sqlite", str(harness_dir))

    sess_store = SessionStore(str(tmp_path / "harness_sessions.json"))
    row = sess_store.create(title="cache split", repo=str(repo), workspace_root=str(repo))
    sid = row["id"]
    monkeypatch.setattr(server, "_sessions", sess_store)

    job = harness_store.create_job("cache split", label=job_label_for_session(sid))
    _save_task(harness_store, job.id, str(repo), session_id=sid, model="worker-model")
    harness_store.save_artifact(
        _verification(
            job.id, "t1", "worker-model", 50_000, 5_000, tokens_cached=20_000
        )
    )

    monkeypatch.setattr(
        server,
        "_boot_usage_meters",
        lambda: {
            "_tokens_used": 85_000,
            # 80k in = 30k pilot-native + 50k worker-folded (matches store job)
            "_tokens_in": 80_000,
            "_tokens_out": 5_000,
            # 50k total cached = 30k pilot-native + 20k worker-folded (also in store)
            "_tokens_cached": 50_000,
            "_worker_tokens_in": 50_000,
            "_worker_tokens_out": 5_000,
            "_worker_tokens_cached": 20_000,
            "_worker_cost_usd": 0.0,
        },
    )
    monkeypatch.setattr(server, "_boot_session_cost", lambda price_in, price_out: 0.01)
    monkeypatch.setattr(
        "pmharness.registry.resolve_price",
        lambda driver: (3.0, 15.0),
    )

    httpd, port = _api_server(str(harness_dir))
    try:
        monkeypatch.setattr(
            server,
            "_jobs_snapshot",
            lambda: [
                {
                    "id": job.id,
                    "goal": "cache split",
                    "status": "complete",
                    "adapter": "agentic",
                    "label": job_label_for_session(sid),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            ],
        )
        monkeypatch.setattr(
            server._session,
            "state",
            lambda: SimpleNamespace(store=harness_store),
        )
        monkeypatch.setattr(
            "harness.cli_job_merge.resolve_cli_state_dir",
            lambda workspace_root="": None,
        )
        monkeypatch.setattr(
            server,
            "_swarm_registry",
            lambda: [_registry_spec("worker-model", input_per_mtok_usd=3.0)],
        )
        monkeypatch.setattr(server, "_job_savings_fields", lambda jid: {})
        monkeypatch.setattr(server, "_job_in_cost_window", lambda created_at: True)
        server._cfg.repo = str(repo)

        scoped = urllib.parse.quote(str(repo), safe="")
        usage = json.loads(
            _api_get(port, f"/api/usage?repo={scoped}", server._TOKEN).read().decode()
        )
        sess = usage["session"]
        pilot_cached = int(sess["pilot_cache_read_tokens"])
        swarm_cached = int(sess["swarm_cache_read_tokens"])
        assert pilot_cached == 30_000
        assert swarm_cached == 20_000
        # Absolute tokens_cached peels worker overlap once (not pilot+swarm double).
        assert sess["tokens_cached"] == pilot_cached + swarm_cached
        assert abs(sess["cache_savings_usd"] - (pilot_cached / 1_000_000.0) * 3.0 * 0.9) < 1e-9
        assert abs(sess["cache_saved_usd_swarm"] - (swarm_cached / 1_000_000.0) * 3.0 * 0.9) < 1e-9
        assert sess["pilot_input_tokens"] == 30_000
        assert abs(sess["pilot_cache_hit_ratio"] - 1.0) < 1e-9
        assert sess["swarm_input_tokens"] == 50_000
        assert abs(sess["swarm_cache_hit_ratio"] - 0.4) < 1e-9
        assert sess["prompt_input_tokens"] == 80_000
        assert abs(sess["prompt_cache_hit_ratio"] - (50_000 / 80_000)) < 1e-9
    finally:
        httpd.shutdown()


def test_cache_hit_ratio_is_cache_read_over_input_never_total():
    from harness.api.cost_accounting import _cache_hit_ratio

    assert abs(_cache_hit_ratio(96_800, 100_000) - 0.968) < 1e-9
    assert abs(_cache_hit_ratio(86_600, 100_000) - 0.866) < 1e-9
    # Invalid provider/meter skew: never clamp into a perfect 100% hit.
    assert _cache_hit_ratio(120_000, 100_000) is None


def test_cache_hit_ratio_unavailable_denominator_is_none_not_zero():
    from harness.api.cost_accounting import _cache_hit_ratio

    assert _cache_hit_ratio(10_000, 0) is None
    assert _cache_hit_ratio(10_000, None) is None
    assert _cache_hit_ratio(0, 0) is None


def test_source_owned_lanes_independent_pilot_and_swarm_no_subtraction_leakage():
    from harness.api.cost_accounting import _source_owned_cache_lanes

    lanes = _source_owned_cache_lanes(
        pilot_tokens_in=100_000,
        pilot_tokens_cached=96_800,
        worker_tokens_in=0,
        worker_tokens_cached=0,
        swarm_tokens_in=200_000,
        swarm_tokens_cached=173_200,
    )
    assert lanes["pilot_cache_read_tokens"] == 96_800
    assert lanes["swarm_cache_read_tokens"] == 173_200
    assert lanes["tokens_cached"] == 96_800 + 173_200
    assert lanes["prompt_cache_read_tokens"] == lanes["tokens_cached"]
    assert abs(lanes["pilot_cache_hit_ratio"] - 0.968) < 1e-9
    assert abs(lanes["swarm_cache_hit_ratio"] - 0.866) < 1e-9
    assert abs(lanes["prompt_cache_hit_ratio"] - ((96_800 + 173_200) / 300_000)) < 1e-9


def test_source_owned_lanes_worker_fold_does_not_double_count():
    from harness.api.cost_accounting import _source_owned_cache_lanes

    lanes = _source_owned_cache_lanes(
        pilot_tokens_in=80_000,
        pilot_tokens_cached=50_000,
        worker_tokens_in=50_000,
        worker_tokens_cached=20_000,
        swarm_tokens_in=50_000,
        swarm_tokens_cached=20_000,
    )
    assert lanes["pilot_cache_read_tokens"] == 30_000
    assert lanes["swarm_cache_read_tokens"] == 20_000
    assert lanes["tokens_cached"] == 50_000
    assert lanes["prompt_cache_read_tokens"] == lanes["tokens_cached"]
    assert lanes["pilot_cache_savings_tokens"] == 30_000


def test_source_owned_lanes_local_only_residual_workers_in_ratio():
    """Local workers with empty swarm store must not show native-only % vs full reads."""
    from harness.api.cost_accounting import _source_owned_cache_lanes

    lanes = _source_owned_cache_lanes(
        pilot_tokens_in=100_000,
        pilot_tokens_cached=70_000,
        worker_tokens_in=80_000,
        worker_tokens_cached=65_000,
        swarm_tokens_in=0,
        swarm_tokens_cached=0,
    )
    assert lanes["tokens_cached"] == 70_000
    assert lanes["pilot_cache_read_tokens"] == 70_000
    assert lanes["prompt_cache_read_tokens"] == 70_000
    assert lanes["pilot_cache_savings_tokens"] == 70_000
    assert lanes["pilot_input_tokens"] == 100_000
    assert lanes["prompt_input_tokens"] == 100_000
    assert abs(lanes["pilot_cache_hit_ratio"] - 0.7) < 1e-9
    assert abs(lanes["prompt_cache_hit_ratio"] - 0.7) < 1e-9
    assert lanes["swarm_cache_hit_ratio"] is None


def test_source_owned_lanes_residual_without_matching_input_nulls_ratio():
    """Residual cache without max(0,w_in-s_in) must null ratios, not invent a %."""
    from harness.api.cost_accounting import _source_owned_cache_lanes

    lanes = _source_owned_cache_lanes(
        pilot_tokens_in=100_000,
        pilot_tokens_cached=70_000,
        worker_tokens_in=50_000,
        worker_tokens_cached=65_000,
        swarm_tokens_in=50_000,
        swarm_tokens_cached=20_000,
    )
    # Reads still reconcile; ratios refuse the unmatched residual.
    assert lanes["tokens_cached"] == 70_000
    assert lanes["prompt_cache_read_tokens"] == 70_000
    assert lanes["pilot_cache_read_tokens"] == 50_000
    assert lanes["swarm_cache_read_tokens"] == 20_000
    assert lanes["pilot_cache_hit_ratio"] is None
    assert lanes["prompt_cache_hit_ratio"] is None
    assert abs(lanes["swarm_cache_hit_ratio"] - 0.4) < 1e-9


def test_source_owned_lanes_cache_gt_input_nulls_lane_ratio():
    from harness.api.cost_accounting import _source_owned_cache_lanes

    lanes = _source_owned_cache_lanes(
        pilot_tokens_in=10_000,
        pilot_tokens_cached=12_000,
        worker_tokens_in=0,
        worker_tokens_cached=0,
        swarm_tokens_in=0,
        swarm_tokens_cached=0,
    )
    assert lanes["pilot_cache_read_tokens"] == 12_000
    assert lanes["tokens_cached"] == 12_000
    assert lanes["prompt_cache_read_tokens"] == 12_000
    assert lanes["pilot_cache_hit_ratio"] is None
    assert lanes["prompt_cache_hit_ratio"] is None


def test_tokens_in_swarm_dedupes_per_task():
    arts = [
        _verification("j1", "t1", "worker-model", 200_000, 10_000, tokens_cached=100_000),
        _verification("j1", "t1", "worker-model", 200_000, 10_000, tokens_cached=100_000),
        _verification("j1", "t2", "worker-model", 50_000, 5_000, tokens_cached=40_000),
    ]
    from harness.api.swarm_cost import _tokens_in_swarm

    assert _tokens_in_swarm(arts) == 250_000
