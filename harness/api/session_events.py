"""Session-scoped store event cursor (Puppetmaster ``read_events_since`` pattern).

Unifies mid-turn chat ring frames and session/runners state onto one monotonic
cursor so the GUI can subscribe once instead of running three pollers
(``useSessionSwitch`` / ``chatEventsReattach`` / ``useRunnersBusyPoll``).

Stdlib only. Mirror-on-read: each ``read_events_since`` call pulls retained SSE
ring frames into the store and samples session state, appending a ``runners``
event when the fingerprint changes.
"""

from __future__ import annotations

import json
import threading
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, Optional, Tuple, Union

from harness.api.session_control import SessionControlServices, get_session_state
from harness.api.sse import SseServices, get_chat_events

_STORE_CAP = 1024
_STORE_MAX_SESSIONS = 64

JsonPayload = Union[dict, list]


def _state_fingerprint(payload: dict) -> str:
    """Stable fingerprint for runners / pilot state / pending swarms."""
    runners = payload.get("runners") if isinstance(payload.get("runners"), dict) else {}
    runners_items = sorted(
        (str(k), str(v)) for k, v in runners.items()
    )
    blob = {
        "state": str(payload.get("state") or ""),
        "pending_swarms": bool(payload.get("pending_swarms")),
        "runners": runners_items,
        "active_view_id": str(payload.get("active_view_id") or ""),
    }
    return json.dumps(blob, separators=(",", ":"), sort_keys=True)


class SessionEventStore:
    """Bounded per-session append-only event log with a monotonic cursor."""

    def __init__(self, *, cap: int = _STORE_CAP, max_sessions: int = _STORE_MAX_SESSIONS):
        self.cap = max(1, int(cap))
        self.max_sessions = max(1, int(max_sessions))
        self._lock = threading.Lock()
        self._sessions: Dict[str, Deque[Tuple[int, dict]]] = {}
        self._cursors: Dict[str, int] = {}
        self._mirrored_ring_cursor: Dict[str, int] = {}
        self._mirrored_ring_generation: Dict[str, int] = {}
        self._state_fp: Dict[str, str] = {}
        self._last_ring_miss_fp: Dict[str, str] = {}

    def clear_session(self, session_id: str) -> None:
        sid = (session_id or "").strip()
        if not sid:
            return
        with self._lock:
            self._sessions.pop(sid, None)
            self._cursors.pop(sid, None)
            self._mirrored_ring_cursor.pop(sid, None)
            self._mirrored_ring_generation.pop(sid, None)
            self._state_fp.pop(sid, None)
            self._last_ring_miss_fp.pop(sid, None)

    def event_cursor(self, session_id: str) -> int:
        sid = (session_id or "").strip()
        with self._lock:
            return int(self._cursors.get(sid, 0) or 0)

    def append(self, session_id: str, kind: str, data: Any) -> int:
        """Append one store event; returns its monotonic id."""
        sid = (session_id or "").strip()
        if not sid:
            return 0
        with self._lock:
            return self._append_unlocked(sid, kind, data)

    def _append_unlocked(self, sid: str, kind: str, data: Any) -> int:
        self._evict_if_needed_unlocked(sid)
        nxt = int(self._cursors.get(sid, 0) or 0) + 1
        self._cursors[sid] = nxt
        ev = {
            "id": nxt,
            "kind": str(kind or "event"),
            "data": data if data is not None else {},
            "session_id": sid,
        }
        bucket = self._sessions.get(sid)
        if bucket is None:
            bucket = deque()
            self._sessions[sid] = bucket
        bucket.append((nxt, ev))
        while len(bucket) > self.cap:
            bucket.popleft()
        return nxt

    def _evict_if_needed_unlocked(self, keep_sid: str) -> None:
        while len(self._sessions) >= self.max_sessions and keep_sid not in self._sessions:
            victim = next(
                (k for k in self._sessions.keys() if k != keep_sid),
                None,
            )
            if victim is None:
                break
            self._sessions.pop(victim, None)
            self._cursors.pop(victim, None)
            self._mirrored_ring_cursor.pop(victim, None)
            self._mirrored_ring_generation.pop(victim, None)
            self._state_fp.pop(victim, None)
            self._last_ring_miss_fp.pop(victim, None)

    def since(self, session_id: str, cursor: int = 0) -> dict:
        """Return events with id > ``cursor`` plus the high-water cursor.

        When ``since > 0`` and cap-eviction left a hole (oldest retained id >
        since+1, or the bucket is empty while high-water is still ahead of
        ``since``), sets ``gap`` so callers refuse a contiguous-looking replay.
        """
        sid = (session_id or "").strip()
        try:
            since_c = int(cursor or 0)
        except (TypeError, ValueError):
            since_c = 0
        with self._lock:
            bucket = self._sessions.get(sid) or deque()
            high = int(self._cursors.get(sid, 0) or 0)
            gap = False
            if since_c > 0:
                if not bucket:
                    if high > since_c:
                        gap = True
                else:
                    oldest = bucket[0][0]
                    if oldest > since_c + 1:
                        gap = True
            events = [] if gap else [ev for eid, ev in bucket if eid > since_c]
            return {
                "session_id": sid,
                "cursor": high,
                "events": events,
                "gap": gap,
            }

    def mirrored_ring_cursor(self, session_id: str) -> int:
        sid = (session_id or "").strip()
        with self._lock:
            return int(self._mirrored_ring_cursor.get(sid, 0) or 0)

    def set_mirrored_ring(self, session_id: str, ring_cursor: int, generation: int) -> None:
        sid = (session_id or "").strip()
        with self._lock:
            self._mirrored_ring_cursor[sid] = max(0, int(ring_cursor or 0))
            self._mirrored_ring_generation[sid] = max(0, int(generation or 0))

    def mirrored_ring_generation(self, session_id: str) -> int:
        sid = (session_id or "").strip()
        with self._lock:
            return int(self._mirrored_ring_generation.get(sid, 0) or 0)

    def state_fingerprint(self, session_id: str) -> str:
        sid = (session_id or "").strip()
        with self._lock:
            return self._state_fp.get(sid, "")

    def set_state_fingerprint(self, session_id: str, fp: str) -> None:
        sid = (session_id or "").strip()
        with self._lock:
            self._state_fp[sid] = fp or ""

    def last_ring_miss_fp(self, session_id: str) -> str:
        sid = (session_id or "").strip()
        with self._lock:
            return self._last_ring_miss_fp.get(sid, "")

    def set_last_ring_miss_fp(self, session_id: str, fp: str) -> None:
        sid = (session_id or "").strip()
        with self._lock:
            if fp:
                self._last_ring_miss_fp[sid] = fp
            else:
                self._last_ring_miss_fp.pop(sid, None)


_default_store = SessionEventStore()


def get_default_store() -> SessionEventStore:
    return _default_store


def reset_default_store_for_tests() -> SessionEventStore:
    """Replace the process-wide store (tests only)."""
    global _default_store
    _default_store = SessionEventStore()
    return _default_store


@dataclass
class SessionEventsServices:
    """Deps for ``read_events_since`` HTTP handler."""

    sse_services: Any
    session_control_services: Any
    store: Optional[SessionEventStore] = None


def _resolve_svc(maybe_factory: Any) -> Any:
    if callable(maybe_factory) and not isinstance(maybe_factory, type):
        try:
            return maybe_factory()
        except TypeError:
            return maybe_factory
    return maybe_factory


def _mirror_ring_into_store(
    store: SessionEventStore,
    sse_svc: SseServices,
    session_id: str,
    generation: Optional[int],
) -> Optional[dict]:
    """Pull new ring frames / miss into the store. Returns ring miss payload if any."""
    sid = (session_id or "").strip()
    if not sid:
        return None

    live_gen = sse_svc.current_generation(sid)
    mirrored_gen = store.mirrored_ring_generation(sid)
    if live_gen is not None and int(live_gen) != mirrored_gen and mirrored_gen:
        store.set_mirrored_ring(sid, 0, int(live_gen))

    since_ring = store.mirrored_ring_cursor(sid)
    status, payload = get_chat_events(sse_svc, sid, since_ring, generation)
    if not isinstance(payload, dict):
        return None
    if status != 200:
        return None

    if not payload.get("ok"):
        miss_code = str(payload.get("code") or "ring_miss")
        miss_gen = int(payload.get("generation") or 0)
        had_mirror = since_ring > 0 or mirrored_gen > 0
        if had_mirror or miss_code in ("generation_mismatch", "cursor_gap"):
            miss_fp = f"{miss_code}:{miss_gen}:{int(payload.get('cursor') or 0)}"
            if miss_fp != store.last_ring_miss_fp(sid):
                store.append(
                    sid,
                    "ring_miss",
                    {
                        "ok": False,
                        "code": miss_code,
                        "missed": True,
                        "available": False,
                        "generation": miss_gen,
                        "cursor": int(payload.get("cursor") or 0),
                        "session_id": sid,
                    },
                )
                store.set_last_ring_miss_fp(sid, miss_fp)
        if miss_code == "generation_mismatch" and miss_gen > 0:
            store.set_mirrored_ring(sid, 0, miss_gen)
        elif miss_code == "ring_miss":
            store.set_mirrored_ring(sid, 0, 0)
        elif miss_code == "cursor_gap":
            store.set_mirrored_ring(
                sid, 0, int(payload.get("generation") or mirrored_gen or 0),
            )
        return payload

    store.set_last_ring_miss_fp(sid, "")

    frames = payload.get("events") if isinstance(payload.get("events"), list) else []
    ring_hw = int(payload.get("cursor") or since_ring or 0)
    ring_gen = int(payload.get("generation") or 0)
    for frame in frames:
        if not isinstance(frame, dict):
            continue
        store.append(
            sid,
            "stream",
            {
                "cursor": frame.get("cursor"),
                "kind": frame.get("kind") or "event",
                "data": frame.get("data") if frame.get("data") is not None else {},
                **({"turn": frame["turn"]} if "turn" in frame else {}),
                "generation": ring_gen,
            },
        )
    if ring_hw >= since_ring:
        store.set_mirrored_ring(sid, ring_hw, ring_gen)
    return None


def _sample_runners_into_store(
    store: SessionEventStore,
    session_control_svc: SessionControlServices,
    session_id: str,
) -> None:
    """Append a runners event when session state fingerprint changes."""
    sid = (session_id or "").strip()
    if not sid:
        return
    try:
        _status, payload = get_session_state({"session_id": [sid]}, session_control_svc)
    except Exception:
        return
    if not isinstance(payload, dict):
        return
    fp = _state_fingerprint(payload)
    if fp == store.state_fingerprint(sid):
        return
    store.append(
        sid,
        "runners",
        {
            "state": payload.get("state"),
            "pending_swarms": bool(payload.get("pending_swarms")),
            "runners": payload.get("runners") if isinstance(payload.get("runners"), dict) else {},
            "active_view_id": payload.get("active_view_id"),
            "resume_pending": bool(payload.get("resume_pending")),
            "goal": payload.get("goal") if isinstance(payload.get("goal"), dict) else {},
        },
    )
    store.set_state_fingerprint(sid, fp)


def read_events_since(
    session_id: str,
    cursor: int = 0,
    *,
    sse_svc: SseServices,
    session_control_svc: SessionControlServices,
    store: Optional[SessionEventStore] = None,
    generation: Optional[int] = None,
) -> tuple[int, JsonPayload]:
    """Return store events after ``cursor`` and the new high-water cursor.

    Mirrors the SSE chat ring and samples session state on each call so one
    GUI subscription covers reattach frames and busy chrome.
    """
    store = store or get_default_store()
    sid = (session_id or "").strip() or (
        sse_svc.default_session_id() if sse_svc is not None else ""
    )
    try:
        since_c = int(cursor or 0)
    except (TypeError, ValueError):
        since_c = 0

    if not sid:
        return 200, {
            "ok": True,
            "session_id": "",
            "cursor": 0,
            "events": [],
        }

    _mirror_ring_into_store(store, sse_svc, sid, generation)
    _sample_runners_into_store(store, session_control_svc, sid)

    batch = store.since(sid, since_c)
    events = batch.get("events") if isinstance(batch.get("events"), list) else []
    gap = bool(batch.get("gap"))
    if gap:
        events = [
            {
                "id": since_c,
                "kind": "ring_miss",
                "data": {
                    "ok": False,
                    "code": "cursor_gap",
                    "missed": True,
                    "available": True,
                    "generation": store.mirrored_ring_generation(sid),
                    "cursor": int(batch.get("cursor") or 0),
                    "session_id": sid,
                },
                "session_id": sid,
            }
        ]
    return 200, {
        "ok": True,
        "session_id": sid,
        "cursor": int(batch.get("cursor") or 0),
        "events": events,
        "gap": gap,
    }


def read_events_since_http(
    qs: dict,
    svc: SessionEventsServices,
) -> tuple[int, JsonPayload]:
    """GET /api/session/events — query: session, since, generation."""
    qs = qs or {}
    session = (qs.get("session", [""])[0] or qs.get("session_id", [""])[0] or "").strip()
    since_raw = qs.get("since", ["0"])[0]
    try:
        since_c = int(since_raw or 0)
    except (TypeError, ValueError):
        since_c = 0
    gen_raw = qs.get("generation", [""])[0]
    generation = None
    if gen_raw not in ("", None):
        try:
            generation = int(gen_raw)
        except (TypeError, ValueError):
            return 400, {"ok": False, "error": "generation must be an integer"}

    sse_svc = _resolve_svc(svc.sse_services)
    sc_svc = _resolve_svc(svc.session_control_services)
    store = svc.store if svc.store is not None else get_default_store()
    return read_events_since(
        session,
        since_c,
        sse_svc=sse_svc,
        session_control_svc=sc_svc,
        store=store,
        generation=generation,
    )
