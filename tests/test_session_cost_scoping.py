"""Session cost must not double-bill swarm jobs or bill jobs from past runs.

Two regressions behind the wildly inflated status-bar cost:

1. Awaited swarm-store jobs had their dollars attributed TWICE: once folded
   into the pilot's _worker_cost_usd (at the PILOT's model rate, since
   resolve_price cannot price adapter names like 'agentic') and once in
   /api/usage's swarm_cost (the authoritative usage x registry pricing).

2. /api/usage summed est_cost_usd over EVERY job in the swarm store, which is
   persistent SQLite -- so the "session" figure quietly accumulated the whole
   state dir's history across app launches.
"""
from __future__ import annotations

from datetime import timedelta

import harness.server as server
from harness.config import HarnessConfig
from harness.conversation import ConversationalSession


def _session() -> ConversationalSession:
    return ConversationalSession(HarnessConfig())


def test_swarm_store_artifacts_add_no_worker_dollars():
    """Draining a store-backed job's artifacts records the token split (so the
    pilot-priced portion excludes them) but attributes ZERO dollars -- the
    job's dollars come from /api/usage swarm_cost, priced at the model the
    worker actually ran on."""
    s = _session()
    s._add_worker_tokens_from_artifacts([
        {"task_id": "t1", "tokens_in": 500_000, "tokens_out": 100_000},
    ])
    assert s._worker_tokens_in == 500_000
    assert s._worker_tokens_out == 100_000
    assert s._worker_cost_usd == 0.0


def test_attribute_worker_cost_dollars_still_counted_for_local_workers():
    """Local provider workers are NOT in the swarm store, so their dollars must
    keep flowing into _worker_cost_usd (the default path)."""
    s = _session()
    s._attribute_worker_cost(10_000, 5_000, real_cost_usd=1.25)
    assert s._worker_cost_usd == 1.25
    assert s._worker_tokens_in == 10_000
    assert s._worker_tokens_out == 5_000


def test_attribute_worker_cost_count_dollars_false_records_split_only():
    s = _session()
    s._attribute_worker_cost(7, 3, real_cost_usd=9.99, count_dollars=False)
    assert s._worker_cost_usd == 0.0
    assert s._worker_tokens_in == 7
    assert s._worker_tokens_out == 3


def test_job_cost_window_excludes_prior_run_jobs():
    old = (server._COST_EPOCH - timedelta(days=2)).isoformat(timespec="seconds")
    assert server._job_in_cost_window(old) is False


def test_job_cost_window_includes_this_run_jobs():
    # Read the epoch at call time: importing harness.server elsewhere in the
    # suite may predate this test module's import by minutes.
    fresh = (server._COST_EPOCH + timedelta(seconds=5)).isoformat(
        timespec="seconds")
    assert server._job_in_cost_window(fresh) is True


def test_job_cost_window_keeps_unknown_timestamps():
    """A job without a parseable created_at must stay visible in the cost sum
    rather than silently dropping live spend."""
    assert server._job_in_cost_window(None) is True
    assert server._job_in_cost_window("") is True
    assert server._job_in_cost_window("not-a-date") is True


def _session_store_with_active(tmp_path, cost: float = 0.0):
    from harness.sessions import SessionStore

    store = SessionStore(str(tmp_path / "harness_sessions.json"))
    row = store.create(title="budget session")
    if cost:
        store.accumulate_meters(row["id"], estimated_cost_usd=cost)
    return store, row["id"]


def test_active_session_total_combines_meters_and_stamped_jobs(tmp_path, monkeypatch):
    """The lifetime figure = persisted session meters + all-time swarm dollars
    for jobs stamped with this session -- the two sources are disjoint by
    design (store-job dollars never enter the persisted meters), so adding
    them cannot double-bill."""
    store, sid = _session_store_with_active(tmp_path, cost=1.5)
    monkeypatch.setattr(server, "_sessions", store)
    monkeypatch.setattr(
        server, "_job_swarm_accounting", lambda arts, registry: (100, 0.25)
    )
    total = server._active_session_total(
        ["job_a", "job_b"], lambda jid: [], registry=[]
    )
    assert total is not None
    assert total["session_id"] == sid
    assert total["est_cost_usd"] == 2.0  # 1.5 persisted + 2 x 0.25 swarm


def test_active_session_total_none_without_active_session(tmp_path, monkeypatch):
    from harness.sessions import SessionStore

    store = SessionStore(str(tmp_path / "harness_sessions.json"))
    monkeypatch.setattr(server, "_sessions", store)
    assert server._active_session_total([], lambda jid: [], registry=[]) is None


def test_active_session_total_survives_priced_job_errors(tmp_path, monkeypatch):
    """One unpriceable job must degrade to its exclusion, not sink the total."""
    store, sid = _session_store_with_active(tmp_path, cost=0.75)
    monkeypatch.setattr(server, "_sessions", store)

    def _boom(arts, registry):
        raise RuntimeError("bad artifacts")

    monkeypatch.setattr(server, "_job_swarm_accounting", _boom)
    total = server._active_session_total(["job_a"], lambda jid: [], registry=[])
    assert total is not None
    assert total["est_cost_usd"] == 0.75
