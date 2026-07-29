"""Actual-usage routing list-price value + uncapped cache gross."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from harness.api.cost_accounting import CACHE_READ_MULTIPLIER, _cache_savings_gross
from harness.api.routing_savings import (
    _delegation_saved_usd,
    _delegation_saved_usd_detail,
    _registry_rates,
    _routing_saved_usd,
    _routing_saved_usd_detail,
    _sum_job_set_savings_detail,
)
from harness.server import _cache_savings_with_basis
from puppetmaster.models import Artifact, ArtifactType


PRICE_IN = 3.0


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
    )


def _routing(
    task_id: str,
    *,
    policy: str,
    baseline: float,
    estimated: float,
    model_id: str = "cheap-model",
    baseline_model_id: str = "",
    created_by: str = "router",
):
    return Artifact(
        job_id="j1",
        task_id=task_id,
        type=ArtifactType.ROUTING,
        created_by=created_by,
        payload={
            "model_id": model_id,
            "adapter": "agentic",
            "policy": policy,
            "baseline_cost_usd": baseline,
            "estimated_cost_usd": estimated,
            "baseline_model_id": baseline_model_id,
        },
        confidence=1.0,
        evidence=["route"],
    )


def _verification(
    task_id: str,
    model: str,
    tin: int,
    tout: int,
    *,
    tokens_cached: int = 0,
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
    return Artifact(
        job_id="j1",
        task_id=task_id,
        type=ArtifactType.VERIFICATION,
        created_by="worker",
        payload=payload,
        confidence=0.9,
        evidence=["usage"],
    )


def test_routing_preflight_estimated_still_works():
    arts = [
        _routing("t1", policy="balanced", baseline=0.50, estimated=0.10),
        _routing("t2", policy="quality", baseline=0.50, estimated=0.10),
    ]
    detail = _routing_saved_usd_detail(arts, [])
    assert detail["routing_saved_usd"] == pytest.approx(0.40)
    assert detail["routing_savings_basis"] == "estimated"


def test_routing_actual_usage_1m_cheap_vs_expensive_materially_saved():
    registry = [
        _registry_spec("cheap-model", input_per_mtok_usd=1.0, output_per_mtok_usd=2.0),
        _registry_spec("expensive-model", input_per_mtok_usd=10.0, output_per_mtok_usd=30.0),
    ]
    arts = [
        _routing(
            "t1",
            policy="balanced",
            baseline=0.01,
            estimated=0.009,
            model_id="cheap-model",
            baseline_model_id="expensive-model",
        ),
        _verification("t1", "cheap-model", 1_000_000, 0),
    ]
    detail = _routing_saved_usd_detail(arts, registry)
    assert detail["routing_saved_usd"] == pytest.approx(9.0)
    assert detail["routing_savings_basis"] == "actual_usage"
    assert detail["routing_tokens_compared"] == 1_000_000


def test_routing_actual_usage_model_overrides_stale_route_model():
    registry = [
        _registry_spec("stale-route-model", input_per_mtok_usd=8.0, output_per_mtok_usd=20.0),
        _registry_spec("actual-cheap-model", input_per_mtok_usd=1.0, output_per_mtok_usd=2.0),
        _registry_spec("expensive-model", input_per_mtok_usd=10.0, output_per_mtok_usd=30.0),
    ]
    arts = [
        _routing(
            "t1",
            policy="balanced",
            baseline=0.02,
            estimated=0.01,
            model_id="stale-route-model",
            baseline_model_id="expensive-model",
        ),
        _verification("t1", "actual-cheap-model", 1_000_000, 0),
    ]
    detail = _routing_saved_usd_detail(arts, registry)
    assert detail["routing_saved_usd"] == pytest.approx(9.0)
    assert detail["routing_savings_basis"] == "actual_usage"


def test_routing_actual_usage_symmetric_cache_multiplier():
    registry = [
        _registry_spec("cheap-model", input_per_mtok_usd=1.0, output_per_mtok_usd=2.0),
        _registry_spec("expensive-model", input_per_mtok_usd=10.0, output_per_mtok_usd=30.0),
    ]
    arts = [
        _routing(
            "t1",
            policy="cheap",
            baseline=99.0,
            estimated=0.01,
            model_id="cheap-model",
            baseline_model_id="expensive-model",
        ),
        _verification("t1", "cheap-model", 1_000_000, 0, tokens_cached=400_000),
    ]
    detail = _routing_saved_usd_detail(arts, registry)
    assert detail["routing_saved_usd"] == pytest.approx(5.76)
    assert detail["routing_savings_basis"] == "actual_usage"
    assert CACHE_READ_MULTIPLIER == 0.1


def test_routing_actual_overrides_tiny_preflight_estimate():
    registry = [
        _registry_spec("cheap-model", input_per_mtok_usd=1.0, output_per_mtok_usd=2.0),
        _registry_spec("expensive-model", input_per_mtok_usd=5.0, output_per_mtok_usd=15.0),
    ]
    arts = [
        _routing(
            "t1",
            policy="balanced",
            baseline=0.02,
            estimated=0.019,
            model_id="cheap-model",
            baseline_model_id="expensive-model",
        ),
        _verification("t1", "cheap-model", 500_000, 100_000),
    ]
    detail = _routing_saved_usd_detail(arts, registry)
    assert detail["routing_saved_usd"] == pytest.approx(3.3)
    assert detail["routing_savings_basis"] == "actual_usage"
    assert detail["routing_saved_usd"] > 1.0


def test_routing_zero_priced_plan_uses_active_baseline_fallback():
    registry = [
        _registry_spec("cheap-model", input_per_mtok_usd=1.0, output_per_mtok_usd=2.0),
    ]
    arts = [
        _routing(
            "t1",
            policy="balanced",
            baseline=0.0,
            estimated=0.0,
            model_id="cheap-model",
            baseline_model_id="cursor/unknown-frontier",
        ),
        _verification("t1", "cheap-model", 1_000_000, 0),
    ]
    detail = _routing_saved_usd_detail(
        arts,
        registry,
        active_price_in=10.0,
        active_price_out=30.0,
    )
    assert detail["routing_saved_usd"] == pytest.approx(9.0)
    assert detail["routing_savings_basis"] == "actual_usage"


def test_routing_unresolved_chosen_is_unknown_zero(monkeypatch):
    registry = [
        _registry_spec("expensive-model", input_per_mtok_usd=10.0, output_per_mtok_usd=30.0),
    ]
    monkeypatch.setattr(
        "harness.api.routing_savings._pmharness_positive_rates",
        lambda _mid: (0.0, 0.0),
    )
    arts = [
        _routing(
            "t1",
            policy="balanced",
            baseline=5.0,
            estimated=0.1,
            model_id="totally-unknown-chosen",
            baseline_model_id="expensive-model",
        ),
        _verification("t1", "totally-unknown-chosen", 1_000_000, 0),
    ]
    detail = _routing_saved_usd_detail(arts, registry)
    assert detail["routing_saved_usd"] == 0.0
    assert detail["routing_savings_basis"] == "unknown"
    assert _routing_saved_usd(arts, registry) == 0.0


def test_routing_duplicate_artifacts_count_once():
    registry = [
        _registry_spec("cheap-model", input_per_mtok_usd=1.0, output_per_mtok_usd=2.0),
        _registry_spec("expensive-model", input_per_mtok_usd=10.0, output_per_mtok_usd=30.0),
    ]
    arts = [
        _routing(
            "t1",
            policy="balanced",
            baseline=0.5,
            estimated=0.1,
            model_id="cheap-model",
            baseline_model_id="expensive-model",
            created_by="router",
        ),
        _routing(
            "t1",
            policy="balanced",
            baseline=0.5,
            estimated=0.1,
            model_id="cheap-model",
            baseline_model_id="expensive-model",
            created_by="router-fallback",
        ),
        _verification("t1", "cheap-model", 1_000_000, 0),
        _verification("t1", "cheap-model", 1_000_000, 0),
    ]
    detail = _routing_saved_usd_detail(arts, registry)
    assert detail["routing_saved_usd"] == pytest.approx(9.0)
    assert detail["routing_tokens_compared"] == 1_000_000


def test_cache_savings_gross_grows_past_provider_cap():
    cached = 1_000_000
    gross = _cache_savings_gross(cached, PRICE_IN)
    capped, basis = _cache_savings_with_basis(cached, PRICE_IN, provider_cost_usd=1.10)
    assert gross == pytest.approx(2.7)
    assert capped == pytest.approx(1.10)
    assert basis == "capped"
    assert gross > capped


def test_sum_job_set_savings_detail_aggregate_basis_and_tokens(monkeypatch):
    """actual_usage wins; estimated only when every counted job is estimated."""
    registry = [
        _registry_spec("cheap-model", input_per_mtok_usd=1.0, output_per_mtok_usd=2.0),
        _registry_spec("expensive-model", input_per_mtok_usd=10.0, output_per_mtok_usd=30.0),
    ]
    arts_by_job = {
        "actual": [
            _routing(
                "t1",
                policy="balanced",
                baseline=0.01,
                estimated=0.009,
                model_id="cheap-model",
                baseline_model_id="expensive-model",
            ),
            _verification("t1", "cheap-model", 1_000_000, 0),
        ],
        "estimated": [
            _routing("t2", policy="cheap", baseline=0.50, estimated=0.10),
        ],
    }

    def _arts(jid):
        return arts_by_job[jid]

    # Keep this test focused on routing aggregation (not swarm-cache USD).
    import harness.server as server

    monkeypatch.setattr(server, "_cache_saved_usd_swarm", lambda arts, reg: 0.0)

    mixed = _sum_job_set_savings_detail(
        ["actual", "estimated"], _arts, registry
    )
    assert mixed["routing_savings_basis"] == "actual_usage"
    assert mixed["routing_tokens_compared"] == 1_000_000
    assert mixed["routing_saved_usd"] == pytest.approx(9.0 + 0.40)

    est_only = _sum_job_set_savings_detail(["estimated"], _arts, registry)
    assert est_only["routing_savings_basis"] == "estimated"
    assert est_only["routing_tokens_compared"] == 0
    assert est_only["routing_saved_usd"] == pytest.approx(0.40)


def test_measured_usage_without_baseline_model_is_unknown_not_preflight():
    registry = [
        _registry_spec("cheap-model", input_per_mtok_usd=1.0, output_per_mtok_usd=2.0),
    ]
    arts = [
        _routing(
            "t1",
            policy="balanced",
            baseline=0.50,
            estimated=0.10,
            model_id="cheap-model",
        ),
        _verification("t1", "cheap-model", 1_000_000, 0),
    ]
    detail = _routing_saved_usd_detail(arts, registry)
    assert detail["routing_saved_usd"] == 0.0
    assert detail["routing_savings_basis"] == "unknown"
    assert detail["routing_savings_counted"] is True


def test_delegation_heavy_cache_not_collapsed_to_ten_percent():
    """Model-selection value ignores cache discount — not ~5.76 like routing."""
    registry = [
        _registry_spec("cheap-model", input_per_mtok_usd=1.0, output_per_mtok_usd=2.0),
        _registry_spec("expensive-model", input_per_mtok_usd=10.0, output_per_mtok_usd=30.0),
    ]
    arts = [
        _routing(
            "t1",
            policy="cheap",
            baseline=99.0,
            estimated=0.01,
            model_id="cheap-model",
            baseline_model_id="expensive-model",
        ),
        _verification("t1", "cheap-model", 1_000_000, 0, tokens_cached=400_000),
    ]
    routing = _routing_saved_usd_detail(arts, registry)
    delegation = _delegation_saved_usd_detail(arts, registry)
    assert routing["routing_saved_usd"] == pytest.approx(5.76)
    assert delegation["delegation_saved_usd"] == pytest.approx(9.0)
    assert delegation["delegation_savings_basis"] == "actual_usage"
    assert delegation["delegation_tokens_compared"] == 1_000_000


def test_delegation_non_routed_worker_with_active_baseline():
    """Local/non-routed workers contribute when both rates are known."""
    registry = [
        _registry_spec("cheap-model", input_per_mtok_usd=1.0, output_per_mtok_usd=2.0),
    ]
    arts = [
        _verification("t1", "cheap-model", 1_000_000, 0, tokens_cached=900_000),
    ]
    detail = _delegation_saved_usd_detail(
        arts,
        registry,
        active_price_in=10.0,
        active_price_out=30.0,
    )
    assert detail["delegation_saved_usd"] == pytest.approx(9.0)
    assert detail["delegation_savings_basis"] == "actual_usage"
    assert _routing_saved_usd_detail(arts, registry)["routing_saved_usd"] == 0.0


def test_delegation_unresolved_chosen_is_unknown_zero(monkeypatch):
    registry = [
        _registry_spec("expensive-model", input_per_mtok_usd=10.0, output_per_mtok_usd=30.0),
    ]
    monkeypatch.setattr(
        "harness.api.routing_savings._pmharness_positive_rates",
        lambda _mid: (0.0, 0.0),
    )
    arts = [
        _verification("t1", "totally-unknown-chosen", 1_000_000, 0),
    ]
    detail = _delegation_saved_usd_detail(
        arts,
        registry,
        active_price_in=10.0,
        active_price_out=30.0,
    )
    assert detail["delegation_saved_usd"] == 0.0
    assert detail["delegation_savings_basis"] == "unknown"
    assert _delegation_saved_usd(
        arts,
        registry,
        active_price_in=10.0,
        active_price_out=30.0,
    ) == 0.0


def test_sum_job_set_savings_detail_includes_delegation(monkeypatch):
    registry = [
        _registry_spec("cheap-model", input_per_mtok_usd=1.0, output_per_mtok_usd=2.0),
        _registry_spec("expensive-model", input_per_mtok_usd=10.0, output_per_mtok_usd=30.0),
    ]
    arts_by_job = {
        "j1": [
            _routing(
                "t1",
                policy="balanced",
                baseline=0.01,
                estimated=0.009,
                model_id="cheap-model",
                baseline_model_id="expensive-model",
            ),
            _verification("t1", "cheap-model", 1_000_000, 0, tokens_cached=500_000),
        ],
    }

    def _arts(jid):
        return arts_by_job[jid]

    import harness.server as server

    monkeypatch.setattr(server, "_cache_saved_usd_swarm", lambda arts, reg: 0.0)

    detail = _sum_job_set_savings_detail(["j1"], _arts, registry)
    assert detail["routing_saved_usd"] == pytest.approx(4.95)
    assert detail["delegation_saved_usd"] == pytest.approx(9.0)
    assert detail["delegation_savings_basis"] == "actual_usage"
    assert detail["delegation_tokens_compared"] == 1_000_000


def test_registry_rates_exact_id_beats_earlier_fuzzy_collision():
    """Exact gpt-4 row wins even when vendor-a/gpt4 appears first in registry."""
    registry = [
        _registry_spec("vendor-a/gpt4", input_per_mtok_usd=1.0, output_per_mtok_usd=2.0),
        _registry_spec("gpt-4", input_per_mtok_usd=3.0, output_per_mtok_usd=6.0),
    ]
    pin, pout = _registry_rates("gpt-4", registry)
    assert pin == pytest.approx(3.0)
    assert pout == pytest.approx(6.0)


def test_registry_rates_exact_id_beats_fuzzy_when_reversed_order():
    registry = [
        _registry_spec("gpt-4", input_per_mtok_usd=3.0, output_per_mtok_usd=6.0),
        _registry_spec("vendor-a/gpt4", input_per_mtok_usd=1.0, output_per_mtok_usd=2.0),
    ]
    pin, pout = _registry_rates("gpt-4", registry)
    assert pin == pytest.approx(3.0)


def test_registry_rates_conflicting_fuzzy_prices_fail_closed(monkeypatch):
    """Ambiguous fuzzy-only matches must not pick first — fall through to zero."""
    registry = [
        _registry_spec("vendor-a/gpt4", input_per_mtok_usd=1.0, output_per_mtok_usd=2.0),
        _registry_spec("vendor-b/gpt4", input_per_mtok_usd=5.0, output_per_mtok_usd=10.0),
    ]
    monkeypatch.setattr(
        "harness.api.routing_savings._pmharness_positive_rates",
        lambda _mid: (0.0, 0.0),
    )
    pin, pout = _registry_rates("gpt-4", registry)
    assert pin == 0.0
    assert pout == 0.0


def test_registry_rates_conflicting_exact_prices_do_not_fall_through_to_fuzzy():
    registry = [
        _registry_spec("gpt-4", input_per_mtok_usd=3.0, output_per_mtok_usd=6.0),
        _registry_spec("gpt-4", input_per_mtok_usd=4.0, output_per_mtok_usd=8.0),
        _registry_spec("vendor-a/gpt4", input_per_mtok_usd=1.0, output_per_mtok_usd=2.0),
    ]

    assert _registry_rates("gpt-4", registry) == (0.0, 0.0)


def test_registry_rates_unambiguous_fuzzy_still_resolves():
    registry = [
        _registry_spec("gpt-5-3-codex", input_per_mtok_usd=2.5, output_per_mtok_usd=10.0),
    ]
    pin, _pout = _registry_rates("codex/gpt.5.3.codex", registry)
    assert pin == pytest.approx(2.5)


def test_registry_rates_agentic_prefix_alias_exact_tier():
    registry = [
        _registry_spec(
            "agentic/deepseek/deepseek-v4-pro",
            input_per_mtok_usd=0.435,
            output_per_mtok_usd=0.87,
        ),
    ]
    pin, _pout = _registry_rates("deepseek/deepseek-v4-pro", registry)
    assert pin == pytest.approx(0.435)


def test_sum_job_set_savings_detail_threads_swarm_cache_honesty(monkeypatch):
    """Aggregate swarm_cache_savings_basis and unpriced tokens across jobs."""
    registry = [
        _registry_spec("priced-model", input_per_mtok_usd=3.0, output_per_mtok_usd=6.0),
    ]
    priced_arts = [
        _verification("t-priced", "priced-model", 100_000, 5_000, tokens_cached=20_000),
    ]
    unpriced_arts = [
        _verification("t-unpriced", "totally-unknown", 100_000, 5_000, tokens_cached=30_000),
    ]
    arts_by_job = {"priced": priced_arts, "unpriced": unpriced_arts}

    def _arts(jid):
        return arts_by_job[jid]

    monkeypatch.setattr(
        "harness.api.routing_savings._pmharness_positive_rates",
        lambda _mid: (0.0, 0.0),
    )

    detail = _sum_job_set_savings_detail(["priced", "unpriced"], _arts, registry)
    expected_cache = (20_000 / 1_000_000.0) * 3.0 * (1.0 - CACHE_READ_MULTIPLIER)
    assert detail["cache_saved_usd_swarm"] == pytest.approx(expected_cache)
    assert detail["swarm_cache_unpriced_tokens"] == 30_000
    assert detail["swarm_cache_savings_basis"] == "unknown"
