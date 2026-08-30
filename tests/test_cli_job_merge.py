"""CLI Puppetmaster job merge into harness job views."""
from __future__ import annotations

import json
import os
import threading
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from harness.cli_job_merge import (
    is_marionette_host_scratch_dir,
    mark_marionette_host_scratch,
    merge_running_cli_jobs_all_projects,
    merge_scoped_cli_jobs,
    open_cli_durable_at,
    open_cli_durable_state,
    reset_merge_diag_for_tests,
    resolve_cli_state_dir,
)
from harness.job_scoping import (
    job_label_for_session,
    stamp_task_payload,
)
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
        "harness.job_scoping.filter_store_jobs_with_tasks",
        lambda rows, store, **kwargs: (rows, {}),
    )
    # Do not scan the developer's live PM project stores (SQLite locks hang).
    monkeypatch.setattr(
        "harness.cli_job_merge.merge_running_cli_jobs_all_projects",
        lambda **kwargs: [],
    )

    harness_rows = [
        {"id": harness_job.id, "goal": "harness goal", "status": "complete", "adapter": "agentic"},
    ]
    merged, _, _ = merge_scoped_cli_jobs(
        harness_rows,
        harness_store=harness_store,
        active_session_id="sess-x",
        repo_root=str(repo),
        workspace_root=str(repo),
    )
    by_id = {row["id"]: row for row in merged}
    assert by_id[harness_job.id]["source"] == "harness"
    assert by_id["cli-only"]["source"] == "cli"
    assert not by_id[harness_job.id].get("cross_project")
    assert not by_id["cli-only"].get("cross_project")
    assert len(merged) == 2


def test_unreadable_cli_store_contributes_nothing(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    harness_store = create_store("sqlite", str(tmp_path / "harness-state"))
    harness_job = harness_store.create_job("harness goal")
    _save_task(harness_store, harness_job.id, str(repo))

    reset_merge_diag_for_tests()

    monkeypatch.setattr("harness.cli_job_merge.open_cli_durable_state", lambda workspace_root="": None)
    monkeypatch.setattr(
        "harness.cli_job_merge.merge_running_cli_jobs_all_projects",
        lambda **kwargs: [],
    )

    harness_rows = [
        {"id": harness_job.id, "goal": "harness goal", "status": "complete", "adapter": "agentic"},
    ]
    merged, cli_store, _ = merge_scoped_cli_jobs(
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


def test_merge_running_cli_jobs_all_projects_scans_foreign_live(tmp_path, monkeypatch):
    """Low-level sibling scan still finds running rows; ownership is applied later."""
    foreign = tmp_path / "foreign-state"
    store = create_store("sqlite", str(foreign))
    job = store.create_job("foreign swarm")
    try:
        store.update_job_status(job.id, "running")
    except Exception:
        store.set_job_status(job.id, "running")

    monkeypatch.setattr(
        "puppetmaster.state.list_project_state_dirs",
        lambda: [foreign],
    )
    seen: set = set()
    rows = merge_running_cli_jobs_all_projects(seen_ids=seen, primary_state_dir="")
    assert len(rows) == 1
    assert rows[0]["id"] == job.id
    assert rows[0]["source"] == "cli"
    assert rows[0]["cli_state_dir"]
    assert rows[0]["cross_project"] is True
    assert job.id in seen


def test_merge_scoped_cli_jobs_drops_unstamped_foreign_running(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    harness_store = create_store("sqlite", str(tmp_path / "harness-state"))
    monkeypatch.setenv("HARNESS_CLI_CROSS_PROJECT", "1")

    monkeypatch.setattr("harness.cli_job_merge.open_cli_durable_state", lambda workspace_root="": None)
    monkeypatch.setattr(
        "harness.cli_job_merge.merge_running_cli_jobs_all_projects",
        lambda **kwargs: [{
            "id": "job_foreign_running",
            "goal": "foreign swarm",
            "status": "running",
            "source": "cli",
            "cross_project": True,
            "cli_state_dir": str(tmp_path / "foreign"),
        }],
    )

    merged, _, _ = merge_scoped_cli_jobs(
        [],
        harness_store=harness_store,
        active_session_id="sess-x",
        repo_root=str(repo),
        workspace_root=str(repo),
    )
    assert merged == []


def test_merge_scoped_cli_jobs_keeps_marionette_stamped_foreign(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    harness_store = create_store("sqlite", str(tmp_path / "harness-state"))
    label = job_label_for_session("sess-x")
    monkeypatch.setenv("HARNESS_CLI_CROSS_PROJECT", "1")

    monkeypatch.setattr("harness.cli_job_merge.open_cli_durable_state", lambda workspace_root="": None)
    monkeypatch.setattr(
        "harness.cli_job_merge.merge_running_cli_jobs_all_projects",
        lambda **kwargs: [{
            "id": "job_owned_foreign",
            "goal": "owned sibling",
            "status": "running",
            "source": "cli",
            "cross_project": True,
            "label": label,
            "cli_state_dir": str(tmp_path / "foreign"),
        }],
    )

    merged, _, _ = merge_scoped_cli_jobs(
        [],
        harness_store=harness_store,
        active_session_id="sess-x",
        repo_root=str(repo),
        workspace_root=str(repo),
    )
    assert [row["id"] for row in merged] == ["job_owned_foreign"]
    assert merged[0].get("session_id") == "sess-x"


def test_registered_id_does_not_admit_sibling_cli_store(tmp_path, monkeypatch):
    """A harness-registered id must not heal a colliding unstamped sibling row."""
    repo = tmp_path / "repo"
    repo.mkdir()
    harness_store = create_store("sqlite", str(tmp_path / "harness-state"))
    foreign = tmp_path / "foreign-collide"
    store = create_store("sqlite", str(foreign))
    job = store.create_job("foreign collide")
    _save_task(store, job.id, str(repo))
    store.update_job_status(job.id, "running")

    monkeypatch.setenv("HARNESS_CLI_CROSS_PROJECT", "1")
    monkeypatch.setattr("harness.cli_job_merge.open_cli_durable_state", lambda workspace_root="": None)
    monkeypatch.setattr(
        "harness.cli_job_merge.merge_running_cli_jobs_all_projects",
        lambda **kwargs: [{
            "id": job.id,
            "goal": "foreign collide",
            "status": "running",
            "source": "cli",
            "cross_project": True,
            "cli_state_dir": str(foreign),
        }],
    )

    merged, _, _ = merge_scoped_cli_jobs(
        [],
        harness_store=harness_store,
        active_session_id="sess-x",
        repo_root=str(repo),
        workspace_root=str(repo),
        registered_job_ids=[job.id],
    )
    assert merged == []


def test_sibling_task_only_stamp_uses_one_store_open(tmp_path, monkeypatch):
    """Listing and task-stamp ownership must share one sibling store open."""
    repo = tmp_path / "repo"
    repo.mkdir()
    harness_store = create_store("sqlite", str(tmp_path / "harness-state"))
    foreign = tmp_path / "foreign-one-open"
    store = create_store("sqlite", str(foreign))
    job = store.create_job("sibling one open")
    payload = stamp_task_payload(
        {"cwd": str(repo)},
        session_id="sess-x",
        origin="marionette",
    )
    store.save_task(Task(
        job_id=job.id,
        role="implement",
        instruction="do work",
        adapter="agentic",
        payload=payload,
    ))
    store.update_job_status(job.id, "running")

    opens: list[str] = []
    real_open = open_cli_durable_at

    def _count_open(state_dir, **kwargs):
        opens.append(str(state_dir))
        return real_open(state_dir, **kwargs)

    monkeypatch.setenv("HARNESS_CLI_CROSS_PROJECT", "1")
    monkeypatch.setattr("harness.cli_job_merge.open_cli_durable_state", lambda workspace_root="": None)
    monkeypatch.setattr("harness.cli_job_merge.open_cli_durable_at", _count_open)
    monkeypatch.setattr(
        "puppetmaster.state.list_project_state_dirs",
        lambda: [foreign],
    )

    merged, _, tasks_by_job = merge_scoped_cli_jobs(
        [],
        harness_store=harness_store,
        active_session_id="sess-x",
        repo_root=str(repo),
        workspace_root=str(repo),
    )
    assert [row["id"] for row in merged] == [job.id]
    assert merged[0]["session_id"] == "sess-x"
    assert job.id in tasks_by_job
    assert "tasks" not in merged[0]
    sibling_opens = [path for path in opens if str(foreign) in path]
    assert len(sibling_opens) == 1


def test_foreign_state_dir_candidates_skips_stale_and_caps(tmp_path, monkeypatch):
    """Bloated projects/ trees must not all enter the open path."""
    from harness.cli_job_merge import _foreign_state_dir_candidates
    import os
    import time as _time

    fresh = tmp_path / "fresh"
    stale = tmp_path / "stale"
    fresh.mkdir()
    stale.mkdir()
    (fresh / "state.sqlite3").write_bytes(b"")
    (stale / "state.sqlite3").write_bytes(b"")
    old = _time.time() - (60 * 3600)
    os.utime(stale / "state.sqlite3", (old, old))

    monkeypatch.setattr(
        "puppetmaster.state.list_project_state_dirs",
        lambda: [fresh, stale],
    )
    got = _foreign_state_dir_candidates("", max_opens=8, max_age_s=48 * 3600)
    assert any(str(fresh.resolve()) == p or str(fresh) in p for p in got)
    assert not any("stale" in p for p in got)


def test_scratch_identity_prefix_and_marker(tmp_path):
    prefixed = tmp_path / "pmh-edit-abc123"
    prefixed.mkdir()
    assert is_marionette_host_scratch_dir(prefixed)
    cursor = tmp_path / "pmh-cursor-edit-xyz"
    cursor.mkdir()
    assert is_marionette_host_scratch_dir(cursor)
    hashed = tmp_path / "workspace-deadbeef"
    hashed.mkdir()
    assert not is_marionette_host_scratch_dir(hashed)
    mark_marionette_host_scratch(hashed)
    assert is_marionette_host_scratch_dir(hashed)
    assert is_marionette_host_scratch_dir("") is False
    assert is_marionette_host_scratch_dir(None) is False


def test_resolve_cli_state_dir_ignores_host_scratch_env(tmp_path, monkeypatch):
    import subprocess

    from puppetmaster.state import default_state_dir

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    monkeypatch.setattr(
        "puppetmaster.state.app_state_root",
        lambda: tmp_path / "pm-root",
    )
    ws = default_state_dir(repo)
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "state.sqlite3").write_bytes(b"x")

    scratch = tmp_path / "pmh-edit-hijack"
    scratch.mkdir()
    (scratch / "state.sqlite3").write_bytes(b"x")
    mark_marionette_host_scratch(scratch)
    monkeypatch.setenv("PUPPETMASTER_STATE_DIR", str(scratch))

    got = resolve_cli_state_dir(str(repo))
    assert got is not None
    assert Path(got).resolve() == ws.resolve()
    assert Path(os.environ["PUPPETMASTER_STATE_DIR"]).resolve() == scratch.resolve()


def test_resolve_cli_state_dir_honors_non_scratch_env(tmp_path, monkeypatch):
    override = tmp_path / "operator-store"
    override.mkdir()
    (override / "state.sqlite3").write_bytes(b"x")
    monkeypatch.setenv("PUPPETMASTER_STATE_DIR", str(override))
    got = resolve_cli_state_dir(str(tmp_path / "unused"))
    assert Path(got).resolve() == override.resolve()


def test_foreign_candidates_skip_host_scratch(tmp_path, monkeypatch):
    from harness.cli_job_merge import _foreign_state_dir_candidates

    scratch = tmp_path / "pmh-edit-foreign"
    durable = tmp_path / "durable-ok"
    scratch.mkdir()
    durable.mkdir()
    (scratch / "state.sqlite3").write_bytes(b"")
    (durable / "state.sqlite3").write_bytes(b"")
    mark_marionette_host_scratch(scratch)
    monkeypatch.setattr(
        "puppetmaster.state.list_project_state_dirs",
        lambda: [scratch, durable],
    )
    got = _foreign_state_dir_candidates("", max_opens=8, max_age_s=48 * 3600)
    assert not any("pmh-edit" in p for p in got)
    assert any("durable-ok" in p for p in got)


def test_open_cli_durable_at_skips_scratch(tmp_path):
    scratch = tmp_path / "pmh-edit-open"
    scratch.mkdir()
    (scratch / "state.sqlite3").write_bytes(b"x")
    mark_marionette_host_scratch(scratch)
    assert open_cli_durable_at(str(scratch)) is None


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


def test_api_swarm_live_excludes_unstamped_cli_jobs(tmp_path, monkeypatch):
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
        srv._cfg.repo = str(repo)

        scoped = urllib.parse.quote(str(repo), safe="")
        data = json.loads(
            _api_get(port, f"/api/swarm/live?repo={scoped}", srv._TOKEN).read().decode()
        )
        cli_rows = [j for j in data["jobs"] if j.get("id") == cli_job_id]
        assert cli_rows == []
    finally:
        httpd.shutdown()


def _save_unstamped_external_task(store, job_id: str, cwd: str):
    """Plain cwd-only task — external Cursor MCP with no Marionette stamp."""
    task = Task(
        job_id=job_id,
        role="implement",
        instruction="do work",
        adapter="agentic",
        payload={"cwd": cwd},
    )
    store.save_task(task)
    return task


def _seed_unstamped_cli_store(tmp_path, repo_root: str, goal: str = "external cli"):
    cli_dir = tmp_path / "cli-external"
    store = create_store("sqlite", str(cli_dir))
    job = store.create_job(goal)
    _save_unstamped_external_task(store, job.id, repo_root)
    store.save_artifact(_verification(job.id, "t-ext", "worker-model", 50_000, 10_000))
    return store, str(cli_dir), job.id


def test_scoped_jobs_snapshot_unstamped_external_cli_absent(tmp_path, monkeypatch):
    """External Cursor MCP row (cwd only) must not appear on Marionette surfaces."""
    import harness.server as srv

    repo = tmp_path / "repo"
    repo.mkdir()
    harness_dir = tmp_path / "harness-state"
    harness_store = create_store("sqlite", str(harness_dir))
    _cli_store, cli_dir, cli_job_id = _seed_unstamped_cli_store(tmp_path, str(repo))

    monkeypatch.setenv("HARNESS_APP_RUN_ID", "run-test")
    monkeypatch.setenv("HARNESS_CLI_COST_MERGE", "1")
    monkeypatch.setattr(srv, "_jobs_snapshot", lambda: [])
    monkeypatch.setattr(srv._session, "state", lambda: SimpleNamespace(store=harness_store))
    monkeypatch.setattr(
        "harness.cli_job_merge.resolve_cli_state_dir",
        lambda workspace_root="": str(cli_dir),
    )
    monkeypatch.setattr(
        "harness.cli_job_merge.merge_running_cli_jobs_all_projects",
        lambda **kwargs: [],
    )
    srv._cfg.repo = str(repo)
    srv._sessions._active = "sess-live"
    srv._pilot.harness_session_id = "sess-live"

    rows = _scoped_jobs_snapshot(repo_root=str(repo))
    cli_rows = [r for r in rows if r.get("id") == cli_job_id]
    assert cli_rows == []


def test_scoped_jobs_snapshot_task_only_marionette_stamp_owned(tmp_path, monkeypatch):
    """Task payload stamps alone can own CLI economics when merge opt-in is on."""
    import harness.server as srv

    repo = tmp_path / "repo"
    repo.mkdir()
    harness_dir = tmp_path / "harness-state"
    harness_store = create_store("sqlite", str(harness_dir))
    cli_dir = tmp_path / "cli-task-stamp"
    cli_store = create_store("sqlite", str(cli_dir))
    job = cli_store.create_job("stamped via task")
    payload = stamp_task_payload(
        {"cwd": str(repo)},
        session_id="sess-live",
        origin="marionette",
        app_run_id="run-test",
    )
    cli_store.save_task(Task(
        job_id=job.id,
        role="implement",
        instruction="do work",
        adapter="agentic",
        payload=payload,
    ))

    monkeypatch.setenv("HARNESS_APP_RUN_ID", "run-test")
    monkeypatch.setenv("HARNESS_CLI_COST_MERGE", "1")
    monkeypatch.setattr(srv, "_jobs_snapshot", lambda: [])
    monkeypatch.setattr(srv._session, "state", lambda: SimpleNamespace(store=harness_store))
    monkeypatch.setattr(
        "harness.cli_job_merge.resolve_cli_state_dir",
        lambda workspace_root="": str(cli_dir),
    )
    monkeypatch.setattr(
        "harness.cli_job_merge.merge_running_cli_jobs_all_projects",
        lambda **kwargs: [],
    )
    srv._cfg.repo = str(repo)
    srv._sessions._active = "sess-live"
    srv._pilot.harness_session_id = "sess-live"

    rows = _scoped_jobs_snapshot(repo_root=str(repo))
    cli_rows = [r for r in rows if r.get("id") == job.id]
    assert len(cli_rows) == 1
    assert cli_rows[0]["accounting_owned"] is True
    assert cli_rows[0]["session_id"] == "sess-live"


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
    monkeypatch.setattr(
        "harness.cli_job_merge.merge_running_cli_jobs_all_projects",
        lambda **kwargs: [],
    )
    srv._cfg.repo = str(repo)
    monkeypatch.setattr(srv._sessions, "_active", "")
    srv._pilot.harness_session_id = ""

    rows = _scoped_jobs_snapshot(repo_root=str(repo))
    assert [r["id"] for r in rows if r.get("id") == cli_job_id] == []


def test_scoped_jobs_snapshot_keeps_marionette_cli_without_app_run_id(tmp_path, monkeypatch):
    """Visibility uses origin+session, not the process app_run_id."""
    import harness.server as srv

    repo = tmp_path / "repo"
    repo.mkdir()
    harness_dir = tmp_path / "harness-state"
    harness_store = create_store("sqlite", str(harness_dir))
    cli_dir = tmp_path / "cli-owned"
    cli_store = create_store("sqlite", str(cli_dir))
    job = cli_store.create_job("owned cli", label=job_label_for_session("sess-live", app_run_id="run-old"))
    payload = stamp_task_payload(
        {"cwd": str(repo)},
        session_id="sess-live",
        origin="marionette",
        app_run_id="run-old",
    )
    cli_store.save_task(Task(
        job_id=job.id,
        role="implement",
        instruction="do work",
        adapter="agentic",
        payload=payload,
    ))

    monkeypatch.setenv("HARNESS_APP_RUN_ID", "run-after-restart")
    monkeypatch.setattr(srv, "_jobs_snapshot", lambda: [])
    monkeypatch.setattr(srv._session, "state", lambda: SimpleNamespace(store=harness_store))
    monkeypatch.setattr(
        "harness.cli_job_merge.resolve_cli_state_dir",
        lambda workspace_root="": str(cli_dir),
    )
    monkeypatch.setattr(
        "harness.cli_job_merge.merge_running_cli_jobs_all_projects",
        lambda **kwargs: [],
    )
    srv._cfg.repo = str(repo)
    srv._sessions._active = "sess-live"
    srv._pilot.harness_session_id = "sess-live"

    rows = _scoped_jobs_snapshot(repo_root=str(repo))
    assert [r["id"] for r in rows] == [job.id]


def test_scoped_jobs_snapshot_keeps_registered_unstamped_harness_job(tmp_path, monkeypatch):
    import harness.server as srv

    repo = tmp_path / "repo"
    repo.mkdir()
    harness_dir = tmp_path / "harness-state"
    harness_store = create_store("sqlite", str(harness_dir))
    job = harness_store.create_job("legacy heal")
    _save_task(harness_store, job.id, str(repo))

    monkeypatch.setattr(
        srv,
        "_jobs_snapshot",
        lambda: [{"id": job.id, "goal": "legacy heal", "status": "running", "adapter": "agentic"}],
    )
    monkeypatch.setattr(srv._session, "state", lambda: SimpleNamespace(store=harness_store))
    monkeypatch.setattr(
        "harness.cli_job_merge.merge_running_cli_jobs_all_projects",
        lambda **kwargs: [],
    )
    monkeypatch.setattr(srv._pilot, "_session_job_ids", [job.id], raising=False)
    srv._cfg.repo = str(repo)
    srv._sessions._active = "sess-live"
    srv._pilot.harness_session_id = "sess-live"

    rows = _scoped_jobs_snapshot(repo_root=str(repo))
    assert [r["id"] for r in rows] == [job.id]
    assert rows[0]["session_id"] == "sess-live"
