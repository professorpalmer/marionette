"""HTTP handlers for collab web presence."""

from __future__ import annotations

from typing import Any, Union

from harness.collab_presence import STORE, DEFAULT_TTL_SECONDS

JsonPayload = Union[dict, list]


def _qs_get(qs: dict, key: str) -> str:
    val = qs.get(key, "")
    if isinstance(val, list):
        return str(val[0] if val else "")
    return str(val or "")


def _peer_fields(body: dict) -> tuple[str, str, str]:
    session_id = str(body.get("session_id") or "").strip()
    peer_id = str(body.get("id") or body.get("peer_id") or "").strip()
    label = str(body.get("label") or "").strip()
    return session_id, peer_id, label


def post_presence_heartbeat(body: dict) -> tuple[int, JsonPayload]:
    """POST /api/collab/presence/heartbeat -- upsert last_seen for a peer."""
    session_id, peer_id, label = _peer_fields(body or {})
    try:
        peer = STORE.heartbeat(session_id, peer_id, label)
    except ValueError as exc:
        return 400, {"error": str(exc)}
    return 200, {
        "ok": True,
        "session_id": session_id,
        "peer": peer,
        "ttl": DEFAULT_TTL_SECONDS,
    }


def get_presence(qs: dict) -> tuple[int, JsonPayload]:
    """GET /api/collab/presence -- live peers for a session (stale expired)."""
    session_id = _qs_get(qs or {}, "session_id").strip()
    try:
        peers = STORE.list_peers(session_id)
    except ValueError as exc:
        return 400, {"error": str(exc)}
    return 200, {
        "session_id": session_id,
        "peers": peers,
        "ttl": DEFAULT_TTL_SECONDS,
    }
