"""Session-scoped job visibility, per-session meters, and model-badge fallback."""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

from harness.job_scoping import (
    cwd_under_repo,
    filter_local_jobs,
    filter_store_jobs,
    job_label_for_session,
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
    assert json.loads(label)["session_id"] == "sess-a"
    assert parse_job_session_id(label, []) == "sess-a"


def test_legacy_job_visible_only_in_matching_repo():
    tasks = [SimpleNamespace(payload={"cwd": "/work/a/project"})]
    assert job_visible_for_view(
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


def test_stamped_job_visible_only_for_matching_session():
    label = job_label_for_session("sess-a")
    tasks = [SimpleNamespace(payload={"cwd": "/work/a/project", "session_id": "sess-a"})]
    assert job_visible_for_view(
        session_id="sess-a",
        label=label,
        tasks=tasks,
        active_session_id="sess-a",
        repo_root="/work/a",
    )
    assert not job_visible_for_view(
        session_id="sess-a",
        label=label,
        tasks=tasks,
        active_session_id="sess-b",
        repo_root="/work/a",
    )


def test_filter_store_jobs_two_sessions_two_repos(tmp_path):
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
    assert legacy.id in ids_a
    assert job_b.id not in ids_a

    scoped_b = filter_store_jobs(rows, store, active_session_id="sess-b", repo_root=str(repo_b))
    ids_b = {j["id"] for j in scoped_b}
    assert job_b.id in ids_b
    assert job_a.id not in ids_b
    assert legacy.id not in ids_b


def test_filter_local_jobs_respects_session_and_legacy_repo():
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
    assert "local-leg" in ids
    assert "local-bbb" not in ids


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
