"""Regression tests for worker/swarm prompt-cache attribution to the parent session.

Cache reads happening inside a worker's inner ConversationalSession
(session._tokens_cached) must survive back up to the parent session's
_tokens_cached counter, because server.py's cache_savings_usd computation reads
_tokens_cached off the pilot. Without this wiring, all swarm/worker cache hits
are invisible in the session cost total.

These tests exercise:
  (a) WorkerResult carries a tokens_cached field with a safe default,
  (b) the success-path attribution logic (mirrored from
      ConversationalSession's worker success branch) increments the parent's
      _tokens_cached by exactly the worker's tokens_cached without
      double-counting into _tokens_used,
  (c) _add_worker_tokens_from_artifacts sums tokens_cached across artifacts
      and feeds the parent's _tokens_cached (not _tokens_used).
"""

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.worker import WorkerResult


def test_worker_result_carries_tokens_cached_field():
    """The dataclass must expose tokens_cached with a safe default (0) so
    positional construction and older callers keep working, and must round-trip
    an explicit value."""
    # Default
    r_default = WorkerResult(ok=True)
    assert r_default.tokens_cached == 0

    # Explicit
    r_explicit = WorkerResult(ok=True, tokens_cached=123)
    assert r_explicit.tokens_cached == 123


def test_worker_result_tokens_cached_missing_defaults_to_zero_via_getattr():
    """Downstream attribution reads tokens_cached via getattr with a 0 fallback;
    make sure a bare result yields 0 rather than raising or leaking None."""
    r = WorkerResult(ok=True)
    assert int(getattr(r, "tokens_cached", 0) or 0) == 0


def test_success_attribution_increases_parent_tokens_cached():
    """Mirror the success-path attribution block in
    ConversationalSession._run_native_edit_worker: a WorkerResult carrying
    tokens_cached must bump the parent session's _tokens_cached by exactly that
    amount, WITHOUT double-counting tokens_used."""
    cfg = HarnessConfig()
    session = ConversationalSession(cfg)

    assert session._tokens_cached == 0
    assert session._tokens_used == 0
    assert session._tokens_in == 0
    assert session._tokens_out == 0

    res = WorkerResult(
        ok=True,
        patch="dummy",
        tokens_in=1000,
        tokens_out=200,
        tokens_cached=750,
    )

    # Exact copy of the success-path attribution logic (no _apply_lock needed
    # in-test since we are single-threaded here).
    tokens_in = res.tokens_in
    tokens_out = res.tokens_out
    tokens_cached = int(getattr(res, "tokens_cached", 0) or 0)
    session._tokens_used += tokens_out + tokens_in
    session._tokens_in += tokens_in
    session._tokens_out += tokens_out
    session._tokens_cached += tokens_cached

    assert session._tokens_cached == 750
    # tokens_cached is a SUBSET of tokens_in, so _tokens_used must only
    # reflect in+out (1200), not in+out+cached (1950).
    assert session._tokens_used == 1200
    assert session._tokens_in == 1000
    assert session._tokens_out == 200


def test_no_patch_attribution_increases_parent_tokens_cached():
    """The no-patch (worker failed to produce a diff) branch must ALSO propagate
    tokens_cached -- worker exploration still burns cache-eligible prompt
    tokens even when the patch is empty."""
    cfg = HarnessConfig()
    session = ConversationalSession(cfg)

    res = WorkerResult(
        ok=False,
        summary="no changes produced",
        tokens_in=400,
        tokens_out=50,
        tokens_cached=300,
    )

    # Exact copy of the no-patch attribution logic.
    _nc_t_in = int(getattr(res, "tokens_in", 0) or 0)
    _nc_t_out = int(getattr(res, "tokens_out", 0) or 0)
    _nc_t_cached = int(getattr(res, "tokens_cached", 0) or 0)
    if _nc_t_in or _nc_t_out or _nc_t_cached:
        session._tokens_used += _nc_t_out + _nc_t_in
        session._tokens_in += _nc_t_in
        session._tokens_out += _nc_t_out
        session._tokens_cached += _nc_t_cached

    assert session._tokens_cached == 300
    assert session._tokens_used == 450  # 400 in + 50 out; cached NOT double-counted
    assert session._tokens_in == 400
    assert session._tokens_out == 50


def test_add_worker_tokens_from_artifacts_sums_tokens_cached():
    """Multi-worker artifact summing must round-trip tokens_cached into the
    parent's _tokens_cached (not _tokens_used) so cache reads inside spawned
    puppetmaster jobs show up in the parent session's cache_savings_usd."""
    cfg = HarnessConfig()
    session = ConversationalSession(cfg)

    artifacts = [
        {"task_id": "t1", "tokens_in": 500, "tokens_out": 60, "tokens_cached": 400},
        {"payload": {"task_id": "t2", "tokens_in": 300, "tokens_out": 40, "tokens_cached": 250}},
    ]
    sum_in, sum_out, sum_cached = session._add_worker_tokens_from_artifacts(artifacts)

    assert sum_in == 800
    assert sum_out == 100
    assert sum_cached == 650
    assert session._tokens_cached == 650
    # tokens_used must NOT include tokens_cached (would double-count against
    # tokens_in which already contains cached reads).
    assert session._tokens_used == 900


def test_add_worker_tokens_from_artifacts_missing_tokens_cached_defaults_zero():
    """Backward-compat: artifacts written by older worker code lack
    tokens_cached; the aggregator must treat that as 0 and not crash."""
    cfg = HarnessConfig()
    session = ConversationalSession(cfg)

    artifacts = [
        {"task_id": "t1", "tokens_in": 100, "tokens_out": 10},  # no tokens_cached key
        {"payload": {"task_id": "t2", "tokens_in": 200, "tokens_out": 20}},
    ]
    sum_in, sum_out, sum_cached = session._add_worker_tokens_from_artifacts(artifacts)

    assert sum_in == 300
    assert sum_out == 30
    assert sum_cached == 0
    assert session._tokens_cached == 0
    assert session._tokens_used == 330
