"""Session-scoped job visibility, per-session meters, and model-badge fallback."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

from harness.job_scoping import (
    cwd_under_repo,
    filter_local_jobs,
    filter_store_jobs,
    inspect_store_job_ownership,
    job_label_for_session,
    job_owned_by_marionette,
    job_repo_cwd,
    job_visible_for_view,
    parse_job_session_id,
    resolve_job_model,
    stamp_task_payload,
)
from harness.server import _job_swarm_accounting
from harness.sessions import SessionStore
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


def _routing(task_id: str, model_id: str, cost: float = 0.05):
    return Artifact(
        job_id="job-1",
        task_id=task_id,
        type=ArtifactType.ROUTING,
        created_by="router",
        payload={"model_id": model_id, "estimated_cost_usd": cost},
        confidence=0.9,
        evidence=[],
    )


def _verification(task_id: str, model: str, tin: int, tout: int):
    return Artifact(
        job_id="job-1",
        task_id=task_id,
        type=ArtifactType.VERIFICATION,
        created_by="worker",
        payload={"model": model, "tokens_in": tin, "tokens_out": tout},
        confidence=0.9,
        evidence=[],
    )


def test_job_label_roundtrip():
    label = job_label_for_session("sess-a")
    data = json.loads(label)
    assert data["session_id"] == "sess-a"
    assert data["origin"] == "marionette"
    assert parse_job_session_id(label, []) == "sess-a"


def test_job_label_roundtrips_dispatch_identity():
    from harness.job_scoping import parse_job_dispatch_id

    label = job_label_for_session("sess-a", dispatch_id="call_abc")

    assert json.loads(label)["dispatch_id"] == "call_abc"
    assert parse_job_dispatch_id(label) == "call_abc"


def test_legacy_job_not_visible_via_cwd_match():
    tasks = [SimpleNamespace(payload={"cwd": "/work/a/project"})]
    assert not job_visible_for_view(
        session_id="",
        label=None,
        tasks=tasks,
        active_session_id="sess-b",
        repo_root="/work/a",
    )
    assert not job_visible_for_view(
        session_id="",
        label=None,
        tasks=tasks,
        active_session_id="sess-b",
        repo_root="/work/b",
    )


def test_stamped_marionette_job_visible_across_sessions():
    label = job_label_for_session("sess-a")
    tasks = [SimpleNamespace(payload={"cwd": "/work/a/project", "session_id": "sess-a"})]
    assert job_visible_for_view(
        session_id="sess-a",
        label=label,
        tasks=tasks,
        active_session_id="sess-a",
        repo_root="/work/a",
    )
    # Ownership is origin+session stamp, not the active chat. Frontend session
    # scope hides other chats; repo/all still list this owned row.
    assert job_visible_for_view(
        session_id="sess-a",
        label=label,
        tasks=tasks,
        active_session_id="sess-b",
        repo_root="/work/a",
        status="complete",
    )


def test_running_stamped_job_visible_under_repo_across_sessions():
    """Owned Marionette work stays listed after a session switch."""
    label = job_label_for_session("sess-a")
    tasks = [SimpleNamespace(payload={"cwd": "/work/a/project", "session_id": "sess-a"})]
    assert job_visible_for_view(
        session_id="sess-a",
        label=label,
        tasks=tasks,
        active_session_id="sess-b",
        repo_root="/work/a",
        status="running",
    )


def test_owned_job_visible_without_cwd_or_active_session_match():
    label = job_label_for_session("sess-a")
    tasks = [SimpleNamespace(payload={"cwd": "/somewhere/else", "session_id": "sess-a"})]
    assert job_visible_for_view(
        session_id="sess-a",
        label=label,
        tasks=tasks,
        active_session_id="sess-b",
        repo_root="/work/b",
        status="running",
    )
    assert job_visible_for_view(
        session_id="sess-a",
        label=label,
        tasks=tasks,
        active_session_id="sess-b",
        repo_root="/work/b",
        status="complete",
    )


def test_running_job_visible_when_session_matches():
    label = job_label_for_session("sess-a")
    tasks = [SimpleNamespace(payload={"cwd": "/somewhere/else", "session_id": "sess-a"})]
    assert job_visible_for_view(
        session_id="sess-a",
        label=label,
        tasks=tasks,
        active_session_id="sess-a",
        repo_root="/work/b",
        status="running",
    )


def test_running_job_not_visible_when_legacy_cwd_matches():
    tasks = [SimpleNamespace(payload={"cwd": "/work/a/project"})]
    assert not job_visible_for_view(
        session_id="",
        label=None,
        tasks=tasks,
        active_session_id="sess-b",
        repo_root="/work/a",
        status="running",
    )


def test_running_orphan_not_visible_without_session_or_cwd():
    assert not job_visible_for_view(
        session_id="",
        label=None,
        tasks=[],
        active_session_id="sess-b",
        repo_root="/work/b",
        status="running",
    )


def test_registered_job_id_visible_without_stamps():
    assert job_visible_for_view(
        session_id="",
        label=None,
        tasks=[],
        active_session_id="sess-b",
        repo_root="/work/b",
        status="complete",
        job_id="job_orphan",
        registered_job_ids=["job_orphan"],
    )
    assert job_owned_by_marionette(job_id="local-heal", registered_job_ids=["local-heal"])


def test_app_run_id_is_not_a_visibility_key(monkeypatch):
    monkeypatch.setenv("HARNESS_APP_RUN_ID", "run-after-restart")
    label = job_label_for_session("sess-a", app_run_id="run-before-restart")
    assert job_visible_for_view(
        session_id="sess-a",
        label=label,
        tasks=[],
        active_session_id="sess-a",
        repo_root="/work/a",
        job_id="job_stamped",
    )
    unstamped = json.dumps({"app_run_id": "run-after-restart"})
    assert not job_visible_for_view(
        session_id="",
        label=unstamped,
        tasks=[],
        active_session_id="sess-a",
        repo_root="/work/a",
        job_id="job_run_only",
    )


def test_origin_without_session_is_not_owned():
    label = json.dumps({"origin": "marionette"})
    assert not job_owned_by_marionette(label=label, job_id="job_origin_only")


def test_pre_origin_session_owned_on_harness_not_cli():
    label = json.dumps({"session_id": "sess-a"})
    assert job_owned_by_marionette(label=label, session_id="sess-a", job_id="job_sess_only")
    assert job_owned_by_marionette(
        label=label, session_id="sess-a", job_id="job_sess_only", source="harness",
    )
    assert not job_owned_by_marionette(
        label=label, session_id="sess-a", job_id="job_sess_only", source="cli",
    )
    assert job_owned_by_marionette(session_id="sess-a", job_id="local-persisted")
    assert job_owned_by_marionette(
        session_id="sess-a",
        job_id="job_sess_only",
        registered_job_ids=["job_sess_only"],
    )
    assert not job_owned_by_marionette(
        session_id="sess-a",
        job_id="job_sess_only",
        source="cli",
        registered_job_ids=["job_sess_only"],
        allow_registered_heal=True,
    )


def test_running_local_job_owned_by_session_stamp():
    rows = [
        {"id": "local-1", "status": "running", "session_id": "sess-a", "cwd": "/elsewhere"},
        {"id": "local-2", "status": "complete", "session_id": "sess-a", "cwd": "/elsewhere"},
    ]
    visible = filter_local_jobs(rows, active_session_id="sess-b", repo_root="/work/b")
    assert [j["id"] for j in visible] == ["local-1", "local-2"]


def test_session_stamped_local_jobs_remain_owned_across_sessions():
    rows = [
        {"id": "local-1", "status": "running", "session_id": "sess-a", "cwd": "/work/b/sub"},
        {"id": "local-2", "status": "completed", "session_id": "sess-a", "cwd": "/work/b/sub"},
    ]
    visible = filter_local_jobs(rows, active_session_id="sess-b", repo_root="/work/b")
    assert [j["id"] for j in visible] == ["local-1", "local-2"]


def test_running_local_job_visible_when_session_matches():
    rows = [
        {"id": "local-1", "status": "running", "session_id": "sess-a", "cwd": "/elsewhere"},
    ]
    visible = filter_local_jobs(rows, active_session_id="sess-a", repo_root="/work/b")
    assert [j["id"] for j in visible] == ["local-1"]


def test_running_local_job_not_visible_when_legacy_cwd_matches():
    rows = [
        {"id": "local-1", "status": "running", "cwd": "/work/a/sub"},
    ]
    visible = filter_local_jobs(rows, active_session_id="sess-b", repo_root="/work/a")
    assert [j["id"] for j in visible] == []


def test_running_local_orphan_not_visible_without_session_or_cwd():
    rows = [
        {"id": "local-orphan", "status": "running"},
    ]
    visible = filter_local_jobs(rows, active_session_id="sess-b", repo_root="/work/b")
    assert [j["id"] for j in visible] == []


def test_registered_local_job_visible_without_session_stamp():
    rows = [
        {"id": "local-heal", "status": "running"},
    ]
    visible = filter_local_jobs(
        rows,
        active_session_id="sess-b",
        repo_root="/work/b",
        registered_job_ids=["local-heal"],
    )
    assert [j["id"] for j in visible] == ["local-heal"]
    assert visible[0]["session_id"] == "sess-b"


def test_registered_heal_without_active_session_omits_sessionless_row(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    store = create_store("sqlite", str(tmp_path / "state"))
    job = store.create_job("legacy heal")
    _save_task(store, job.id, str(repo))
    rows = [{"id": job.id, "goal": "legacy heal", "status": "running"}]
    visible = filter_store_jobs(
        rows,
        store,
        active_session_id="",
        repo_root=str(repo),
        registered_job_ids=[job.id],
    )
    assert visible == []
    locals_visible = filter_local_jobs(
        [{"id": "local-heal", "status": "running"}],
        active_session_id="",
        repo_root=str(repo),
        registered_job_ids=["local-heal"],
    )
    assert locals_visible == []


def test_inspect_store_job_ownership_owned_unowned_and_absent(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    store = create_store("sqlite", str(tmp_path / "state"))
    owned = store.create_job("owned", label=job_label_for_session("sess-a"))
    _save_task(store, owned.id, str(repo), session_id="sess-a")
    leftover = store.create_job("leftover")
    _save_task(store, leftover.id, str(repo))
    assert inspect_store_job_ownership(store, owned.id, source="harness") is True
    assert inspect_store_job_ownership(store, leftover.id, source="harness") is False
    assert inspect_store_job_ownership(store, "missing", source="harness") is None
    assert inspect_store_job_ownership(
        store,
        leftover.id,
        source="cli",
        registered_job_ids=[leftover.id],
        allow_registered_heal=True,
    ) is False
    assert inspect_store_job_ownership(
        store,
        leftover.id,
        source="harness",
        registered_job_ids=[leftover.id],
        allow_registered_heal=True,
    ) is True


def test_filter_store_jobs_keeps_owned_drops_legacy(tmp_path):
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    repo_a.mkdir()
    repo_b.mkdir()
    store = create_store("sqlite", str(tmp_path / "state"))

    job_a = store.create_job("goal a", label=job_label_for_session("sess-a"))
    _save_task(store, job_a.id, str(repo_a), session_id="sess-a")

    job_b = store.create_job("goal b", label=job_label_for_session("sess-b"))
    _save_task(store, job_b.id, str(repo_b), session_id="sess-b")

    legacy = store.create_job("legacy")
    _save_task(store, legacy.id, str(repo_a))

    rows = [
        {"id": job_a.id, "goal": "goal a", "status": "complete", "adapter": "agentic"},
        {"id": job_b.id, "goal": "goal b", "status": "complete", "adapter": "agentic"},
        {"id": legacy.id, "goal": "legacy", "status": "complete", "adapter": "agentic"},
    ]

    scoped_a = filter_store_jobs(rows, store, active_session_id="sess-a", repo_root=str(repo_a))
    ids_a = {j["id"] for j in scoped_a}
    assert job_a.id in ids_a
    assert job_b.id in ids_a
    assert legacy.id not in ids_a

    scoped_b = filter_store_jobs(rows, store, active_session_id="sess-b", repo_root=str(repo_b))
    ids_b = {j["id"] for j in scoped_b}
    assert job_b.id in ids_b
    assert job_a.id in ids_b
    assert legacy.id not in ids_b


def test_filter_store_jobs_keeps_pre_origin_harness_session(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    store = create_store("sqlite", str(tmp_path / "state"))
    job = store.create_job("legacy sess", label=json.dumps({"session_id": "sess-a"}))
    task = Task(
        job_id=job.id,
        role="implement",
        instruction="do work",
        adapter="agentic",
        payload={"cwd": str(repo), "session_id": "sess-a"},
    )
    store.save_task(task)
    rows = [{"id": job.id, "goal": "legacy sess", "status": "complete"}]
    visible = filter_store_jobs(rows, store, active_session_id="sess-b", repo_root=str(repo))
    assert [j["id"] for j in visible] == [job.id]
    assert visible[0]["session_id"] == "sess-a"


def test_filter_local_jobs_keeps_stamped_drops_legacy_cwd():
    local_a = {
        "id": "local-aaa",
        "session_id": "sess-a",
        "goal": "edit",
        "cwd": "/work/a",
    }
    local_b = {
        "id": "local-bbb",
        "session_id": "sess-b",
        "goal": "edit",
        "cwd": "/work/b",
    }
    legacy = {
        "id": "local-leg",
        "goal": "legacy edit",
        "cwd": "/work/a",
    }
    visible = filter_local_jobs(
        [local_a, local_b, legacy],
        active_session_id="sess-a",
        repo_root="/work/a",
    )
    ids = {j["id"] for j in visible}
    assert "local-aaa" in ids
    assert "local-bbb" in ids
    assert "local-leg" not in ids


def test_session_meta_meter_accumulation(tmp_path):
    path = tmp_path / "harness_sessions.json"
    store = SessionStore(str(path))
    created = store.create(title="Meter test", repo="/repo", branch="main")
    sid = created["id"]

    store.accumulate_meters(sid, input_tokens=100, output_tokens=40, cache_read_tokens=10, estimated_cost_usd=0.25)
    store.accumulate_meters(sid, input_tokens=50, output_tokens=10, estimated_cost_usd=0.05)

    listed = store.list()
    row = next(s for s in listed if s["id"] == sid)
    assert row["input_tokens"] == 150
    assert row["output_tokens"] == 50
    assert row["cache_read_tokens"] == 10
    assert abs(row["estimated_cost_usd"] - 0.30) < 1e-9


def test_resolve_job_model_prefers_routing_then_task_then_adapter():
    arts = [_routing("t1", "router-model")]
    tasks = [SimpleNamespace(payload={"cwd": "/repo", "model": "task-model"})]
    assert resolve_job_model(arts, tasks, "agentic") == "router-model"

    assert resolve_job_model([], tasks, "agentic") == "task-model"
    assert resolve_job_model([], [], "agentic") == "agentic"


def test_resolve_job_model_prefers_fallback_over_initial_router():
    """Failed first pick must not badge the job; final fallback wins."""
    arts = [
        Artifact(
            job_id="job-1",
            task_id="t1",
            type=ArtifactType.ROUTING,
            created_by="router",
            payload={"model_id": "cursor/gpt-5-4", "estimated_cost_usd": 0.0},
            confidence=0.9,
            evidence=[],
        ),
        Artifact(
            job_id="job-1",
            task_id="t1",
            type=ArtifactType.ROUTING,
            created_by="router-fallback",
            payload={"model_id": "agentic/z-ai/glm-5.2", "estimated_cost_usd": 0.0048},
            confidence=0.9,
            evidence=[],
        ),
    ]
    assert resolve_job_model(arts, [], "cursor") == "agentic/z-ai/glm-5.2"


def test_job_swarm_accounting_uses_verification_when_no_routing():
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
    arts = [_verification("t1", "worker-model", 100_000, 20_000)]
    tokens, cost = _job_swarm_accounting(arts, registry)
    assert tokens == 120_000
    assert abs(cost - 0.14) < 1e-6


def test_cwd_under_repo_longest_prefix():
    assert cwd_under_repo("/work/a/sub", "/work/a")
    assert not cwd_under_repo("/work/b", "/work/a")
    assert job_repo_cwd([
        SimpleNamespace(payload={"cwd": "/work/a"}),
        SimpleNamespace(payload={"cwd": "/work/a/deep/nested"}),
    ]) == os.path.normcase(os.path.abspath("/work/a/deep/nested"))
    assert job_repo_cwd([
        {"payload": {"cwd": "/work/a"}},
        {"payload": {"cwd": "/work/a/deep/nested"}},
    ]) == os.path.normcase(os.path.abspath("/work/a/deep/nested"))


def _alias_or_symlink_root(tmp_path):
    repo = tmp_path / "marionette"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    alias = tmp_path / "Marionette"
    if not alias.exists():
        alias.symlink_to(repo, target_is_directory=True)
    return repo, alias


def test_cwd_under_repo_nested_alias_under_canonical(tmp_path):
    repo, alias = _alias_or_symlink_root(tmp_path)
    nested = alias / "nested" / "child"
    nested.mkdir(parents=True)
    missing = alias / "nested" / "historical-gone"
    other = tmp_path / "other-root" / "child"
    other.mkdir(parents=True)

    assert cwd_under_repo(str(nested), str(repo))
    assert cwd_under_repo(str(missing), str(repo))
    assert not cwd_under_repo(str(other), str(repo))

    nested_tasks = [SimpleNamespace(payload={"cwd": str(nested)})]
    assert not job_visible_for_view(
        session_id="",
        label=None,
        tasks=nested_tasks,
        active_session_id="sess-b",
        repo_root=str(repo),
    )
    missing_tasks = [SimpleNamespace(payload={"cwd": str(missing)})]
    assert not job_visible_for_view(
        session_id="",
        label=None,
        tasks=missing_tasks,
        active_session_id="sess-b",
        repo_root=str(repo),
    )
    other_tasks = [SimpleNamespace(payload={"cwd": str(other)})]
    assert not job_visible_for_view(
        session_id="",
        label=None,
        tasks=other_tasks,
        active_session_id="sess-b",
        repo_root=str(repo),
    )


def test_jobs_api_lists_stamped_alias_job_not_cwd_only(tmp_path, monkeypatch):
    repo, alias = _alias_or_symlink_root(tmp_path)
    store = create_store("sqlite", str(tmp_path / "state"))
    created = store.create_job("aliased job", label=job_label_for_session("sess-a"))
    _save_task(store, created.id, str(alias), session_id="sess-a")
    legacy = store.create_job("legacy alias")
    _save_task(store, legacy.id, str(alias))
    httpd, port, srv = _api_server(str(tmp_path / "state"))

    try:
        monkeypatch.setattr(srv, "_jobs_snapshot", lambda: [
            {"id": created.id, "goal": "aliased job", "status": "complete", "adapter": "agentic"},
            {"id": legacy.id, "goal": "legacy alias", "status": "complete", "adapter": "agentic"},
        ])
        monkeypatch.setattr(srv._session, "state", lambda: SimpleNamespace(store=store))
        srv._cfg.repo = str(repo)

        jobs = json.loads(_api_get(port, "/api/jobs", srv._TOKEN).read().decode())

        assert {job["id"] for job in jobs} == {created.id}
    finally:
        httpd.shutdown()


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


def _seed_two_repo_jobs(tmp_path):
    """Marionette-stamped jobs under two repo roots plus an unstamped leftover."""
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    repo_a.mkdir()
    repo_b.mkdir()
    store = create_store("sqlite", str(tmp_path / "state"))

    job_a = store.create_job("job in repo a", label=job_label_for_session("sess-a"))
    _save_task(store, job_a.id, str(repo_a), session_id="sess-a")

    job_b = store.create_job("job in repo b", label=job_label_for_session("sess-b"))
    _save_task(store, job_b.id, str(repo_b), session_id="sess-b")

    legacy = store.create_job("legacy leftover")
    _save_task(store, legacy.id, str(repo_a))

    return store, str(repo_a), str(repo_b), job_a.id, job_b.id, legacy.id


def test_api_jobs_lists_owned_jobs_not_cwd_or_repo_query(tmp_path, monkeypatch):
    """Owned Marionette jobs stay listed; cwd/?repo= cannot admit unstamped rows."""
    store, repo_a, repo_b, job_a_id, job_b_id, legacy_id = _seed_two_repo_jobs(tmp_path)
    tmp_dir = tempfile.mkdtemp()
    try:
        httpd, port, srv = _api_server(str(tmp_path / "state"))
        try:
            monkeypatch.setattr(srv, "_jobs_snapshot", lambda: [
                {"id": job_a_id, "goal": "job in repo a", "status": "complete", "adapter": "agentic"},
                {"id": job_b_id, "goal": "job in repo b", "status": "complete", "adapter": "agentic"},
                {"id": legacy_id, "goal": "legacy leftover", "status": "complete", "adapter": "agentic"},
            ])
            monkeypatch.setattr(srv._session, "state", lambda: SimpleNamespace(store=store))
            srv._cfg.repo = repo_b

            headers_token = srv._TOKEN
            default_ids = {j["id"] for j in json.loads(
                _api_get(port, "/api/jobs", headers_token).read().decode()
            )}
            assert job_a_id in default_ids
            assert job_b_id in default_ids
            assert legacy_id not in default_ids

            scoped_a = urllib.parse.quote(repo_a, safe="")
            override_ids = {j["id"] for j in json.loads(
                _api_get(port, f"/api/jobs?repo={scoped_a}", headers_token).read().decode()
            )}
            assert job_a_id in override_ids
            assert job_b_id in override_ids
            assert legacy_id not in override_ids
        finally:
            httpd.shutdown()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_api_jobs_missing_repo_param_still_drops_unstamped(tmp_path, monkeypatch):
    store, repo_a, repo_b, job_a_id, job_b_id, legacy_id = _seed_two_repo_jobs(tmp_path)
    try:
        httpd, port, srv = _api_server(str(tmp_path / "state"))
        try:
            monkeypatch.setattr(srv, "_jobs_snapshot", lambda: [
                {"id": job_a_id, "goal": "job in repo a", "status": "complete", "adapter": "agentic"},
                {"id": job_b_id, "goal": "job in repo b", "status": "complete", "adapter": "agentic"},
                {"id": legacy_id, "goal": "legacy leftover", "status": "complete", "adapter": "agentic"},
            ])
            monkeypatch.setattr(srv._session, "state", lambda: SimpleNamespace(store=store))
            srv._cfg.repo = repo_a

            ids = {j["id"] for j in json.loads(
                _api_get(port, "/api/jobs", srv._TOKEN).read().decode()
            )}
            assert job_a_id in ids
            assert job_b_id in ids
            assert legacy_id not in ids
        finally:
            httpd.shutdown()
    finally:
        pass


def test_api_swarm_live_lists_owned_jobs_not_cwd_scope(tmp_path, monkeypatch):
    store, repo_a, repo_b, job_a_id, job_b_id, legacy_id = _seed_two_repo_jobs(tmp_path)
    try:
        httpd, port, srv = _api_server(str(tmp_path / "state"))
        try:
            monkeypatch.setattr(srv, "_jobs_snapshot", lambda: [
                {"id": job_a_id, "goal": "job in repo a", "status": "complete", "adapter": "agentic"},
                {"id": job_b_id, "goal": "job in repo b", "status": "complete", "adapter": "agentic"},
                {"id": legacy_id, "goal": "legacy leftover", "status": "complete", "adapter": "agentic"},
            ])
            monkeypatch.setattr(srv._session, "state", lambda: SimpleNamespace(
                store=store,
                format_artifacts=lambda arts: [],
                job_artifacts=lambda jid: [],
            ))
            monkeypatch.setattr(srv, "_swarm_registry", lambda: [])
            monkeypatch.setattr(srv, "_job_swarm_accounting", lambda arts, registry: (0, 0.0))
            monkeypatch.setattr(srv, "_job_savings_fields", lambda jid: {})
            monkeypatch.setattr(srv._pilot, "live_local_jobs", lambda: [])
            srv._cfg.repo = repo_b

            scoped_a = urllib.parse.quote(repo_a, safe="")
            data = json.loads(
                _api_get(port, f"/api/swarm/live?repo={scoped_a}", srv._TOKEN).read().decode()
            )
            ids = {j["id"] for j in data["jobs"]}
            assert job_a_id in ids
            assert job_b_id in ids
            assert legacy_id not in ids
        finally:
            httpd.shutdown()
    finally:
        pass


def test_api_jobs_and_swarm_live_keep_registered_legacy_job(tmp_path, monkeypatch):
    store, repo_a, _repo_b, _job_a_id, _job_b_id, legacy_id = _seed_two_repo_jobs(tmp_path)
    httpd, port, srv = _api_server(str(tmp_path / "state"))
    try:
        monkeypatch.setattr(srv, "_jobs_snapshot", lambda: [
            {"id": legacy_id, "goal": "legacy leftover", "status": "running", "adapter": "agentic"},
        ])
        monkeypatch.setattr(srv._session, "state", lambda: SimpleNamespace(
            store=store,
            format_artifacts=lambda arts: [],
            job_artifacts=lambda jid: [],
        ))
        monkeypatch.setattr(srv, "_swarm_registry", lambda: [])
        monkeypatch.setattr(srv, "_job_swarm_accounting", lambda arts, registry: (0, 0.0))
        monkeypatch.setattr(srv, "_job_savings_fields", lambda jid: {})
        monkeypatch.setattr(srv._pilot, "live_local_jobs", lambda: [])
        monkeypatch.setattr(srv._pilot, "_session_job_ids", [legacy_id], raising=False)
        srv._cfg.repo = repo_a
        srv._sessions._active = "sess-live"

        jobs = json.loads(_api_get(port, "/api/jobs", srv._TOKEN).read().decode())
        assert {j["id"] for j in jobs} == {legacy_id}
        assert all(j.get("session_id") == "sess-live" for j in jobs)

        live = json.loads(_api_get(port, "/api/swarm/live", srv._TOKEN).read().decode())
        assert {j["id"] for j in live["jobs"]} == {legacy_id}
        assert all(j.get("session_id") == "sess-live" for j in live["jobs"])
    finally:
        httpd.shutdown()
