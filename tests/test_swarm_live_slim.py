"""Terminal /api/swarm/live rows ship slim artifacts; expand uses /api/artifacts."""
from __future__ import annotations

from types import SimpleNamespace

from harness.api.jobs import canonical_job_outcome
from harness.server import (
    _job_status_is_terminal,
    _slim_swarm_list_artifacts,
)
from puppetmaster.models import Artifact, ArtifactType


def _art(
    *,
    art_type: ArtifactType,
    payload: dict | None = None,
    created_by: str = "worker",
) -> Artifact:
    return Artifact(
        job_id="job-1",
        task_id="task-1",
        type=art_type,
        created_by=created_by,
        payload=payload or {},
        confidence=0.9,
        evidence=[],
    )


class _Fmt:
    def format_artifacts(self, artifacts: list) -> list:
        out = []
        for a in artifacts:
            payload = getattr(a, "payload", {}) or {}
            out.append({
                "type": str(getattr(a, "type", "")),
                "headline": payload.get("claim") or payload.get("check") or "",
                "result": payload.get("result"),
                "failure": payload.get("failure"),
                "model": payload.get("model_id"),
            })
        return out


def test_job_status_is_terminal():
    assert _job_status_is_terminal("completed")
    assert _job_status_is_terminal("JobStatus.COMPLETE")
    assert _job_status_is_terminal("failed")
    assert _job_status_is_terminal("cancelled")
    assert _job_status_is_terminal("stalled")
    assert not _job_status_is_terminal("running")
    assert not _job_status_is_terminal("in_progress")
    assert not _job_status_is_terminal("pending")


def test_slim_keeps_routing_and_verdicts_drops_findings():
    raw = [
        _art(
            art_type=ArtifactType.ROUTING,
            created_by="router",
            payload={"model_id": "glm-5.2", "estimated_cost_usd": 0.01},
        ),
        _art(
            art_type=ArtifactType.FINDING,
            payload={"claim": "big finding that should not ship on every poll"},
        ),
        _art(
            art_type=ArtifactType.VERIFICATION,
            payload={"check": "worker", "result": "failed", "failure": "no_model"},
        ),
        _art(
            art_type=ArtifactType.RISK,
            payload={"risk": "should also be omitted from slim list"},
        ),
    ]
    slim = _slim_swarm_list_artifacts(raw, _Fmt())
    types = {str(a["type"]).lower() for a in slim}
    assert "routing" in types
    assert "verification" in types
    assert "finding" not in types
    assert "risk" not in types
    assert len(slim) == 2


def test_canonical_outcome_uses_full_raw_artifacts_before_slim():
    failed_before_execution = [
        _art(
            art_type=ArtifactType.ROUTING,
            created_by="router",
            payload={"model_id": "gpt-5.6-luna"},
        ),
        _art(
            art_type=ArtifactType.VERIFICATION,
            payload={"result": "failed", "failure": "provider_error"},
        ),
    ]
    outcome = canonical_job_outcome(failed_before_execution)
    assert outcome["quality"] == "degraded"
    assert outcome["trustworthy"] is False
    assert outcome["reasons"] == [
        "only verification artifacts — no findings/decisions/patches"
    ]

    substantive = failed_before_execution + [
        _art(art_type=ArtifactType.FINDING, payload={"claim": "real work"}),
    ]
    outcome = canonical_job_outcome(substantive)
    assert outcome["quality"] == "ok"
    assert outcome["trustworthy"] is True

    # Canonical quality is computed before the poll payload drops findings.
    slim_only = _slim_swarm_list_artifacts(substantive, _Fmt())
    assert all(
        (a.get("result") or "").lower() in ("failed", "blocked", "")
        for a in slim_only
        if "verification" in str(a["type"]).lower()
    )


def test_canonical_outcome_fail_closed_when_kernel_errors(monkeypatch):
    """A missing or throwing quality module must not paint green or wipe live."""
    import puppetmaster.quality as quality

    def _boom(_artifacts):
        raise RuntimeError("kernel down")

    monkeypatch.setattr(quality, "assess_run_quality", _boom)
    outcome = canonical_job_outcome([])
    assert outcome["trustworthy"] is False
    assert outcome["quality"] == "empty"
    assert "could not be assessed" in outcome["reasons"][0]
