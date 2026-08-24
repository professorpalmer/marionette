"""In-process metaharness scoring and leader election.

Peers report a numeric score (and optional heartbeat). One leader is
elected: highest live score, ties broken stably by peer id
(lexicographically smallest). No marketplace, Sentry, or Guardian.
"""
from __future__ import annotations

import math
import threading
import time
from typing import Any, Callable, Optional


DEFAULT_TTL_SECONDS = 30.0


class ScoreError(ValueError):
    """Reported score could not be accepted."""


def score_value(raw: Any) -> float:
    """Normalize a reported score to a finite float."""
    if raw is None or isinstance(raw, bool):
        raise ScoreError("score must be a finite number")
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ScoreError("score must be a finite number") from exc
    if not math.isfinite(value):
        raise ScoreError("score must be a finite number")
    return value


def score_report(payload: dict) -> float:
    """Score a peer report.

    Prefer an explicit ``score``. Otherwise derive a small composite from
    ``ok`` / ``success`` plus optional ``latency_ms`` (lower is better).
    """
    if not isinstance(payload, dict):
        raise ScoreError("report must be an object")
    if "score" in payload and payload["score"] is not None:
        return score_value(payload["score"])
    ok_raw = payload.get("ok", payload.get("success"))
    if ok_raw is None:
        raise ScoreError("score or ok/success is required")
    ok = bool(ok_raw)
    latency_raw = payload.get("latency_ms", 0)
    if latency_raw in (None, ""):
        latency_raw = 0
    try:
        latency = float(latency_raw)
    except (TypeError, ValueError) as exc:
        raise ScoreError("latency_ms must be a number") from exc
    if not math.isfinite(latency) or latency < 0:
        raise ScoreError("latency_ms must be a non-negative finite number")
    # Success is the bulk of the score; latency is a small tie-breaker.
    return (1.0 if ok else 0.0) + (1.0 / (1.0 + latency))


class LeaderLatch:
    """Thread-safe in-process peer score table + leader latch."""

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self._ttl = float(ttl_seconds)
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._peers: dict[str, dict[str, Any]] = {}

    def reset(self) -> None:
        with self._lock:
            self._peers.clear()

    def report_score(self, peer_id: str, score: Any) -> dict[str, Any]:
        pid = _require_peer_id(peer_id)
        value = score_value(score)
        now = self._clock()
        with self._lock:
            self._peers[pid] = {"id": pid, "score": value, "seen_at": now}
            return self._snapshot_locked(now)

    def heartbeat(self, peer_id: str, score: Any = None) -> dict[str, Any]:
        pid = _require_peer_id(peer_id)
        now = self._clock()
        with self._lock:
            prev = self._peers.get(pid)
            if score is None:
                if prev is None:
                    raise ScoreError("heartbeat without a prior score requires score")
                value = float(prev["score"])
            else:
                value = score_value(score)
            self._peers[pid] = {"id": pid, "score": value, "seen_at": now}
            return self._snapshot_locked(now)

    def status(self) -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            return self._snapshot_locked(now)

    def _live_peers(self, now: float) -> list[dict[str, Any]]:
        live: list[dict[str, Any]] = []
        stale: list[str] = []
        for pid, rec in self._peers.items():
            age = now - float(rec["seen_at"])
            if age > self._ttl:
                stale.append(pid)
                continue
            live.append({"id": rec["id"], "score": rec["score"]})
        for pid in stale:
            self._peers.pop(pid, None)
        live.sort(key=lambda p: (-float(p["score"]), str(p["id"])))
        return live

    def _elect(self, live: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
        if not live:
            return None
        return {"id": live[0]["id"], "score": live[0]["score"]}

    def _snapshot_locked(self, now: float) -> dict[str, Any]:
        live = self._live_peers(now)
        leader = self._elect(live)
        return {
            "ok": True,
            "leader": leader,
            "elected": leader is not None,
            "peers": live,
            "ttl_seconds": self._ttl,
        }


_LATCH: Optional[LeaderLatch] = None
_LATCH_LOCK = threading.Lock()


def get_latch() -> LeaderLatch:
    global _LATCH
    with _LATCH_LOCK:
        if _LATCH is None:
            _LATCH = LeaderLatch()
        return _LATCH


def reset_latch() -> LeaderLatch:
    """Replace the process latch (tests)."""
    global _LATCH
    with _LATCH_LOCK:
        _LATCH = LeaderLatch()
        return _LATCH


def _require_peer_id(peer_id: Any) -> str:
    pid = str(peer_id or "").strip()
    if not pid:
        raise ScoreError("peer_id is required")
    if len(pid) > 128:
        raise ScoreError("peer_id is too long")
    return pid
