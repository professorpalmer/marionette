"""Characterization tests for cost/usage helper peel into harness.api.cost."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import harness.api.cost as cost
import harness.server as server


def test_cost_module_owns_multipliers_and_session_cost():
    assert cost.CACHE_READ_MULTIPLIER == 0.1
    assert server.CACHE_READ_MULTIPLIER is cost.CACHE_READ_MULTIPLIER
    assert cost._session_cost(1_000_000, 0, 0, 3.0, 15.0) == 3.0
    assert server._session_cost is cost._session_cost


def test_server_reexports_swarm_accounting_helpers():
    assert server._job_swarm_accounting is cost._job_swarm_accounting
    assert server._sum_job_set_savings is cost._sum_job_set_savings
    assert server._scoped_jobs_with_stores is cost._scoped_jobs_with_stores
    assert server._boot_usage_meters is cost._boot_usage_meters
    assert server._persist_boot_usage is cost._persist_boot_usage


def test_boot_scalar_aliases_write_through_to_cost_module():
    prior = cost._BOOT_CARRY_COST_USD
    prior_epoch = cost._COST_EPOCH
    try:
        server._BOOT_CARRY_COST_USD = 9.25
        assert cost._BOOT_CARRY_COST_USD == 9.25
        assert server._BOOT_CARRY_COST_USD == 9.25
        stamp = datetime(2026, 1, 2, tzinfo=timezone.utc)
        server._COST_EPOCH = stamp
        assert cost._COST_EPOCH is stamp
        assert server._COST_EPOCH is stamp
    finally:
        cost._BOOT_CARRY_COST_USD = prior
        cost._COST_EPOCH = prior_epoch


def test_boot_meter_carry_is_shared_mutable():
    assert server._BOOT_METER_CARRY is cost._BOOT_METER_CARRY
    assert server._BOOT_REPOS is cost._BOOT_REPOS


def test_usage_cache_helpers_live_in_cost():
    assert server._usage_cache_get is cost._usage_cache_get
    assert server._usage_response_cache is cost._usage_response_cache
    server._usage_cache_clear_for_tests()
    server._usage_cache_put("peel-key", {"ok": True})
    # Under pytest the get path always misses (hermetic).
    assert server._usage_cache_get("peel-key") is None


def test_pure_job_cost_and_cache_savings():
    assert cost._job_cost(1_000_000, 1_000_000, 0, 1.0, 2.0) == 3.0
    assert cost._cache_savings(1_000_000, 10.0) == 9.0


def test_cost_source_label_estimated_vs_provider():
    est = SimpleNamespace(
        _provider_billed_tokens_in=0,
        _provider_billed_tokens_out=0,
        _plan_billing=False,
    )
    assert cost._cost_source_label(est) == "estimated"
    prov = SimpleNamespace(
        _provider_billed_tokens_in=10,
        _provider_billed_tokens_out=5,
        _tokens_in=10,
        _tokens_out=5,
        _worker_tokens_in=0,
        _worker_tokens_out=0,
    )
    assert cost._cost_source_label(prov) == "provider"


def test_sum_job_set_savings_aggregates(monkeypatch):
    # Patch on server: _sum_job_set_savings late-binds through harness.server
    # so historical monkeypatches keep working after the peel.
    monkeypatch.setattr(server, "_routing_saved_usd", lambda arts: 1.5)
    monkeypatch.setattr(server, "_cache_saved_usd_swarm", lambda arts, reg: 0.25)
    routing, cache = cost._sum_job_set_savings(
        ["a", "b"], lambda jid: [], registry=[]
    )
    assert routing == 3.0
    assert cache == 0.5
