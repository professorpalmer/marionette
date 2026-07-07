"""Per-job swarm accounting prefers measured usage over routing estimates."""
from __future__ import annotations

from types import SimpleNamespace

from harness.server import _job_swarm_accounting, _routing_estimate_cost
from puppetmaster.models import Artifact, ArtifactType


def _artifact(
    *,
    task_id: str,
    art_type: ArtifactType = ArtifactType.VERIFICATION,
    created_by: str = "worker",
    payload: dict | None = None,
) -> Artifact:
    return Artifact(
        job_id="job-1",
        task_id=task_id,
        type=art_type,
        created_by=created_by,
        payload=payload or {},
        confidence=0.9,
        evidence=[],
    )


def _registry_spec(
    spec_id: str,
    *,
    input_per_mtok_usd: float = 1.0,
    output_per_mtok_usd: float = 2.0,
    billing: str = "metered",
):
    return SimpleNamespace(
        id=spec_id,
        adapter_model_name=spec_id,
        input_per_mtok_usd=input_per_mtok_usd,
        output_per_mtok_usd=output_per_mtok_usd,
        billing=billing,
        marginal_cost_usd=lambda tin, tout: (
            (tin / 1_000_000.0) * input_per_mtok_usd
            + (tout / 1_000_000.0) * output_per_mtok_usd
        ),
        estimate_cost_usd=lambda tin, tout: (
            (tin / 1_000_000.0) * input_per_mtok_usd
            + (tout / 1_000_000.0) * output_per_mtok_usd
        ),
    )


def test_routing_estimate_cost_dedupes_per_task():
    arts = [
        _artifact(
            task_id="t1",
            art_type=ArtifactType.ROUTING,
            created_by="router",
            payload={"estimated_cost_usd": 0.05, "model_id": "cheap"},
        ),
        _artifact(
            task_id="t1",
            art_type=ArtifactType.ROUTING,
            created_by="router",
            payload={"estimated_cost_usd": 0.99, "model_id": "cheap"},
        ),
        _artifact(
            task_id="t2",
            art_type=ArtifactType.ROUTING,
            created_by="router",
            payload={"estimated_cost_usd": 0.03, "model_id": "cheap"},
        ),
    ]
    assert abs(_routing_estimate_cost(arts) - 0.08) < 1e-9


def test_job_swarm_accounting_uses_actual_usage_not_routing_estimate():
    registry = [_registry_spec("worker-model", input_per_mtok_usd=1.0, output_per_mtok_usd=2.0)]
    arts = [
        _artifact(
            task_id="t1",
            art_type=ArtifactType.ROUTING,
            created_by="router",
            payload={"estimated_cost_usd": 0.062, "model_id": "worker-model"},
        ),
        _artifact(
            task_id="t1",
            payload={
                "tokens_in": 100_000,
                "tokens_out": 20_000,
                "model": "worker-model",
            },
        ),
    ]
    tokens, cost = _job_swarm_accounting(arts, registry)
    assert tokens == 120_000
    # 100k*$1/M + 20k*$2/M = $0.14, not the $0.062 routing estimate.
    assert abs(cost - 0.14) < 1e-6


def test_job_swarm_accounting_falls_back_to_routing_before_usage():
    registry = [_registry_spec("worker-model")]
    arts = [
        _artifact(
            task_id="t1",
            art_type=ArtifactType.ROUTING,
            created_by="router",
            payload={"estimated_cost_usd": 0.062, "model_id": "worker-model"},
        ),
    ]
    tokens, cost = _job_swarm_accounting(arts, registry)
    assert tokens == 0
    assert abs(cost - 0.062) < 1e-6
