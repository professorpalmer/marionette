"""Characterization: cost.py facade split into accounting / meters / swarm."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import harness.api.cost as cost
import harness.api.cost_accounting as accounting
import harness.api.swarm_cost as swarm
import harness.api.usage_meters as meters
import harness.server as server


def test_accounting_module_owns_pure_pricing_helpers():
    assert accounting._session_cost is cost._session_cost
    assert accounting._job_cost is cost._job_cost
    assert accounting._cache_savings is cost._cache_savings
    assert accounting._cost_source_label is cost._cost_source_label
    assert accounting.CACHE_READ_MULTIPLIER is cost.CACHE_READ_MULTIPLIER
    assert accounting._session_cost(1_000_000, 0, 0, 3.0, 15.0) == 3.0
    assert accounting._job_cost(0, 0, 1_000_000, 1.0, 2.0) == 2.0


def test_usage_meters_module_owns_boot_state_and_cache():
    assert meters._boot_usage_meters is cost._boot_usage_meters
    assert meters._persist_boot_usage is cost._persist_boot_usage
    assert meters._usage_cache_get is cost._usage_cache_get
    assert meters._BOOT_METER_CARRY is cost._BOOT_METER_CARRY
    assert meters._BOOT_REPOS is cost._BOOT_REPOS
    assert server._BOOT_METER_CARRY is meters._BOOT_METER_CARRY


def test_swarm_module_owns_job_accounting_helpers():
    assert swarm._job_swarm_accounting is cost._job_swarm_accounting
    assert swarm._sum_job_set_savings is cost._sum_job_set_savings
    assert swarm._routing_saved_usd is cost._routing_saved_usd
    assert swarm._cache_saved_usd_swarm is cost._cache_saved_usd_swarm
    assert swarm._COST_OPTIMIZING_POLICIES is cost._COST_OPTIMIZING_POLICIES


def test_boot_scalars_write_through_facade_to_usage_meters():
    prior = meters._BOOT_CARRY_COST_USD
    prior_epoch = meters._COST_EPOCH
    try:
        cost._BOOT_CARRY_COST_USD = 4.5
        assert meters._BOOT_CARRY_COST_USD == 4.5
        assert server._BOOT_CARRY_COST_USD == 4.5
        stamp = datetime(2026, 7, 17, tzinfo=timezone.utc)
        server._COST_EPOCH = stamp
        assert meters._COST_EPOCH is stamp
        assert cost._COST_EPOCH is stamp
    finally:
        meters._BOOT_CARRY_COST_USD = prior
        meters._COST_EPOCH = prior_epoch


def test_job_in_cost_window_uses_meters_epoch():
    prior = meters._COST_EPOCH
    try:
        meters._COST_EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)
        assert meters._job_in_cost_window("2026-06-01T00:00:00+00:00") is True
        assert meters._job_in_cost_window("2025-01-01T00:00:00+00:00") is False
        assert cost._job_in_cost_window is meters._job_in_cost_window
    finally:
        meters._COST_EPOCH = prior


def test_session_cost_split_still_prices_provider_slice():
    pilot = SimpleNamespace(
        _tokens_in=1000,
        _tokens_out=100,
        _tokens_cached=0,
        _tokens_cache_write=0,
        _tokens_cache_write_5m=0,
        _tokens_cache_write_1h=0,
        _worker_tokens_in=0,
        _worker_tokens_out=0,
        _worker_cost_usd=0.0,
        _provider_cost_usd=1.25,
        _provider_billed_tokens_in=1000,
        _provider_billed_tokens_out=100,
        _provider_billed_tokens_cached=0,
        _provider_billed_tokens_cache_write=0,
        _provider_billed_tokens_cache_write_5m=0,
        _provider_billed_tokens_cache_write_1h=0,
    )
    assert accounting._session_cost_split(pilot, 3.0, 15.0) == 1.25
