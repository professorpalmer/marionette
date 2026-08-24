"""Hermetic metaharness scoring and leader-election tests."""
from __future__ import annotations

import pytest

from harness.api import metaharness as api
from harness import metaharness as mh


@pytest.fixture(autouse=True)
def _fresh_latch():
    mh.reset_latch()
    yield
    mh.reset_latch()


def test_highest_score_wins():
    latch = mh.LeaderLatch(ttl_seconds=30, clock=lambda: 0.0)
    latch.report_score("b", 1.0)
    latch.report_score("a", 2.5)
    snap = latch.status()
    assert snap["elected"] is True
    assert snap["leader"] == {"id": "a", "score": 2.5}
    assert [p["id"] for p in snap["peers"]] == ["a", "b"]


def test_tie_breaks_stably_by_id():
    latch = mh.LeaderLatch(ttl_seconds=30, clock=lambda: 0.0)
    latch.report_score("zeta", 10)
    latch.report_score("alpha", 10)
    latch.report_score("mu", 10)
    assert latch.status()["leader"]["id"] == "alpha"
    # Re-report in a different order; leader must stay alpha.
    latch.report_score("mu", 10)
    latch.report_score("zeta", 10)
    assert latch.status()["leader"]["id"] == "alpha"


def test_stale_peer_loses_election():
    now = [0.0]

    def clock() -> float:
        return now[0]

    latch = mh.LeaderLatch(ttl_seconds=5, clock=clock)
    latch.report_score("old", 99)
    now[0] = 6.0
    latch.report_score("new", 1)
    snap = latch.status()
    assert snap["leader"]["id"] == "new"
    assert [p["id"] for p in snap["peers"]] == ["new"]


def test_score_value_rejects_non_finite():
    with pytest.raises(mh.ScoreError):
        mh.score_value(float("nan"))
    with pytest.raises(mh.ScoreError):
        mh.score_value(float("inf"))
    with pytest.raises(mh.ScoreError):
        mh.score_value("nope")
    with pytest.raises(mh.ScoreError):
        mh.score_value(True)


def test_derived_score_from_ok_and_latency():
    good = mh.score_report({"ok": True, "latency_ms": 0})
    slow = mh.score_report({"success": True, "latency_ms": 100})
    bad = mh.score_report({"ok": False, "latency_ms": 0})
    assert good > slow > bad
    assert good == pytest.approx(2.0)


def test_api_score_heartbeat_and_status():
    st, body = api.get_metaharness_status()
    assert st == 200
    assert body["leader"] is None
    assert body["peers"] == []

    st, body = api.post_metaharness_score({"peer_id": "p2", "score": 3})
    assert st == 200
    assert body["leader"]["id"] == "p2"

    st, body = api.post_metaharness_score({"id": "p1", "score": 3})
    assert st == 200
    assert body["leader"]["id"] == "p1"  # same score, smaller id

    st, body = api.post_metaharness_heartbeat({"peer_id": "p2"})
    assert st == 200
    assert body["leader"]["id"] == "p1"
    assert {p["id"] for p in body["peers"]} == {"p1", "p2"}

    st, status = api.get_metaharness_status()
    assert st == 200
    assert status["leader"]["id"] == "p1"


def test_api_rejects_bad_payloads():
    st, body = api.post_metaharness_score({"peer_id": "x"})
    assert st == 400
    assert body["ok"] is False

    st, body = api.post_metaharness_heartbeat({"peer_id": "ghost"})
    assert st == 400

    st, body = api.post_metaharness_score({"peer_id": "", "score": 1})
    assert st == 400


class _Svc:
    def __getattr__(self, name):
        return lambda: None


def test_routes_are_wired():
    import harness.http_routes as http_routes

    svc = _Svc()
    post = http_routes.build_post_json_routes(svc)
    get = http_routes.build_get_routes(svc)
    assert "/api/metaharness/status" in get
    assert "/api/metaharness/score" in post
    assert "/api/metaharness/heartbeat" in post
    assert callable(get["/api/metaharness/status"])
    assert callable(post["/api/metaharness/score"])
    assert callable(post["/api/metaharness/heartbeat"])
