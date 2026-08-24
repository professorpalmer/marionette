"""In-process collab web presence: who is in a session.

Stdlib only. Heartbeats refresh last_seen; list expires stale peers.
No marketplace, Sentry/Otel, or Guardian. No live-cursor / share layer
existed on origin/main -- this is the first presence store.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

DEFAULT_TTL_SECONDS = 45.0

NowFn = Callable[[], float]


def _now() -> float:
    return time.time()


def _norm(value: Any) -> str:
    return str(value or "").strip()


class PresenceStore:
    """Thread-safe per-session peer map keyed by peer id."""

    def __init__(self, ttl_seconds: float = DEFAULT_TTL_SECONDS):
        self.ttl_seconds = float(ttl_seconds)
        self._lock = threading.Lock()
        self._sessions: dict[str, dict[str, dict[str, Any]]] = {}

    def reset_for_tests(self) -> None:
        with self._lock:
            self._sessions.clear()

    def heartbeat(
        self,
        session_id: str,
        peer_id: str,
        label: str = "",
        *,
        now: Optional[NowFn] = None,
    ) -> dict[str, Any]:
        sid = _norm(session_id)
        pid = _norm(peer_id)
        if not sid:
            raise ValueError("session_id is required")
        if not pid:
            raise ValueError("id is required")
        stamp = float((now or _now)())
        name = _norm(label) or pid
        peer = {"id": pid, "label": name, "last_seen": stamp}
        with self._lock:
            bucket = self._sessions.setdefault(sid, {})
            bucket[pid] = peer
        return dict(peer)

    def expire_stale(
        self,
        session_id: Optional[str] = None,
        *,
        now: Optional[NowFn] = None,
        ttl: Optional[float] = None,
    ) -> int:
        stamp = float((now or _now)())
        limit = self.ttl_seconds if ttl is None else float(ttl)
        dropped = 0
        with self._lock:
            if session_id is None:
                keys = list(self._sessions)
            else:
                sid = _norm(session_id)
                keys = [sid] if sid in self._sessions else []
            for sid in keys:
                bucket = self._sessions.get(sid) or {}
                dead = [
                    pid
                    for pid, peer in bucket.items()
                    if stamp - float(peer.get("last_seen") or 0) > limit
                ]
                for pid in dead:
                    bucket.pop(pid, None)
                    dropped += 1
                if not bucket:
                    self._sessions.pop(sid, None)
        return dropped

    def list_peers(
        self,
        session_id: str,
        *,
        now: Optional[NowFn] = None,
        ttl: Optional[float] = None,
    ) -> list[dict[str, Any]]:
        sid = _norm(session_id)
        if not sid:
            raise ValueError("session_id is required")
        self.expire_stale(sid, now=now, ttl=ttl)
        with self._lock:
            peers = [dict(p) for p in (self._sessions.get(sid) or {}).values()]
        peers.sort(key=lambda p: (str(p.get("label") or ""), str(p.get("id") or "")))
        return peers


STORE = PresenceStore()
