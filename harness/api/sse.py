"""SSE ring buffer + pump/write helpers (peeled from ``harness.server``).

Bounded per-session/per-generation frame buffer for mid-turn reattach, plus
Hermes-style ``sse_write`` / ``sse_pump`` that take a handler-like ``wfile``.
Stream route bodies live in ``harness.api.streams``. ``server.py`` re-exports
historical names and keeps thin ``Handler`` wrappers so tests keep binding
``Handler._sse_write`` / ``Handler._sse_pump``.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, Optional, Tuple

# Mid-turn SSE reattach: bounded per-session/per-generation event ring. When the
# UI detaches, _sse_pump keeps draining the turn and RETAINS recent frames here
# so GET /api/chat/events?since=cursor can replay what was missed. Cap + TTL
# keep memory bounded across long detached turns.
# Miss contract: when the ring is absent, the requested generation is stale, or
# cap/TTL prune left a hole after ``since`` (oldest retained > since+1, or the
# ring is empty while the high-water cursor is still ahead), the endpoint
# returns ok:false with code ring_miss / generation_mismatch / cursor_gap
# (plus missed:true, available:false) -- never ok:true with a contiguous-looking
# replay that skips cursors, which clients would misread as successful catch-up.
_SSE_RING_CAP = 512
_SSE_RING_TTL = 300.0  # seconds
_SSE_RING_MAX_SESSIONS = 32


class SseEventRing:
    """Bounded cursor-addressable SSE frame buffer for one turn generation."""

    def __init__(
        self,
        session_id: str,
        generation: int,
        *,
        cap: int = _SSE_RING_CAP,
        ttl: float = _SSE_RING_TTL,
    ):
        self.session_id = session_id or ""
        self.generation = int(generation)
        self.cap = max(1, int(cap))
        self.ttl = float(ttl)
        self._lock = threading.Lock()
        self._cursor = 0
        # (cursor, monotonic_ts, event_dict)
        self._entries: Deque[Tuple[int, float, dict]] = deque()

    def append(self, kind: str, data: Any = None, turn: Any = None) -> int:
        """Append one logical SSE event; returns its cursor id."""
        with self._lock:
            self._cursor += 1
            now = time.monotonic()
            ev: dict = {"cursor": self._cursor, "kind": kind, "data": data if data is not None else {}}
            if turn is not None:
                ev["turn"] = turn
            self._entries.append((self._cursor, now, ev))
            self._prune_unlocked(now)
            return self._cursor

    def _prune_unlocked(self, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        while self._entries and (now - self._entries[0][1]) > self.ttl:
            self._entries.popleft()
        while len(self._entries) > self.cap:
            self._entries.popleft()

    def since(self, cursor: int = 0) -> dict:
        """Return frames with cursor > ``cursor`` (oldest retained first).

        When ``since > 0`` and prune left a hole (oldest retained cursor >
        since+1, or retained empty while this generation's high-water cursor is
        still ahead of ``since``), sets ``gap`` so callers can refuse a
        contiguous-looking ok:true replay.
        """
        try:
            since_c = int(cursor or 0)
        except (TypeError, ValueError):
            since_c = 0
        with self._lock:
            self._prune_unlocked()
            gap = False
            if since_c > 0:
                if not self._entries:
                    # Generation still live but nothing retained — client is
                    # behind the high-water mark with no replay available.
                    if self._cursor > since_c:
                        gap = True
                else:
                    oldest = self._entries[0][0]
                    if oldest > since_c + 1:
                        gap = True
            events = [] if gap else [e for c, _ts, e in self._entries if c > since_c]
            return {
                "session_id": self.session_id,
                "generation": self.generation,
                "cursor": self._cursor,
                "events": events,
                "retained": len(self._entries),
                "gap": gap,
            }


# session_id -> generation counter; (session_id, generation) -> ring
_sse_ring_generation: Dict[str, int] = {}
_sse_rings: Dict[Tuple[str, int], SseEventRing] = {}
_sse_rings_lock = threading.Lock()


def _sse_ring_begin(session_id: str) -> SseEventRing:
    """Start a new generation ring for ``session_id`` (drops prior gens)."""
    sid = session_id or ""
    with _sse_rings_lock:
        gen = int(_sse_ring_generation.get(sid, 0) or 0) + 1
        _sse_ring_generation[sid] = gen
        # Drop older generations for this session.
        for key in list(_sse_rings.keys()):
            if key[0] == sid:
                _sse_rings.pop(key, None)
        ring = SseEventRing(sid, gen)
        _sse_rings[(sid, gen)] = ring
        # Bound global ring count (oldest keys first).
        while len(_sse_rings) > _SSE_RING_MAX_SESSIONS:
            oldest = next(iter(_sse_rings))
            _sse_rings.pop(oldest, None)
        return ring


def _sse_ring_lookup(
    session_id: str,
    generation: Optional[int] = None,
) -> Optional[SseEventRing]:
    """Resolve the live ring for a session (latest gen if generation omitted)."""
    sid = session_id or ""
    with _sse_rings_lock:
        if generation is not None:
            try:
                gen = int(generation)
            except (TypeError, ValueError):
                return None
            return _sse_rings.get((sid, gen))
        gen = _sse_ring_generation.get(sid)
        if gen is None:
            return None
        return _sse_rings.get((sid, gen))


def _sse_ring_clear_for_tests() -> None:
    """Reset ring state between hermetic tests."""
    with _sse_rings_lock:
        _sse_rings.clear()
        _sse_ring_generation.clear()


def sse_write(wfile: Any, payload: bytes) -> bool:
    """Write one SSE frame. Returns False if the client has detached.

    View detach (EventSource close / navigate away) must NOT cancel the
    in-flight turn -- only /api/session/interrupt does. Callers drain the
    generator after a False return so _busy still releases via the
    generator's own finally.
    """
    try:
        wfile.write(payload)
        wfile.flush()
        return True
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        # ConnectionAbortedError is the common Windows EventSource/nav-close
        # path; it is not a subclass of BrokenPipe/Reset. Treat it as detach
        # so the pump can keep draining instead of gen.close()-aborting mid-yield.
        return False


def sse_pump(
    wfile: Any,
    gen: Any,
    frame_for_event: Callable[[Any], bytes],
    *,
    on_event: Optional[Callable[[Any], None]] = None,
    write_done: bool = True,
    ring: Optional[SseEventRing] = None,
) -> bool:
    """Pump a turn generator over SSE with Hermes-style detach semantics.

    While the UI is attached, each event is written. On client disconnect we
    keep consuming the generator so the pilot turn finishes and releases
    _busy -- we never call _pilot.cancel() here. Explicit Stop still goes
    through /api/session/interrupt.

    When ``ring`` is provided, every event (including those after detach) is
    retained in the bounded per-generation buffer for /api/chat/events replay.

    Returns True if the client detached mid-stream.
    """
    detached = False
    try:
        for ev in gen:
            if on_event is not None:
                on_event(ev)
            if ring is not None:
                try:
                    ring.append(
                        getattr(ev, "kind", "event"),
                        getattr(ev, "data", None) or {},
                        getattr(ev, "turn", None),
                    )
                except Exception:
                    pass
            if detached:
                continue
            if not sse_write(wfile, frame_for_event(ev)):
                detached = True
        if write_done and not detached:
            sse_write(wfile, b"data: {\"kind\": \"done\"}\n\n")
        if write_done and ring is not None:
            try:
                ring.append("done", {})
            except Exception:
                pass
    finally:
        # Exhausted generators are a no-op; if the turn raised, close still
        # runs the generator finally so the session lock cannot leak.
        try:
            gen.close()
        except Exception:
            pass
    return detached
