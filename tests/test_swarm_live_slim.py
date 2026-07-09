"""Terminal /api/swarm/live rows ship slim artifacts; expand uses /api/artifacts."""
from __future__ import annotations

from types import SimpleNamespace

from harness.server import (
    _job_dead_run_failure,
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


def test_dead_run_from_raw_before_slim():
    dead = [
        _art(
            art_type=ArtifactType.VERIFICATION,
            payload={"result": "failed", "failure": "no_model"},
        ),
        _art(
            art_type=ArtifactType.VERIFICATION,
            payload={"result": "failed", "failure": "no_model"},
        ),
    ]
    assert _job_dead_run_failure(dead, "completed") == "no_model"

    alive = dead + [
        _art(art_type=ArtifactType.FINDING, payload={"claim": "real work"}),
    ]
    assert _job_dead_run_failure(alive, "completed") is None

    # Slim list of only failed verdicts must not be used client-side alone;
    # server stamp is computed on the full raw list above.
    slim_only = _slim_swarm_list_artifacts(alive, _Fmt())
    # After slim, findings are gone -- client would mis-detect without stamp.
    assert all(
        (a.get("result") or "").lower() in ("failed", "blocked", "")
        for a in slim_only
        if "verification" in str(a["type"]).lower()
    )
