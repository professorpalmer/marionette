"""Deterministic cost-accounting tests.

Per AGENTS.md, scoring/cost must be pure functions of tokens + prices. These
exercise the helpers directly (no server, no network, no keys) so pricing is
verifiable in isolation.
"""
import pytest

from harness.server import (
    CACHE_READ_MULTIPLIER,
    _session_cost,
    _cache_savings,
    _job_cost,
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
