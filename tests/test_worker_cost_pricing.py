"""Worker cost is priced at the worker's OWN model rate and added to the session
total, not repriced at the (possibly cheaper) pilot rate. Hermetic."""
from __future__ import annotations

import types

from harness.server import _session_cost, _session_cost_split


def _fake_pilot(**kw):
    p = types.SimpleNamespace()
    p._tokens_in = kw.get("t_in", 0)
    p._tokens_out = kw.get("t_out", 0)
    p._tokens_cached = kw.get("t_cached", 0)
    p._worker_tokens_in = kw.get("w_in", 0)
    p._worker_tokens_out = kw.get("w_out", 0)
    p._worker_cost_usd = kw.get("w_cost", 0.0)
    return p


# Pilot rate: cheap model (qwen-ish) 0.1 / 0.3 per Mtok.
PILOT_IN, PILOT_OUT = 0.1, 0.3


def test_worker_dollars_added_on_top_of_pilot_tokens():
    # 100k pilot tokens (50k in / 50k out) + a worker that spent $9.54 on opus,
    # whose 300k in / 80k out tokens are ALSO in the grand meters.
    p = _fake_pilot(
        t_in=50_000 + 300_000, t_out=50_000 + 80_000, t_cached=0,
        w_in=300_000, w_out=80_000, w_cost=9.5404,
    )
    total = _session_cost_split(p, PILOT_IN, PILOT_OUT)
    # Pilot portion prices ONLY the 50k/50k at pilot rate; worker dollars added.
    pilot_only = _session_cost(50_000, 50_000, 0, PILOT_IN, PILOT_OUT)
    assert abs(total - (pilot_only + 9.5404)) < 1e-9
    # And the total is dominated by the real worker cost, not the cheap reprice.
    assert total > 9.5


def test_no_double_count_worker_tokens_not_priced_at_pilot_rate():
    # If worker tokens WERE repriced at pilot rate they'd add ~ (300k*.1+80k*.3)/1e6
    # = $0.054. We must NOT see that on top of the $9.54.
    p = _fake_pilot(
        t_in=300_000, t_out=80_000, t_cached=0,
        w_in=300_000, w_out=80_000, w_cost=9.5404,
    )
    total = _session_cost_split(p, PILOT_IN, PILOT_OUT)
    # Pilot tokens = grand - worker = 0, so total is exactly the worker dollars.
    assert abs(total - 9.5404) < 1e-9


def test_no_worker_spend_matches_old_number():
    # A session with no worker attribution must equal the plain pilot pricing.
    p = _fake_pilot(t_in=120_000, t_out=40_000, t_cached=10_000)
    total = _session_cost_split(p, PILOT_IN, PILOT_OUT)
    old = _session_cost(120_000, 40_000, 10_000, PILOT_IN, PILOT_OUT)
    assert abs(total - old) < 1e-12


def test_missing_worker_attrs_defaults_safely():
    # An object lacking the new attrs (old session) behaves exactly as today.
    p = types.SimpleNamespace(_tokens_in=10_000, _tokens_out=5_000, _tokens_cached=0)
    total = _session_cost_split(p, PILOT_IN, PILOT_OUT)
    old = _session_cost(10_000, 5_000, 0, PILOT_IN, PILOT_OUT)
    assert abs(total - old) < 1e-12


def test_worker_tokens_exceeding_grand_floors_at_zero():
    # Defensive: worker split larger than grand meters must not go negative.
    p = _fake_pilot(t_in=1000, t_out=1000, w_in=5000, w_out=5000, w_cost=2.0)
    total = _session_cost_split(p, PILOT_IN, PILOT_OUT)
    assert total == 2.0  # pilot portion floored to 0, only worker dollars remain
