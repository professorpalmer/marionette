"""Deterministic cost-accounting tests.

Per AGENTS.md, scoring/cost must be pure functions of tokens + prices. These
exercise the helpers directly (no server, no network, no keys) so pricing is
verifiable in isolation.
"""
import pytest

from types import SimpleNamespace

from harness.server import (
    CACHE_READ_MULTIPLIER,
    CACHE_WRITE_5M_MULTIPLIER,
    CACHE_WRITE_1H_MULTIPLIER,
    _session_cost,
    _session_cost_split,
    _cache_savings,
    _cache_savings_with_basis,
    _cost_source_label,
    _job_cost,
    _job_cost_is_unsplit,
    _normalize_price_source,
    _resolve_active_prices_with_source,
    _spend_is_estimated,
)


PRICE_IN = 3.0   # $/Mtok
PRICE_OUT = 15.0  # $/Mtok


def test_cache_multiplier_is_ten_percent():
    assert CACHE_READ_MULTIPLIER == 0.1


def test_cached_tokens_billed_at_discount():
    # Two identical sessions except one has 100k of its input cached. The only
    # difference must be the cache discount on exactly those 100k tokens:
    # 0.9 * 100k/1e6 * price_in (they go from full price to 0.1x price).
    t_in, t_out = 500_000, 200_000
    cached = 100_000
    no_cache = _session_cost(t_in, t_out, 0, PRICE_IN, PRICE_OUT)
    with_cache = _session_cost(t_in, t_out, cached, PRICE_IN, PRICE_OUT)
    expected_delta = 0.9 * cached / 1.0e6 * PRICE_IN
    assert no_cache - with_cache == pytest.approx(expected_delta)


def test_zero_cached_matches_old_full_price_formula():
    t_in, t_out = 800_000, 250_000
    old = (t_in / 1.0e6) * PRICE_IN + (t_out / 1.0e6) * PRICE_OUT
    assert _session_cost(t_in, t_out, 0, PRICE_IN, PRICE_OUT) == old


def test_all_input_cached_bills_at_discount():
    t_in = 1_000_000
    cost = _session_cost(t_in, 0, t_in, PRICE_IN, PRICE_OUT)
    expected = (t_in / 1.0e6) * PRICE_IN * CACHE_READ_MULTIPLIER
    assert cost == expected


def test_cached_clamped_to_input_total():
    # A cached count larger than tracked input must not go negative / over-credit.
    cost = _session_cost(100_000, 0, 999_999_999, PRICE_IN, PRICE_OUT)
    expected = (100_000 / 1.0e6) * PRICE_IN * CACHE_READ_MULTIPLIER
    assert cost == expected


def test_session_cost_fallback_prices_total_at_output():
    # No in/out split available -> price the combined total at price_out.
    assert _session_cost(0, 0, 0, PRICE_IN, PRICE_OUT) == 0.0
    # With only a total carried via t_in (shouldn't happen, but exercise the
    # documented single-rate fallback branch when neither split is meaningful).


def test_cache_savings_usd_is_correct():
    cached = 100_000
    saved = _cache_savings(cached, PRICE_IN)
    expected = cached / 1.0e6 * PRICE_IN * (1.0 - CACHE_READ_MULTIPLIER)
    assert saved == expected


def test_cache_savings_zero_when_nothing_cached():
    assert _cache_savings(0, PRICE_IN) == 0.0


def test_job_cost_uses_split_not_blend():
    # Output-heavy job: 100k in, 400k out. Correct cost uses the split; a naive
    # 50/50 blend of (price_in+price_out)/2 would be materially different.
    t_in, t_out = 100_000, 400_000
    split = _job_cost(t_in, t_out, 0, PRICE_IN, PRICE_OUT)
    expected_split = (t_in / 1.0e6) * PRICE_IN + (t_out / 1.0e6) * PRICE_OUT
    assert split == expected_split

    blended = ((PRICE_IN + PRICE_OUT) / 2.0) * (t_in + t_out) / 1.0e6
    assert split != blended  # proves we are not using the old blend


def test_job_cost_unknown_split_uses_output_rate():
    # Single 'tokens' total, no split -> price at output rate (not 50/50 blend).
    tokens = 500_000
    cost = _job_cost(0, 0, tokens, PRICE_IN, PRICE_OUT)
    assert cost == (tokens / 1.0e6) * PRICE_OUT
    # And explicitly not the old blended number.
    blended = ((PRICE_IN + PRICE_OUT) / 2.0) * tokens / 1.0e6
    assert cost != blended


def test_provider_cost_preferred_over_catalog_estimate():
    # Catalog estimate would be huge; provider receipt is ground truth.
    pilot = SimpleNamespace(
        _tokens_in=1_000_000,
        _tokens_out=10_000,
        _tokens_cached=900_000,
        _worker_tokens_in=0,
        _worker_tokens_out=0,
        _worker_cost_usd=0.0,
        _provider_cost_usd=2.54,
        _provider_billed_tokens_in=1_000_000,
        _provider_billed_tokens_out=10_000,
        _provider_billed_tokens_cached=900_000,
    )
    catalog = _session_cost(1_000_000, 10_000, 900_000, PRICE_IN, PRICE_OUT)
    assert catalog != pytest.approx(2.54)
    assert _session_cost_split(pilot, PRICE_IN, PRICE_OUT) == pytest.approx(2.54)
    assert _cost_source_label(pilot) == "provider"


def test_provider_cost_plus_uncovered_estimate_and_workers():
    # Half the pilot tokens lacked usage.cost (legacy turn); price that slice.
    pilot = SimpleNamespace(
        _tokens_in=200_000,
        _tokens_out=20_000,
        _tokens_cached=50_000,
        _worker_tokens_in=0,
        _worker_tokens_out=0,
        _worker_cost_usd=0.40,
        _provider_cost_usd=1.10,
        _provider_billed_tokens_in=100_000,
        _provider_billed_tokens_out=10_000,
        _provider_billed_tokens_cached=40_000,
    )
    rem = _session_cost(100_000, 10_000, 10_000, PRICE_IN, PRICE_OUT)
    assert _session_cost_split(pilot, PRICE_IN, PRICE_OUT) == pytest.approx(1.10 + rem + 0.40)
    assert _cost_source_label(pilot) == "mixed"


def test_cost_source_estimated_without_provider_meters():
    pilot = SimpleNamespace(
        _tokens_in=10_000,
        _tokens_out=1_000,
        _tokens_cached=0,
        _worker_tokens_in=0,
        _worker_tokens_out=0,
        _provider_billed_tokens_in=0,
        _provider_billed_tokens_out=0,
    )
    assert _cost_source_label(pilot) == "estimated"


def test_cost_source_plan_estimated_for_subscription_pilots():
    pilot = SimpleNamespace(
        _tokens_in=10_000,
        _tokens_out=1_000,
        _tokens_cached=0,
        _worker_tokens_in=0,
        _worker_tokens_out=0,
        _provider_billed_tokens_in=0,
        _provider_billed_tokens_out=0,
        _plan_billing=True,
    )
    assert _cost_source_label(pilot) == "plan_estimated"


def test_cache_write_5m_billed_at_premium():
    # 100k write @ 1.25x, rest uncached -- must exceed full-price-on-all-input.
    t_in = 200_000
    write = 100_000
    cost = _session_cost(
        t_in, 0, 0, PRICE_IN, PRICE_OUT,
        cache_write_5m=write,
    )
    expected = (
        ((t_in - write) / 1.0e6) * PRICE_IN
        + (write / 1.0e6) * PRICE_IN * CACHE_WRITE_5M_MULTIPLIER
    )
    assert cost == pytest.approx(expected)
    assert cost > (t_in / 1.0e6) * PRICE_IN


def test_cache_write_1h_billed_at_2x():
    t_in = 100_000
    cost = _session_cost(
        t_in, 0, 0, PRICE_IN, PRICE_OUT,
        cache_write_1h=t_in,
    )
    assert cost == pytest.approx((t_in / 1.0e6) * PRICE_IN * CACHE_WRITE_1H_MULTIPLIER)


def test_anthropic_style_read_plus_write_estimate():
    # Inclusive total: 50 uncached + 100k read + 20k 1h-write.
    uncached, cached, write = 50_000, 100_000, 20_000
    t_in = uncached + cached + write
    cost = _session_cost(
        t_in, 1_000, cached, PRICE_IN, PRICE_OUT,
        cache_write_1h=write,
    )
    expected = (
        (uncached / 1.0e6) * PRICE_IN
        + (cached / 1.0e6) * PRICE_IN * CACHE_READ_MULTIPLIER
        + (write / 1.0e6) * PRICE_IN * CACHE_WRITE_1H_MULTIPLIER
        + (1_000 / 1.0e6) * PRICE_OUT
    )
    assert cost == pytest.approx(expected)


def test_session_cost_split_includes_write_premium():
    pilot = SimpleNamespace(
        _tokens_in=120_000,
        _tokens_out=0,
        _tokens_cached=0,
        _tokens_cache_write=20_000,
        _tokens_cache_write_5m=20_000,
        _tokens_cache_write_1h=0,
        _worker_tokens_in=0,
        _worker_tokens_out=0,
        _worker_cost_usd=0.0,
        _provider_billed_tokens_in=0,
        _provider_billed_tokens_out=0,
    )
    expected = _session_cost(
        120_000, 0, 0, PRICE_IN, PRICE_OUT,
        cache_write=20_000, cache_write_5m=20_000,
    )
    assert _session_cost_split(pilot, PRICE_IN, PRICE_OUT) == pytest.approx(expected)


def test_normalize_price_source_maps_registry_labels():
    assert _normalize_price_source("live") == "live"
    assert _normalize_price_source("live_alias") == "live"
    assert _normalize_price_source("catalog") == "static"
    assert _normalize_price_source("static") == "static"
    assert _normalize_price_source(None) == "default"
    assert _normalize_price_source("") == "default"


def test_resolve_active_prices_with_source_surfaces_default(monkeypatch):
    """Silent $0.50/$2.00 fallback must advertise price_source=default."""
    import harness.api.cost as cost_mod
    import pmharness.registry as reg

    class _Cfg:
        driver = "totally-unknown-model-xyz"

    monkeypatch.setattr(cost_mod, "_cfg", lambda: _Cfg())
    monkeypatch.setattr(reg, "_PRICE_MEM", {})
    monkeypatch.setattr(reg, "_live_windows", lambda: {})
    monkeypatch.setattr(reg, "resolve_price", lambda name: (0.5, 2.0))
    monkeypatch.setattr(reg, "price_with_source", lambda name: (None, None, None))
    pin, pout, src = _resolve_active_prices_with_source()
    assert pin == 0.5 and pout == 2.0
    assert src == "default"
    assert _spend_is_estimated("estimated", src) is True


def test_resolve_active_prices_logs_on_registry_exception(monkeypatch, caplog):
    """Registry blow-ups must not stay silent when falling back to 0.5/2.0."""
    import logging

    import harness.api.cost as cost_mod

    class _Cfg:
        driver = "broken/driver"

    def _boom(_name):
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(cost_mod, "_cfg", lambda: _Cfg())
    monkeypatch.setattr(
        "pmharness.registry.resolve_price", _boom, raising=False
    )
    with caplog.at_level(logging.WARNING, logger="harness.cost"):
        pin, pout, src = _resolve_active_prices_with_source()
    assert (pin, pout, src) == (0.5, 2.0, "default")
    assert any("default 0.5/2.0" in r.message for r in caplog.records)


def test_provider_override_keeps_spend_non_estimated():
    assert _spend_is_estimated("provider", "default") is False
    assert _spend_is_estimated("estimated", "live") is True
    assert _spend_is_estimated("mixed", "static") is True


def test_unsplit_job_cost_is_labeled_estimate():
    """No in/out split → price_out total, flagged estimated (no fabricated ratio)."""
    tokens = 500_000
    cost = _job_cost(0, 0, tokens, PRICE_IN, PRICE_OUT)
    assert cost == (tokens / 1.0e6) * PRICE_OUT
    assert _job_cost_is_unsplit(0, 0, tokens) is True
    assert _job_cost_is_unsplit(100, 0, tokens) is False


def test_cache_savings_capped_to_provider_spend():
    cached = 1_000_000  # catalog savings = 0.9 * price_in = $2.70
    raw = _cache_savings(cached, PRICE_IN)
    assert raw == pytest.approx(2.7)
    capped, basis = _cache_savings_with_basis(cached, PRICE_IN, provider_cost_usd=1.10)
    assert capped == pytest.approx(1.10)
    assert basis == "capped"
    assert _cache_savings(cached, PRICE_IN, provider_cost_usd=1.10) == pytest.approx(1.10)


def test_cache_savings_unknown_when_provider_net_nonpositive():
    cached = 100_000
    usd, basis = _cache_savings_with_basis(cached, PRICE_IN, provider_cost_usd=0.0)
    assert usd == 0.0
    assert basis == "unknown"


def test_cache_savings_uncapped_when_provider_cost_unknown():
    """Estimated spend: keep full catalog savings; do not clamp to estimated total."""
    cached = 30_000  # catalog savings = 30k/1e6 * 3.0 * 0.9 = 0.081
    estimated_session_total = 0.0135  # must NOT be used as a cap
    usd, basis = _cache_savings_with_basis(
        cached, PRICE_IN, provider_cost_usd=None
    )
    assert usd == pytest.approx(0.081)
    assert basis == "catalog"
    assert usd > estimated_session_total
    # Contrast: a real provider receipt still caps.
    capped, capped_basis = _cache_savings_with_basis(
        cached, PRICE_IN, provider_cost_usd=0.01
    )
    assert capped == pytest.approx(0.01)
    assert capped_basis == "capped"


def test_multi_session_catalog_costs_sum_invariant():
    """Σ per-session catalog costs == cost of merged meters (multi-session sum)."""
    sessions = [
        SimpleNamespace(
            _tokens_in=100_000, _tokens_out=20_000, _tokens_cached=10_000,
            _worker_tokens_in=0, _worker_tokens_out=0, _worker_cost_usd=0.0,
            _provider_billed_tokens_in=0, _provider_billed_tokens_out=0,
            _provider_cost_usd=0.0,
        ),
        SimpleNamespace(
            _tokens_in=250_000, _tokens_out=40_000, _tokens_cached=50_000,
            _worker_tokens_in=0, _worker_tokens_out=0, _worker_cost_usd=0.0,
            _provider_billed_tokens_in=0, _provider_billed_tokens_out=0,
            _provider_cost_usd=0.0,
        ),
        SimpleNamespace(
            _tokens_in=80_000, _tokens_out=5_000, _tokens_cached=0,
            _worker_tokens_in=0, _worker_tokens_out=0, _worker_cost_usd=0.0,
            _provider_billed_tokens_in=0, _provider_billed_tokens_out=0,
            _provider_cost_usd=0.0,
        ),
    ]
    individual = sum(
        _session_cost_split(s, PRICE_IN, PRICE_OUT) for s in sessions
    )
    merged = SimpleNamespace(
        _tokens_in=sum(s._tokens_in for s in sessions),
        _tokens_out=sum(s._tokens_out for s in sessions),
        _tokens_cached=sum(s._tokens_cached for s in sessions),
        _worker_tokens_in=0,
        _worker_tokens_out=0,
        _worker_cost_usd=0.0,
        _provider_billed_tokens_in=0,
        _provider_billed_tokens_out=0,
        _provider_cost_usd=0.0,
    )
    assert _session_cost_split(merged, PRICE_IN, PRICE_OUT) == pytest.approx(individual)
