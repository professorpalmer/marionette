from __future__ import annotations

"""Durable local-job provenance: a sidecar row must not overclaim.

Round-trips ``_register_local_job`` -> ``_finish_local_job`` -> the read surfaces
(``/api/swarm/live`` projection, ``job://local-*``, ``artifact://local-*``) for
the cases where the sidecar used to lie: an analysis job stamped as a ``patch``,
an error-only job advertising completed artifacts, and artifact URIs that moved
when the headline was rewritten at finish time.
"""

import json
import os
import threading

import pytest

from harness.internal_uri import (
    InternalUriContext,
    InternalUriError,
    resolve_internal_uri,
)
from harness.job_scoping import job_label_for_session, stamp_task_payload
from harness.local_job_artifacts import (
    artifacts_are_complete,
    resolve_execution_provenance,
    terminal_artifact_type,
)
from harness.local_job_swarm_view import project_local_job_for_swarm_live
from harness.local_jobs import LocalJobsMixin
from puppetmaster.models import AgentRun, Artifact, ArtifactType, Task
from puppetmaster.store_factory import create_store


class _Session(LocalJobsMixin):
    """Minimal host for the mixin: real state, no ConversationalSession."""

    def __init__(self, state_dir, repo, session_id="sess-a"):
        self._local_jobs = {}
        self._local_jobs_lock = threading.RLock()
        self._local_job_cancels = {}
        self._local_jobs_path = os.path.join(state_dir, "swarm_local_jobs.json")
        self.harness_session_id = session_id
        self.config = type("Cfg", (), {"repo": repo, "driver": "stub-oracle-v2"})()


@pytest.fixture
def session(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    return _Session(str(tmp_path), str(repo)), str(tmp_path), str(repo)


def _ctx(state_dir, repo, session_id="sess-a"):
    return InternalUriContext(state_dir=state_dir, repo=repo, session_id=session_id)


def _artifact_types(job):
    """Artifact types excluding the preflight ROUTING card (covered separately)."""
    return [
        str(a.get("type") or "")
        for a in job["artifacts"]
        if str(a.get("type") or "").upper() != "ROUTING"
    ]


def test_read_only_analysis_success_is_analysis_never_patch(session):
    sess, state_dir, repo = session
    sess._register_local_job("local-abc123", "audit routing honesty", role="explore",
                             cwd=repo, engine="native")
    sess._finish_local_job(
        "local-abc123", ok=True,
        summary="Router savings basis is fabricated when alternatives are dropped",
        tokens=4200, engine="native", model="stub-oracle-v2",
    )

    job = sess._local_jobs["local-abc123"]
    assert _artifact_types(job) == ["analysis"]
    assert "patch" not in _artifact_types(job)

    row = project_local_job_for_swarm_live(job)
    assert row["artifacts_complete"] is True
    assert row["status"] == "completed"


def test_implement_success_with_files_is_a_patch(session):
    sess, state_dir, repo = session
    sess._register_local_job("local-patch1", "fix the guard", role="implement",
                             cwd=repo, engine="agentic")
    sess._finish_local_job(
        "local-patch1", ok=True, summary="Guard accepts user Applications bundles",
        files=["harness/browser.py"], engine="agentic", model="kimi-k3",
    )

    job = sess._local_jobs["local-patch1"]
    assert _artifact_types(job) == ["patch"]
    terminal = next(a for a in job["artifacts"] if a["type"] == "patch")
    assert "(1 file)" in terminal["headline"]
    assert project_local_job_for_swarm_live(job)["artifacts_complete"] is True


def test_implement_success_without_file_evidence_is_not_a_patch(session):
    sess, state_dir, repo = session
    sess._register_local_job("local-nofiles", "fix the guard", role="implement",
                             cwd=repo, engine="agentic")
    sess._finish_local_job("local-nofiles", ok=True, summary="nothing needed changing",
                           engine="agentic")

    assert _artifact_types(sess._local_jobs["local-nofiles"]) == ["analysis"]


def test_error_only_job_never_claims_completed_artifacts(session):
    sess, state_dir, repo = session
    sess._register_local_job("local-dead", "audit routing", role="explore",
                             cwd=repo, engine="agentic")
    sess._finish_local_job("local-dead", ok=False, summary="provider timed out",
                           status="failed", engine="agentic")

    job = sess._local_jobs["local-dead"]
    assert _artifact_types(job) == ["error"]
    assert project_local_job_for_swarm_live(job)["artifacts_complete"] is False

    payload = json.loads(
        resolve_internal_uri("job://local-dead", _ctx(state_dir, repo)).content,
    )
    assert payload["artifacts_complete"] is False


def test_routing_card_alone_never_claims_completed_artifacts():
    assert artifacts_are_complete([
        {"type": "ROUTING", "headline": "Routed to kimi-k3"},
    ]) is False
    assert artifacts_are_complete([
        {"type": "ROUTING", "headline": "Routed to kimi-k3"},
        {"type": "error", "headline": "Interrupted by backend restart"},
    ]) is False
    assert artifacts_are_complete([{"type": "placeholder", "headline": "pending"}]) is False
    assert artifacts_are_complete([{"type": "analysis", "headline": "real summary"}]) is True


def test_real_structured_findings_survive_to_artifact_reads(session):
    sess, state_dir, repo = session
    sess._register_local_job("local-findings", "audit keys", role="analysis",
                             cwd=repo, engine="agentic")
    sess._finish_local_job(
        "local-findings", ok=True, summary="2 findings",
        engine="agentic", model="kimi-k3", tokens=9000, est_cost_usd=0.42,
        findings=[
            {"type": "finding", "headline": "keys.py ignores the legacy keyfile"},
            {"type": "risk", "headline": "platform.json split-brain disables toggles"},
            {"type": "verification", "headline": "ran"},
            {"type": "error", "headline": "bookkeeping row must not be promoted"},
        ],
    )

    job = sess._local_jobs["local-findings"]
    assert _artifact_types(job) == ["analysis", "finding", "risk", "verification"]

    finding = next(a for a in job["artifacts"] if a["type"] == "finding")
    assert finding["execution_ref"] == {
        "job_id": "local-findings",
        "terminal_artifact_id": "local-findings-result",
    }
    assert "tokens" not in finding
    assert "est_cost_usd" not in finding

    ctx = _ctx(state_dir, repo)
    for artifact in job["artifacts"]:
        uri = f"artifact://local-findings/{artifact['id']}"  # explicit stable ids
        data = json.loads(resolve_internal_uri(uri, ctx).content)
        assert data["headline"] == artifact["headline"]
        assert data["job_id"] == "local-findings"

    finding_uri = f"artifact://local-findings/{finding['id']}"
    finding_data = json.loads(resolve_internal_uri(finding_uri, ctx).content)
    assert finding_data["execution_ref"]["job_id"] == "local-findings"
    # Resolved display provenance joins the terminal/job meters without
    # promoting spend onto the finding row itself.
    assert finding_data.get("tokens") in (None, 0)
    assert finding_data.get("est_cost_usd") in (None, 0, 0.0)
    assert finding_data["execution"]["tokens"] == 9000
    assert finding_data["execution"]["model"] == "agentic/kimi-k3"
    assert finding_data["execution"]["est_cost_usd"] == 0.42


def test_forged_routing_terminal_artifact_id_cannot_supply_spend(session):
    """A same-job ROUTING id must not hydrate execution model/tokens/cost."""
    sess, state_dir, repo = session
    sess._register_local_job(
        "local-forge", "audit keys", role="analysis", cwd=repo, engine="agentic",
    )
    # Seed a ROUTING card the way preview_agentic_route would, then poison it
    # with forged spend so a naive id join would lie about model/tokens/cost.
    with sess._local_jobs_lock:
        sess._local_jobs["local-forge"]["artifacts"] = [{
            "id": "local-forge-routing",
            "type": "ROUTING",
            "headline": "Routed to forged/evil-model",
            "model": "forged/evil-model",
            "tokens": 1,
            "est_cost_usd": 99.0,
            "adapter": "forged",
            "created_by": "router",
        }]
        sess._persist_local_jobs_locked()
    sess._finish_local_job(
        "local-forge", ok=True, summary="real finding summary",
        engine="agentic", model="kimi-k3", tokens=9000, est_cost_usd=0.42,
        findings=[{"type": "finding", "headline": "keys.py ignores legacy"}],
    )
    job = sess._local_jobs["local-forge"]
    # Re-poison after finish (finish may rewrite ROUTING.model to selected).
    with sess._local_jobs_lock:
        for art in job["artifacts"]:
            if str(art.get("type") or "").upper() == "ROUTING":
                art["model"] = "forged/evil-model"
                art["tokens"] = 1
                art["est_cost_usd"] = 99.0
                art["adapter"] = "forged"
                break
        sess._persist_local_jobs_locked()

    finding = next(a for a in job["artifacts"] if a["type"] == "finding")
    forged = dict(finding)
    forged["execution_ref"] = {
        "job_id": "local-forge",
        "terminal_artifact_id": "local-forge-routing",
    }

    resolved = resolve_execution_provenance(forged, job)
    assert resolved["model"] == "agentic/kimi-k3"
    assert resolved["tokens"] == 9000
    assert resolved["est_cost_usd"] == 0.42
    assert resolved["model"] != "forged/evil-model"
    assert resolved["tokens"] != 1
    assert resolved["est_cost_usd"] != 99.0

    # artifact:// path must also refuse the forged ROUTING pointer.
    ctx = _ctx(state_dir, repo)
    with sess._local_jobs_lock:
        for art in job["artifacts"]:
            if art.get("type") == "finding":
                art["execution_ref"] = forged["execution_ref"]
                break
        sess._persist_local_jobs_locked()
    finding_data = json.loads(
        resolve_internal_uri(
            f"artifact://local-forge/{finding['id']}", ctx,
        ).content,
    )
    assert finding_data["execution"]["model"] == "agentic/kimi-k3"
    assert finding_data["execution"]["tokens"] == 9000
    assert finding_data["execution"]["est_cost_usd"] == 0.42


def test_provenance_is_preserved_on_the_terminal_artifact(session):
    sess, state_dir, repo = session
    sess._register_local_job("local-prov", "audit cost", role="analysis",
                             cwd=repo, engine="agentic")
    sess._finish_local_job(
        "local-prov", ok=True, summary="cost math checks out",
        tokens=12_500, est_cost_usd=0.42, engine="agentic", model="kimi-k3",
    )

    job = sess._local_jobs["local-prov"]
    terminal = next(a for a in job["artifacts"] if a["type"] == "analysis")
    assert terminal["adapter"] == "agentic"
    assert terminal["model"] == "agentic/kimi-k3"
    assert terminal["result"] == "completed"
    assert terminal["tokens"] == 12_500
    assert terminal["est_cost_usd"] == 0.42
    assert terminal["cost_provenance"] == "provider"

    row = project_local_job_for_swarm_live(job)
    assert row["tokens"] == 12_500
    assert row["est_cost_usd"] == 0.42
    assert row["estimated"] is False
    assert row["cost_provenance"] == "provider"


def test_artifact_uri_is_stable_across_register_and_finish(session):
    sess, state_dir, repo = session
    sess._register_local_job("local-stable", "audit routing", role="analysis",
                             cwd=repo, engine="agentic")
    # Seed a ROUTING card the way preview_agentic_route would.
    with sess._local_jobs_lock:
        sess._local_jobs["local-stable"]["artifacts"] = [{
            "id": "local-stable-routing",
            "type": "ROUTING",
            "headline": "Routed to (pending)",
            "created_by": "router",
        }]
        sess._persist_local_jobs_locked()

    ctx = _ctx(state_dir, repo)
    before = json.loads(
        resolve_internal_uri("artifact://local-stable/local-stable-routing", ctx).content,
    )
    assert before["type"] == "ROUTING"

    sess._finish_local_job("local-stable", ok=True, summary="done",
                           engine="agentic", model="kimi-k3")

    after = json.loads(
        resolve_internal_uri("artifact://local-stable/local-stable-routing", ctx).content,
    )
    assert after["headline"] == "Routed to kimi-k3"
    assert after["policy"] == "balanced"


def test_restart_interruption_keeps_routing_and_claims_nothing(session, tmp_path):
    sess, state_dir, repo = session
    sess._register_local_job("local-ghost", "long audit", role="analysis",
                             cwd=repo, engine="agentic")
    with sess._local_jobs_lock:
        sess._local_jobs["local-ghost"]["artifacts"] = [{
            "id": "local-ghost-routing", "type": "ROUTING", "headline": "Routed to kimi-k3",
        }]
        sess._persist_local_jobs_locked()

    reloaded = _Session(state_dir, repo)
    reloaded._load_local_jobs()

    job = reloaded._local_jobs["local-ghost"]
    assert job["status"] == "cancelled"
    assert [a["type"] for a in job["artifacts"]] == ["ROUTING", "error"]
    assert project_local_job_for_swarm_live(job)["artifacts_complete"] is False


def test_local_job_uri_rejects_other_session_and_other_repo(session, tmp_path):
    sess, state_dir, repo = session
    other_repo = tmp_path / "other-repo"
    other_repo.mkdir()
    sess._register_local_job("local-scoped", "audit", role="analysis",
                             cwd=repo, engine="agentic")
    sess._finish_local_job("local-scoped", ok=True, summary="ok", engine="agentic")

    assert resolve_internal_uri("job://local-scoped", _ctx(state_dir, repo)).content

    with pytest.raises(InternalUriError, match="local job not found"):
        resolve_internal_uri("job://local-scoped", _ctx(state_dir, str(other_repo)))
    with pytest.raises(InternalUriError, match="local job not found"):
        resolve_internal_uri(
            "job://local-scoped", _ctx(state_dir, repo, session_id="sess-other"),
        )


def test_terminal_artifact_type_matrix():
    assert terminal_artifact_type(
        ok=True, cancelled=False, role="explore", has_file_evidence=True,
    ) == "analysis"
    assert terminal_artifact_type(
        ok=True, cancelled=False, role="implement", has_file_evidence=True,
    ) == "patch"
    assert terminal_artifact_type(
        ok=True, cancelled=False, role="implement", has_file_evidence=False,
    ) == "analysis"
    assert terminal_artifact_type(
        ok=False, cancelled=False, role="implement", has_file_evidence=True,
    ) == "error"
    assert terminal_artifact_type(
        ok=True, cancelled=True, role="implement", has_file_evidence=True,
    ) == "error"


def test_store_backed_job_ids_never_resolve_through_the_sidecar(session):
    """`job_*` stays store-backed; only `local-*` is sidecar-backed."""
    sess, state_dir, repo = session
    store = create_store("sqlite", state_dir)
    store.init()
    job = store.create_job(
        "audit swarm artifact resolvability",
        label=job_label_for_session("sess-a"),
    )
    task = Task(
        job_id=job.id,
        role="audit",
        instruction="scan",
        payload=stamp_task_payload({}, session_id="sess-a"),
    )
    store.save_task(task)
    run = AgentRun(job_id=job.id, task_id=task.id, role="audit", worker_id="w-1")
    store.save_run(run)

    ctx = _ctx(state_dir, repo)
    payload = json.loads(resolve_internal_uri(f"job://{job.id}", ctx).content)
    assert payload["id"] == job.id
    # A store job id is never confused for a sidecar row, even with a same-named
    # local job present.
    sess._register_local_job("local-decoy", "decoy", role="analysis",
                             cwd=repo, engine="agentic")
    sess._finish_local_job("local-decoy", ok=True, summary="decoy", engine="agentic")
    assert json.loads(
        resolve_internal_uri(f"job://{job.id}", ctx).content,
    )["id"] == job.id


def test_store_surfaced_artifacts_are_resolvable_and_counts_agree(session):
    """Every artifact a store-backed swarm claims must be individually readable."""
    sess, state_dir, repo = session
    store = create_store("sqlite", state_dir)
    store.init()
    job = store.create_job(
        "audit swarm artifact resolvability",
        label=job_label_for_session("sess-a"),
    )
    task = Task(
        job_id=job.id,
        role="audit",
        instruction="scan",
        payload=stamp_task_payload({}, session_id="sess-a"),
    )
    store.save_task(task)
    run = AgentRun(job_id=job.id, task_id=task.id, role="audit", worker_id="w-1")
    store.save_run(run)
    claims = [
        "keys.py ignores the legacy keyfile on upgraded installs",
        "platform.json split-brain silently disables adapter toggles",
        "agentic rows survive for providers with no credential",
    ]
    saved_ids = []
    for claim in claims:
        artifact = Artifact(
            job_id=job.id,
            task_id=task.id,
            type=ArtifactType.FINDING,
            created_by=run.worker_id,
            payload={"claim": claim},
            confidence=0.9,
            evidence=["harness/keys.py"],
        )
        store.save_artifact(artifact)
        saved_ids.append(artifact.id)

    ctx = _ctx(state_dir, repo)
    listing = resolve_internal_uri(f"job://{job.id}/artifacts", ctx).content
    listed_ids = [line.split("\t", 1)[0] for line in listing.splitlines() if line.strip()]
    assert sorted(listed_ids) == sorted(saved_ids)

    for artifact_id, claim in zip(saved_ids, claims):
        data = json.loads(
            resolve_internal_uri(f"artifact://{job.id}/{artifact_id}", ctx).content,
        )
        assert data["id"] == artifact_id
        assert data["payload"]["claim"] == claim

    with pytest.raises(InternalUriError, match="artifact not found"):
        resolve_internal_uri(f"artifact://{job.id}/does-not-exist", ctx)
