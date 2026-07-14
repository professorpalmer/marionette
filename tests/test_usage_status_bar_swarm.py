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
):
    return Artifact(
        job_id=job_id,
        task_id=task_id,
        type=ArtifactType.ROUTING,
        created_by="router",
        payload={
            "model_id": model_id,
            "adapter": "agentic",
            "policy": policy,
            "baseline_cost_usd": baseline,
            "estimated_cost_usd": estimated,
        },
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


def test_api_usage_includes_cli_store_job_dollars(tmp_path, monkeypatch):
    """RC1: CLI-store swarm spend must appear in /api/usage boot + jobs list."""
    repo = tmp_path / "repo"
    repo.mkdir()
    harness_dir = tmp_path / "harness-state"
    harness_store = create_store("sqlite", str(harness_dir))
    _cli_store, cli_dir, cli_job_id = _seed_cli_store(tmp_path, str(repo))

    httpd, port = _api_server(str(harness_dir))
    try:
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
        # Force the CLI job into the boot cost window regardless of store stamp.
        monkeypatch.setattr(server, "_job_in_cost_window", lambda created_at: True)
        server._cfg.repo = str(repo)

        scoped = urllib.parse.quote(str(repo), safe="")
        usage = json.loads(
            _api_get(port, f"/api/usage?repo={scoped}", server._TOKEN).read().decode()
        )
        job_rows = [j for j in usage["jobs"] if j.get("job_id") == cli_job_id]
        assert len(job_rows) == 1
        assert abs(job_rows[0]["est_cost_usd"] - 0.07) < 1e-6
        assert usage["session"]["est_cost_usd"] >= 0.07
    finally:
        httpd.shutdown()


def test_session_total_includes_task_stamp_and_unstamped_visible(
    tmp_path, monkeypatch
):
    """RC2: task-payload stamp (label-less) + workspace-visible unstamped job."""
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
        # stamped: 10k*1 + 2k*2 = 0.014; unstamped: 20k*1 + 4k*2 = 0.028
        assert abs(total["est_cost_usd"] - 0.042) < 1e-6
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
    # Usage so the job prices > 0 (otherwise boot pill may stay at pilot-only).
    harness_store.save_artifact(
        _verification(job.id, "t1", "cheap-model", 1_000, 500)
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
        _routing(job.id, "t1", policy="balanced", baseline=0.50, estimated=0.10)
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
            lambda: [_registry_spec("worker-model", input_per_mtok_usd=3.0)],
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
        assert abs(row["routing_saved_usd"] - 0.40) < 1e-9
        # 100k cached @ $3/MTok * 0.9 = 0.27
        assert abs(row["cache_saved_usd"] - 0.27) < 1e-9
        assert row["tokens_cached"] == 100_000
        assert row["tool_output_tokens_saved"] == 1200
        assert abs(row["tool_output_savings_usd"] - 0.0036) < 1e-9
        assert abs(live["session"]["routing_saved_usd"] - 0.40) < 1e-9
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

    # Pilot meters: 15k used of which 5k in + 2k out are worker-attributed;
    # 40k cached of which 100k swarm would clamp pilot_only_cached to 0.
    # Use a smaller pilot cache so pilot_only_cached stays positive.
    old_pilot = server._pilot
    pilot = SimpleNamespace(
        _tokens_used=15_000,
        _tokens_in=10_000,
        _tokens_out=5_000,
        _tokens_cached=40_000,
        _worker_tokens_in=5_000,
        _worker_tokens_out=2_000,
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
            "_worker_cost_usd": 0.0,
        },
    )
    monkeypatch.setattr(server, "_boot_session_cost", lambda price_in, price_out: 0.01)

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

        # swarm_cached=100k > pilot _t_cached=40k -> pilot_only_cached=0
        # display tokens_cached = 0 + 100_000; cache_savings_usd prices pilot only
        assert usage["session"]["tokens_cached"] == 100_000
        assert abs(usage["session"]["cache_savings_usd"] - 0.0) < 1e-9
        # 100k cached @ $3/MTok * 0.9 = 0.27
        assert abs(usage["session"]["cache_saved_usd_swarm"] - 0.27) < 1e-9

        live = json.loads(
            _api_get(port, f"/api/swarm/live?repo={scoped}", server._TOKEN).read().decode()
        )
        # Repo-scoped live excludes the active pilot's process meters; only
        # that repo's stamped session spend + store jobs (here: jobs only).
        assert live["session"]["tokens_used"] == job_tokens
        assert live["session"]["tokens_cached"] == 100_000
        assert abs(live["session"]["cache_savings_usd"] - 0.0) < 1e-9
        assert abs(live["session"]["cache_saved_usd_swarm"] - 0.27) < 1e-9
    finally:
        httpd.shutdown()
        server._pilot = old_pilot


def test_api_usage_combined_cache_keeps_pilot_only_when_disjoint(tmp_path, monkeypatch):
    """When pilot cache exceeds swarm attribution, keep the exclusive pilot portion."""
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
        # pilot_only_cached = 50k - 20k = 30k; display = 30k + 20k = 50k
        assert usage["session"]["tokens_cached"] == 50_000
        # 30k @ $3/MTok * 0.9 = 0.081
        assert abs(usage["session"]["cache_savings_usd"] - 0.081) < 1e-9
        # 20k @ $3/MTok * 0.9 = 0.054
        assert abs(usage["session"]["cache_saved_usd_swarm"] - 0.054) < 1e-9
        job_tokens = int(usage["jobs"][0]["tokens"] or 0)
        assert usage["session"]["tokens_used"] == 8_000 + job_tokens
    finally:
        httpd.shutdown()
        server._pilot = old_pilot
