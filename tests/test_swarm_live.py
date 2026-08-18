"""Tests for swarm live GET endpoint."""
import json
import threading
import urllib.request
import urllib.error
import urllib.parse
import tempfile
import shutil
import os
import subprocess
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

def _server(tmp_state_dir):
    import harness.server as srv
    # Hermetic: prior suite cases can leave boot carry that /api/usage
    # prices but /api/swarm/live's pilot split ignores.
    srv._boot_usage_reset_for_tests()
    srv._session.state_dir = tmp_state_dir

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, port, srv

def _get(port, path, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers or {}, method="GET")
    return urllib.request.urlopen(req, timeout=10)

def test_swarm_live_returns_expected_shape():
    tmp_dir = tempfile.mkdtemp()
    try:
        httpd, port, srv = _server(tmp_dir)
        try:
            # First try without token -> expect 403
            try:
                _get(port, "/api/swarm/live")
                assert False, "should have failed with 403"
            except urllib.error.HTTPError as e:
                assert e.code == 403

            # Try with valid token
            headers = {"X-Harness-Token": srv._TOKEN}
            resp = _get(port, "/api/swarm/live", headers=headers)
            assert resp.status == 200
            
            data = json.loads(resp.read().decode())
            
            # Verify keys in the returned shape
            assert "session" in data
            assert "jobs" in data
            
            session_data = data["session"]
            assert "tokens_used" in session_data
            assert "est_cost_usd" in session_data
            assert "driver" in session_data
            
            assert isinstance(session_data["tokens_used"], int)
            assert isinstance(session_data["est_cost_usd"], (int, float))
            assert isinstance(session_data["driver"], str)
            
            assert isinstance(data["jobs"], list)
        finally:
            httpd.shutdown()
    finally:
        shutil.rmtree(tmp_dir)


def test_swarm_live_surfaces_local_provider_jobs():
    """Regression: provider-native workers (job_id 'local-*') run on the user's
    own key and never enter the durable store, so the swarm panel showed
    "No swarm jobs yet" while one was visibly running. They must now appear in
    /api/swarm/live and flip to a terminal state when the worker finishes."""
    tmp_dir = tempfile.mkdtemp()
    try:
        httpd, port, srv = _server(tmp_dir)
        try:
            headers = {"X-Harness-Token": srv._TOKEN}

            workspace = str(tmp_dir)
            srv._cfg.repo = workspace
            if not srv._sessions.active:
                srv._sessions.create("Swarm live test", repo=workspace, workspace_root=workspace)
            srv._sync_pilot_session_id()
            srv._pilot._register_local_job(
                "local-abc123",
                "Build the scheduler",
                cwd=workspace,
            )

            data = json.loads(_get(port, "/api/swarm/live", headers=headers).read().decode())
            live = [j for j in data["jobs"] if j.get("id") == "local-abc123"]
            assert len(live) == 1, "running local job must show in the panel"
            assert live[0]["goal"] == "Build the scheduler"
            assert "run" in live[0]["status"].lower()
            assert live[0]["tasks"] and live[0]["tasks"][0]["status"] == "running"

            srv._pilot._finish_local_job(
                "local-abc123", ok=True, summary="Applied patch", files=["a.py", "b.py"],
                tokens=12_500, est_cost_usd=0.42,
            )

            data = json.loads(_get(port, "/api/swarm/live", headers=headers).read().decode())
            done = [j for j in data["jobs"] if j.get("id") == "local-abc123"][0]
            assert done["status"] == "completed"
            assert done["tokens"] == 12_500
            assert abs(done["est_cost_usd"] - 0.42) < 1e-6
            assert done["artifacts"] and "2 files" in done["artifacts"][0]["headline"]
        finally:
            httpd.shutdown()
            srv._pilot._local_jobs.clear()
    finally:
        shutil.rmtree(tmp_dir)


def test_session_total_includes_swarm_store_job_cost(monkeypatch):
    """Regression: swarm store jobs bill on their own adapters, but their cost
    never rolled into the session total shown in the status bar. /api/usage and
    /api/swarm/live must both add store-job spend (and only store-job spend --
    local provider jobs are already inside _worker_cost_usd)."""
    tmp_dir = tempfile.mkdtemp()
    try:
        httpd, port, srv = _server(tmp_dir)
        try:
            headers = {"X-Harness-Token": srv._TOKEN}
            baseline = json.loads(
                _get(port, "/api/usage", headers=headers).read().decode()
            )["session"]["est_cost_usd"]

            # Monkeypatching _scoped_jobs_with_stores bypasses annotate_jobs_accounting;
            # tag fake store rows as Marionette-owned so session totals count them.
            _owned_harness_job = {
                "id": "job_fake1",
                "source": "harness",
                "accounting_owned": True,
                "accounting_scope": "marionette",
            }
            monkeypatch.setattr(srv, "_jobs_snapshot", lambda: [{"id": "job_fake1"}])
            monkeypatch.setattr(
                srv,
                "_scoped_jobs_with_stores",
                lambda repo_root=None: ([_owned_harness_job], srv._session.state().store, None),
            )
            monkeypatch.setattr(
                srv, "_scoped_jobs_snapshot", lambda repo_root=None: [_owned_harness_job]
            )
            monkeypatch.setattr(
                srv, "_job_swarm_accounting", lambda arts, registry: (50_000, 0.37)
            )
            # Bust the short-TTL /api/usage boot-pill cache so the monkeypatched
            # accounting is visible on the next GET (StatusBar polls ~10s apart).
            srv._usage_cache_clear_for_tests()

            usage = json.loads(_get(port, "/api/usage", headers=headers).read().decode())
            assert abs(usage["session"]["est_cost_usd"] - (baseline + 0.37)) < 1e-6

            live = json.loads(
                _get(port, "/api/swarm/live", headers=headers).read().decode()
            )
            assert abs(live["session"]["est_cost_usd"] - (baseline + 0.37)) < 1e-6
        finally:
            httpd.shutdown()
    finally:
        shutil.rmtree(tmp_dir)


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, capture_output=True)
    (path / "README").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "README"], cwd=path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, capture_output=True)
    return path


def _set_running(store, job_id: str) -> None:
    try:
        store.update_job_status(job_id, "running")
    except Exception:
        store.set_job_status(job_id, "running")


def _save_cwd_task(store, job_id: str, cwd: str, session_id: str = "") -> None:
    from harness.job_scoping import stamp_task_payload
    from puppetmaster.models import Task

    payload = stamp_task_payload({"cwd": cwd}, session_id=session_id, cwd=cwd)
    if not session_id:
        payload = {"cwd": cwd}
    store.save_task(Task(
        job_id=job_id,
        role="implement",
        instruction="do work",
        adapter="agentic",
        payload=payload,
    ))


def test_swarm_live_held_open_scratch_hijack_exposes_only_host_local(tmp_path, monkeypatch):
    """Reproduce PUPPETMASTER_STATE_DIR hijack while scratch job_* and local-* coexist.

    Production merge functions stay live. Cross-project is on, but isolated
    under a throwaway app_state_root so developer stores are never opened.
    """
    from harness.cli_job_merge import (
        mark_marionette_host_scratch,
        merge_running_cli_jobs_all_projects,
        merge_scoped_cli_jobs,
        resolve_cli_state_dir,
    )
    from harness.job_scoping import job_label_for_session
    import puppetmaster.state as pm_state
    from puppetmaster.store_factory import create_store

    assert merge_scoped_cli_jobs.__module__ == "harness.cli_job_merge"
    assert merge_running_cli_jobs_all_projects.__module__ == "harness.cli_job_merge"

    repo = _git_repo(tmp_path / "repo")
    monkeypatch.setattr(pm_state, "app_state_root", lambda: tmp_path / "pm-root")
    monkeypatch.setenv("HARNESS_CLI_CROSS_PROJECT", "1")
    monkeypatch.setattr(
        "harness.local_job_routing.preview_agentic_route",
        lambda *a, **k: {
            "model_id": "gpt-5.6-luna",
            "est_cost_usd": 0.01,
            "artifact": {
                "type": "ROUTING",
                "headline": "Routed to gpt-5.6-luna",
                "model": "gpt-5.6-luna",
            },
        },
    )

    goal = "create proof.txt"
    ws_dir = pm_state.default_state_dir(repo)
    ws_dir.mkdir(parents=True, exist_ok=True)
    ws_store = create_store("sqlite", str(ws_dir))
    external = ws_store.create_job(goal)
    _save_cwd_task(ws_store, external.id, str(repo))
    _set_running(ws_store, external.id)

    foreign_dir = pm_state.app_state_root() / "projects" / "foreign-cli"
    foreign_dir.mkdir(parents=True, exist_ok=True)
    foreign_store = create_store("sqlite", str(foreign_dir))
    foreign = foreign_store.create_job(goal)
    _save_cwd_task(foreign_store, foreign.id, str(repo))
    _set_running(foreign_store, foreign.id)

    leaked = pm_state.app_state_root() / "projects" / "pmh-edit-leaked"
    leaked.mkdir(parents=True, exist_ok=True)
    mark_marionette_host_scratch(leaked)
    leaked_store = create_store("sqlite", str(leaked))
    leaked_job = leaked_store.create_job(goal)
    _set_running(leaked_store, leaked_job.id)

    scratch = tmp_path / "pmh-edit-hijack"
    scratch.mkdir()
    mark_marionette_host_scratch(scratch)
    scratch_store = create_store("sqlite", str(scratch))
    httpd, port, srv = _server(str(tmp_path / "harness-state"))
    try:
        headers = {"X-Harness-Token": srv._TOKEN}
        srv._cfg.repo = str(repo)
        if not srv._sessions.active:
            srv._sessions.create(
                "held-open", repo=str(repo), workspace_root=str(repo),
            )
        srv._sync_pilot_session_id()
        sid = srv._pilot.harness_session_id or srv._sessions.active or ""

        scratch_job = scratch_store.create_job(
            goal,
            label=job_label_for_session(sid, dispatch_id="local-host-a"),
        )
        _save_cwd_task(scratch_store, scratch_job.id, str(repo), session_id=sid)
        _set_running(scratch_store, scratch_job.id)
        monkeypatch.setenv("PUPPETMASTER_STATE_DIR", str(scratch))

        resolved = resolve_cli_state_dir(str(repo))
        assert resolved is not None
        assert Path(resolved).resolve() == Path(ws_dir).resolve()
        assert Path(os.environ["PUPPETMASTER_STATE_DIR"]).resolve() == scratch.resolve()

        srv._pilot._register_local_job(
            "local-host-a", goal, role="implement",
            cwd=str(repo), engine="agentic", model="",
        )
        srv._pilot._register_local_job(
            "local-host-b", goal, role="implement",
            cwd=str(repo), engine="agentic", model="",
        )

        scoped = urllib.parse.quote(str(repo), safe="")
        data = json.loads(
            _get(port, f"/api/swarm/live?repo={scoped}", headers=headers).read().decode()
        )
        ids = [j.get("id") for j in data["jobs"]]
        assert "local-host-a" in ids
        assert "local-host-b" in ids
        assert external.id in ids
        assert foreign.id in ids
        assert scratch_job.id not in ids
        assert leaked_job.id not in ids
        locals_ = [j for j in data["jobs"] if str(j.get("id") or "").startswith("local-")]
        assert len(locals_) == 2
        assert all(j.get("model") == "" for j in locals_)
        assert all(j.get("source") == "harness" for j in locals_)
        ext_row = next(j for j in data["jobs"] if j.get("id") == external.id)
        assert ext_row["source"] == "cli"
        assert ext_row["goal"] == goal
    finally:
        httpd.shutdown()
        srv._pilot._local_jobs.clear()
