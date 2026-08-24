"""Hermetic collab web presence: heartbeat, list, expire stale."""

from __future__ import annotations

from harness.api.collab_presence import get_presence, post_presence_heartbeat
from harness.collab_presence import STORE, DEFAULT_TTL_SECONDS


def setup_function(_fn):
    STORE.reset_for_tests()


def test_heartbeat_then_list_returns_id_label_last_seen():
    clock = {"t": 1_700_000_000.0}

    def now():
        return clock["t"]

    peer = STORE.heartbeat("sess-a", "p1", "Cary", now=now)
    assert peer == {"id": "p1", "label": "Cary", "last_seen": clock["t"]}
    listed = STORE.list_peers("sess-a", now=now)
    assert listed == [peer]


def test_second_heartbeat_upserts_last_seen():
    t = {"n": 10.0}
    STORE.heartbeat("sess-a", "p1", "Cary", now=lambda: t["n"])
    t["n"] = 20.0
    STORE.heartbeat("sess-a", "p1", "Cary Palmer", now=lambda: t["n"])
    peers = STORE.list_peers("sess-a", now=lambda: t["n"])
    assert len(peers) == 1
    assert peers[0]["label"] == "Cary Palmer"
    assert peers[0]["last_seen"] == 20.0


def test_expire_stale_drops_old_peers_keeps_fresh():
    now = lambda: 100.0
    STORE.heartbeat("sess-a", "stale", "Old", now=lambda: 10.0)
    STORE.heartbeat("sess-a", "fresh", "New", now=now)
    dropped = STORE.expire_stale("sess-a", now=now, ttl=45.0)
    assert dropped == 1
    peers = STORE.list_peers("sess-a", now=now, ttl=45.0)
    assert [p["id"] for p in peers] == ["fresh"]


def test_sessions_are_isolated():
    STORE.heartbeat("alpha", "p1", "A", now=lambda: 1.0)
    STORE.heartbeat("beta", "p2", "B", now=lambda: 1.0)
    assert [p["id"] for p in STORE.list_peers("alpha", now=lambda: 1.0)] == ["p1"]
    assert [p["id"] for p in STORE.list_peers("beta", now=lambda: 1.0)] == ["p2"]


def test_http_heartbeat_and_get_round_trip():
    status, payload = post_presence_heartbeat(
        {"session_id": "sess-web", "id": "u1", "label": "Cary"}
    )
    assert status == 200
    assert payload["ok"] is True
    assert payload["peer"]["id"] == "u1"
    assert payload["peer"]["label"] == "Cary"
    assert payload["ttl"] == DEFAULT_TTL_SECONDS
    st, listed = get_presence({"session_id": "sess-web"})
    assert st == 200
    assert listed["session_id"] == "sess-web"
    assert len(listed["peers"]) == 1
    assert listed["peers"][0]["id"] == "u1"


def test_http_rejects_missing_ids():
    st, body = post_presence_heartbeat({"label": "nope"})
    assert st == 400
    assert "session_id" in body["error"]
    st, body = post_presence_heartbeat({"session_id": "s"})
    assert st == 400
    assert body["error"] == "id is required"
    st, body = get_presence({})
    assert st == 400
