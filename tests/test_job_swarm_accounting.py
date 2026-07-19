"""Per-job swarm accounting prefers measured usage over routing estimates."""
from __future__ import annotations

from types import SimpleNamespace

from harness.server import (
    _job_swarm_accounting,
    _job_swarm_accounting_detail,
    _routing_estimate_cost,
    _task_swarm_accounting,
)
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


def test_routing_estimate_cost_prefers_fallback_over_plan_zero():
    """Initial plan-billed router pick is $0; fallback estimate must win."""
    arts = [
        _artifact(
            task_id="t1",
            art_type=ArtifactType.ROUTING,
            created_by="router",
            payload={"estimated_cost_usd": 0.0, "model_id": "cursor/gpt-5-4"},
        ),
        _artifact(
            task_id="t1",
            art_type=ArtifactType.ROUTING,
            created_by="router-fallback",
            payload={"estimated_cost_usd": 0.0048, "model_id": "agentic/z-ai/glm-5.2"},
        ),
    ]
    assert abs(_routing_estimate_cost(arts) - 0.0048) < 1e-9


def test_job_swarm_accounting_prices_fallback_not_failed_first_attempt():
    """Cursor fails (sdk_not_installed), agentic GLM succeeds with real_cost.

    Regression: badge/cost followed the initial plan-billed router pick ($0)
    and first-wins usage kept the failed attempt's tiny estimated tokens.
    """
    registry = [
        _registry_spec("cursor/gpt-5-4", billing="plan", input_per_mtok_usd=0.0, output_per_mtok_usd=0.0),
        _registry_spec("agentic/z-ai/glm-5.2", input_per_mtok_usd=0.4, output_per_mtok_usd=1.6),
    ]
    arts = [
        _artifact(
            task_id="t1",
            art_type=ArtifactType.ROUTING,
            created_by="router",
            payload={"estimated_cost_usd": 0.0, "model_id": "cursor/gpt-5-4", "billing": "plan"},
        ),
        _artifact(
            task_id="t1",
            art_type=ArtifactType.ROUTING,
            created_by="router-fallback",
            payload={"estimated_cost_usd": 0.0048, "model_id": "agentic/z-ai/glm-5.2"},
        ),
        _artifact(
            task_id="t1",
            payload={
                "model": "gpt-5.4",
                "result": "failed",
                "failure": "sdk_not_installed",
                "tokens_in": 3888,
                "tokens_out": 0,
                "tokens_estimated": True,
            },
        ),
        _artifact(
            task_id="t1",
            payload={
                "model": "z-ai/glm-5.2",
                "result": "passed",
                "tokens_in": 100_000,
                "tokens_out": 5_000,
                "real_cost_usd": 0.048,
            },
        ),
    ]
    tokens, cost = _job_swarm_accounting(arts, registry)
    assert tokens == 105_000
    assert abs(cost - 0.048) < 1e-6


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


def test_job_swarm_accounting_keeps_estimate_when_usage_unpriceable():
    """Usage from a model the registry cannot price must not zero the cost.

    Regression: a finished job showed the routing estimate while running, then
    snapped to $0 on completion because price_job returned 0 for its unpriced
    usage records and that 0 was treated as authoritative."""
    arts = [
        _artifact(
            task_id="t1",
            art_type=ArtifactType.ROUTING,
            created_by="router",
            payload={"estimated_cost_usd": 0.0067, "model_id": "unknown-model"},
        ),
        _artifact(
            task_id="t1",
            payload={
                "tokens_in": 300_000,
                "tokens_out": 60_000,
                "model": "unknown-model",
            },
        ),
    ]
    tokens, cost = _job_swarm_accounting(arts, registry=[])
    assert tokens == 360_000
    assert abs(cost - 0.0067) < 1e-9


def test_job_swarm_accounting_prices_unknown_models_from_live_map(monkeypatch):
    """Models missing from ~/.puppetmaster/models.json get real usage-based
    pricing from the live OpenRouter price map instead of the frozen routing
    estimate."""
    import pmharness.registry as registry_mod

    monkeypatch.setattr(
        registry_mod, "price",
        lambda name: (0.4, 1.6) if name == "z-ai/glm-5.2" else (None, None),
    )
    arts = [
        _artifact(
            task_id="t1",
            art_type=ArtifactType.ROUTING,
            created_by="router",
            payload={"estimated_cost_usd": 0.0067, "model_id": "z-ai/glm-5.2"},
        ),
        _artifact(
            task_id="t1",
            payload={
                "tokens_in": 1_000_000,
                "tokens_out": 100_000,
                "model": "z-ai/glm-5.2",
            },
        ),
    ]
    tokens, cost = _job_swarm_accounting(arts, registry=[])
    assert tokens == 1_100_000
    # 1M*$0.4/M + 100k*$1.6/M = $0.56 of measured spend, not the $0.0067 estimate.
    assert abs(cost - 0.56) < 1e-6


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


def test_job_swarm_accounting_prices_verification_without_routing():
    registry = [_registry_spec("worker-model", input_per_mtok_usd=1.0, output_per_mtok_usd=2.0)]
    arts = [
        _artifact(
            task_id="t1",
            payload={
                "tokens_in": 50_000,
                "tokens_out": 10_000,
                "model": "worker-model",
            },
        ),
    ]
    tokens, cost = _job_swarm_accounting(arts, registry)
    assert tokens == 60_000
    assert abs(cost - 0.07) < 1e-6


def test_task_swarm_accounting_prefers_usage_over_routing_per_task():
    """Worker rows must show measured tokens/cost, not the routing estimate."""
    registry = [_registry_spec("worker-model", input_per_mtok_usd=1.0, output_per_mtok_usd=2.0)]
    arts = [
        _artifact(
            task_id="t1",
            art_type=ArtifactType.ROUTING,
            created_by="router",
            payload={"estimated_cost_usd": 0.062, "model_id": "worker-model"},
        ),
        _artifact(
            task_id="t2",
            art_type=ArtifactType.ROUTING,
            created_by="router",
            payload={"estimated_cost_usd": 0.03, "model_id": "worker-model"},
        ),
        _artifact(
            task_id="t1",
            payload={
                "tokens_in": 100_000,
                "tokens_out": 20_000,
                "model": "worker-model",
            },
        ),
        _artifact(
            task_id="t2",
            payload={
                "tokens_in": 50_000,
                "tokens_out": 10_000,
                "model": "worker-model",
            },
        ),
    ]
    by_task = _task_swarm_accounting(arts, registry)
    assert by_task["t1"]["tokens"] == 120_000
    assert abs(by_task["t1"]["est_cost_usd"] - 0.14) < 1e-6
    assert by_task["t2"]["tokens"] == 60_000
    assert abs(by_task["t2"]["est_cost_usd"] - 0.07) < 1e-6
    # Job aggregate still matches the sum of per-task meters.
    tokens, cost = _job_swarm_accounting(arts, registry)
    assert tokens == 180_000
    assert abs(cost - 0.21) < 1e-6


def test_task_swarm_accounting_falls_back_to_routing_estimate():
    registry = [_registry_spec("worker-model")]
    arts = [
        _artifact(
            task_id="t1",
            art_type=ArtifactType.ROUTING,
            created_by="router",
            payload={"estimated_cost_usd": 0.062, "model_id": "worker-model"},
        ),
        _artifact(
            task_id="t2",
            art_type=ArtifactType.ROUTING,
            created_by="router-fallback",
            payload={"estimated_cost_usd": 0.0048, "model_id": "worker-model"},
        ),
    ]
    by_task = _task_swarm_accounting(arts, registry)
    assert by_task["t1"]["tokens"] == 0
    assert abs(by_task["t1"]["est_cost_usd"] - 0.062) < 1e-9
    assert by_task["t2"]["tokens"] == 0
    assert abs(by_task["t2"]["est_cost_usd"] - 0.0048) < 1e-9


def test_task_swarm_accounting_keeps_estimate_when_usage_unpriceable():
    arts = [
        _artifact(
            task_id="t1",
            art_type=ArtifactType.ROUTING,
            created_by="router",
            payload={"estimated_cost_usd": 0.0067, "model_id": "unknown-model"},
        ),
        _artifact(
            task_id="t1",
            payload={
                "tokens_in": 300_000,
                "tokens_out": 60_000,
                "model": "unknown-model",
            },
        ),
    ]
    by_task = _task_swarm_accounting(arts, registry=[])
    assert by_task["t1"]["tokens"] == 360_000
    assert abs(by_task["t1"]["est_cost_usd"] - 0.0067) < 1e-9


def test_zero_work_job_does_not_inherit_routing_estimate():
    """Completed job with zero-token usage stays $0, not the routing estimate."""
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
                "tokens_in": 0,
                "tokens_out": 0,
                "model": "worker-model",
                "result": "passed",
            },
        ),
    ]
    tokens, cost = _job_swarm_accounting(arts, registry=[_registry_spec("worker-model")])
    assert tokens == 0
    assert cost == 0.0
    detail = _job_swarm_accounting_detail(arts, registry=[_registry_spec("worker-model")])
    assert detail["est_cost_usd"] == 0.0
    assert detail["estimated"] is False
    assert detail["cost_provenance"] == "provider"


def test_job_detail_marks_provider_override():
    registry = [_registry_spec("worker-model", input_per_mtok_usd=1.0, output_per_mtok_usd=2.0)]
    arts = [
        _artifact(
            task_id="t1",
            art_type=ArtifactType.ROUTING,
            created_by="router",
            payload={"estimated_cost_usd": 0.99, "model_id": "worker-model"},
        ),
        _artifact(
            task_id="t1",
            payload={
                "tokens_in": 100_000,
                "tokens_out": 20_000,
                "model": "worker-model",
                "real_cost_usd": 0.048,
            },
        ),
    ]
    detail = _job_swarm_accounting_detail(arts, registry)
    assert abs(detail["est_cost_usd"] - 0.048) < 1e-6
    assert detail["cost_provenance"] == "provider"
    assert detail["estimated"] is False


def test_task_zero_work_does_not_keep_routing_estimate():
    arts = [
        _artifact(
            task_id="t1",
            art_type=ArtifactType.ROUTING,
            created_by="router",
            payload={"estimated_cost_usd": 0.062, "model_id": "worker-model"},
        ),
        _artifact(
            task_id="t1",
            payload={"tokens_in": 0, "tokens_out": 0, "model": "worker-model"},
        ),
    ]
    by_task = _task_swarm_accounting(arts, registry=[_registry_spec("worker-model")])
    assert by_task["t1"]["tokens"] == 0
    assert by_task["t1"]["est_cost_usd"] == 0.0
    assert by_task["t1"]["estimated"] is False
