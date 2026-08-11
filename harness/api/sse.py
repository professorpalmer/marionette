"""SSE ring buffer + pump/write helpers (peeled from ``harness.server``).

Bounded per-session/per-generation frame buffer for mid-turn reattach, plus
Hermes-style ``sse_write`` / ``sse_pump`` that take a handler-like ``wfile``.
``GET /api/chat/events`` JSON replay lives here as ``get_chat_events``;
``?watch=1`` live ring tail is ``stream_chat_events``. Stream route bodies
live in ``harness.api.streams``. ``server.py`` re-exports historical names
and keeps thin ``Handler`` wrappers so tests keep binding
``Handler._sse_write`` / ``Handler._sse_pump``.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Deque, Dict, Optional, Tuple, TypedDict, Union

from harness.diag import note as _diag_note

# Terminal kinds that end a live ring watch (match webapp isTerminalStreamKind).
_CHAT_EVENTS_WATCH_TERMINAL_KINDS = frozenset({
    "assistant_done",
    "done",
    "error",
    "auto_halt",
    "interrupted",
})
# Poll interval while waiting for new ring frames (same cadence as terminal SSE).
_CHAT_EVENTS_WATCH_POLL_S = 0.05

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
# Soft cap prefers unpinned rings; hard cap force-evicts oldest (even pinned)
# so a stuck-pin storm cannot grow the map without bound.
_SSE_RING_HARD_MAX_SESSIONS = 64


# 3.9-safe optional fields via TypedDict inheritance (NotRequired is 3.11+;
# harness stays stdlib-only so no typing_extensions).
class _StreamEventRequired(TypedDict):
    kind: str


class StreamEventDict(_StreamEventRequired, total=False):
    """Wire SSE payload shared with webapp StreamEvent.

    Chat/auto framers omit turn (ConvEvent); classic /run framers include turn
    (SessionEvent). Do not unify the shapes.
    """

    data: Any
    turn: Any


class _SseRingEventRequired(TypedDict):
    cursor: int
    kind: str
    data: Any


class SseRingEvent(_SseRingEventRequired, total=False):
    """One retained ring frame (GET /api/chat/events events[] item).

    ``turn`` is only present when the source event carried one (SessionEvent
    /run). Chat ConvEvent appends leave it absent — matching
    ``getattr(ev, 'turn', None)`` in sse_pump.
    """

    turn: Any


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
        # True while sse_pump is draining this generation — global eviction
        # must not drop a live reattach buffer under multi-session churn.
        self.pinned = False
        self._lock = threading.Lock()
        self._cursor = 0
        # (cursor, monotonic_ts, event_dict)
        self._entries: Deque[Tuple[int, float, SseRingEvent]] = deque()

    def append(self, kind: str, data: Any = None, turn: Any = None) -> int:
        """Append one logical SSE event; returns its cursor id.

        ``kind`` stays ``str`` so SessionEvent, ConvEvent, and framing-only
        ``done`` can share the ring without unifying their Literal unions.
        """
        with self._lock:
            self._cursor += 1
            now = time.monotonic()
            ev: SseRingEvent = {
                "cursor": self._cursor,
                "kind": kind,
                "data": data if data is not None else {},
            }
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
        # Bound global ring count (oldest unpinned first). Prefer keeping pinned
        # rings whose pump is still live — temporary soft overshoot beats a
        # mid-turn ring_miss for a detached-busy session. Past the hard ceiling,
        # force-evict the oldest entry regardless of pin.
        new_key = (sid, gen)
        while len(_sse_rings) > _SSE_RING_MAX_SESSIONS:
            victim = None
            for key, existing in _sse_rings.items():
                if key == new_key:
                    continue
                if not getattr(existing, "pinned", False):
                    victim = key
                    break
            if victim is None:
                break
            _sse_rings.pop(victim, None)
        while len(_sse_rings) > _SSE_RING_HARD_MAX_SESSIONS:
            victim = None
            for key in _sse_rings:
                if key != new_key:
                    victim = key
                    break
            if victim is None:
                break
            _sse_rings.pop(victim, None)
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


def _sse_ring_current_generation(session_id: str) -> Optional[int]:
    """Latest generation counter for ``session_id``, or None if never begun."""
    sid = session_id or ""
    with _sse_rings_lock:
        gen = _sse_ring_generation.get(sid)
        return int(gen) if gen is not None else None


@dataclass
class SseServices:
    """Explicit deps for SSE HTTP handlers (injected by ``server.py``)."""

    ring_lookup: Callable[[str, Optional[int]], Optional[SseEventRing]]
    current_generation: Callable[[str], Optional[int]]
    default_session_id: Callable[[], str]


JsonPayload = Union[dict, list]


def get_chat_events(
    svc: SseServices,
    session_id: str,
    since: int,
    generation: Optional[int],
) -> tuple[int, JsonPayload]:
    """GET /api/chat/events — mid-turn reattach replay from the SSE ring.

    Preserves miss codes ``ring_miss`` / ``generation_mismatch`` / ``cursor_gap``
    and the ok/missed/available fields clients rely on.
    """
    sid = (session_id or "").strip() or svc.default_session_id()
    try:
        since_c = int(since or 0)
    except (TypeError, ValueError):
        since_c = 0
    ring = svc.ring_lookup(sid, generation)
    if ring is None:
        # Distinguish a missing ring from a stale generation so clients
        # do not treat an empty ok:true replay as a successful catch-up.
        miss_code = "ring_miss"
        current_gen = 0
        live_gen = svc.current_generation(sid)
        if live_gen is not None:
            current_gen = int(live_gen)
            if generation is not None and int(generation) != current_gen:
                miss_code = "generation_mismatch"
        # On generation_mismatch, surface the live ring's high-water cursor so
        # the client can see how far ahead the new generation already is.
        cursor = 0
        if miss_code == "generation_mismatch" and current_gen:
            live_ring = svc.ring_lookup(sid, current_gen)
            if live_ring is not None:
                try:
                    cursor = int(live_ring.since(0).get("cursor") or 0)
                except Exception:
                    cursor = 0
        return 200, {
            "ok": False,
            "code": miss_code,
            "missed": True,
            "available": False,
            "session_id": sid,
            "generation": (
                current_gen if miss_code == "generation_mismatch"
                else (generation if generation is not None else 0)
            ),
            "cursor": cursor,
            "events": [],
            "retained": 0,
        }
    payload = ring.since(since_c)
    if payload.pop("gap", False):
        # Cap/TTL prune punched a hole after ``since`` — refuse so the
        # client hydrates instead of advancing past dropped frames.
        return 200, {
            "ok": False,
            "code": "cursor_gap",
            "missed": True,
            "available": False,
            "session_id": sid,
            "generation": ring.generation,
            "cursor": int(payload.get("cursor") or 0),
            "events": [],
            "retained": int(payload.get("retained") or 0),
        }
    payload["ok"] = True
    payload["missed"] = False
    payload["available"] = True
    return 200, payload


def _encode_chat_events_watch_frame(ev: dict) -> bytes:
    """SSE frame for a retained ring event (kind/data + cursor for reattach)."""
    frame: Dict[str, Any] = {
        "kind": ev.get("kind") or "event",
        "data": ev.get("data") if ev.get("data") is not None else {},
    }
    cursor = ev.get("cursor")
    if isinstance(cursor, int):
        frame["cursor"] = cursor
    turn = ev.get("turn")
    if turn is not None:
        frame["turn"] = turn
    return f"data: {json.dumps(frame)}\n\n".encode()


def stream_chat_events(
    handler: Any,
    svc: SseServices,
    session_id: str,
    since: int,
    generation: Optional[int],
) -> None:
    """Live SSE watch over the chat-events ring (``GET /api/chat/events?watch=1``).

    Replays retained frames since ``since``, then tails new appends until a
    terminal kind (or the ring ends). On miss/gap, returns JSON 409 so the
    client can fall back to the 1Hz pull poll without inventing a second
    transcript protocol.
    """
    _status, payload = get_chat_events(svc, session_id, since, generation)
    if not isinstance(payload, dict) or not payload.get("ok"):
        # Non-OK watch open → client transport onError → JSON poll fallback.
        body = json.dumps(payload if isinstance(payload, dict) else {"ok": False})
        handler._send(409, body)
        return

    sid = str(payload.get("session_id") or session_id or "")
    try:
        cursor = int(since or 0)
    except (TypeError, ValueError):
        cursor = 0
    gen_pin = generation
    if gen_pin is None:
        try:
            gen_pin = int(payload.get("generation") or 0) or None
        except (TypeError, ValueError):
            gen_pin = None

    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler._cors()
    handler.end_headers()

    def _emit_batch(events: list) -> bool:
        """Write frames; return False if client detached or terminal written."""
        nonlocal cursor
        for ev in events:
            if not isinstance(ev, dict):
                continue
            if not sse_write(handler.wfile, _encode_chat_events_watch_frame(ev)):
                return False
            try:
                c = int(ev.get("cursor") or 0)
            except (TypeError, ValueError):
                c = 0
            if c > cursor:
                cursor = c
            kind = str(ev.get("kind") or "")
            if kind in _CHAT_EVENTS_WATCH_TERMINAL_KINDS:
                # Framing done closes the web transport; skip duplicate when
                # the ring frame itself was already kind=done.
                if kind != "done":
                    sse_write(handler.wfile, b"data: {\"kind\": \"done\"}\n\n")
                return False
        return True

    # Initial catch-up from the same payload get_chat_events already built.
    initial = payload.get("events") if isinstance(payload.get("events"), list) else []
    if not _emit_batch(initial):
        return
    try:
        hw = int(payload.get("cursor") or cursor)
    except (TypeError, ValueError):
        hw = cursor
    if hw > cursor:
        cursor = hw

    # Tail new ring appends until terminal / ring gone / client detach.
    idle_unpinned = 0
    while True:
        ring = svc.ring_lookup(sid, gen_pin)
        if ring is None:
            sse_write(handler.wfile, b"data: {\"kind\": \"done\"}\n\n")
            return
        batch = ring.since(cursor)
        if batch.pop("gap", False):
            # Mid-watch hole: close without framing done so the client falls
            # back to JSON poll + disk hydrate (same miss contract).
            return
        events = batch.get("events") if isinstance(batch.get("events"), list) else []
        if events:
            idle_unpinned = 0
            if not _emit_batch(events):
                return
            continue
        pinned = bool(getattr(ring, "pinned", False))
        if not pinned:
            idle_unpinned += 1
            # Pump finished and nothing new — settle (done may already have
            # been pruned by TTL).
            if idle_unpinned >= 2:
                sse_write(handler.wfile, b"data: {\"kind\": \"done\"}\n\n")
                return
        else:
            idle_unpinned = 0
        time.sleep(_CHAT_EVENTS_WATCH_POLL_S)


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
    if ring is not None:
        ring.pinned = True
    try:
        for ev in gen:
            if on_event is not None:
                on_event(ev)
            if ring is not None:
                try:
                    # Match SseEventRing.append: only None becomes {}. Do not use
                    # `or {}` — falsy-but-valid payloads (e.g. 0, "", False) must
                    # round-trip; empty dict is already handled by append.
                    ring.append(
                        getattr(ev, "kind", "event"),
                        getattr(ev, "data", None),
                        getattr(ev, "turn", None),
                    )
                except Exception as exc:
                    _diag_note("sse_pump.ring_append", exc)
            if detached:
                continue
            if not sse_write(wfile, frame_for_event(ev)):
                detached = True
        if write_done and not detached:
            sse_write(wfile, b"data: {\"kind\": \"done\"}\n\n")
        if write_done and ring is not None:
            try:
                ring.append("done", {})
            except Exception as exc:
                _diag_note("sse_pump.ring_append_done", exc)
    finally:
        if ring is not None:
            ring.pinned = False
        # Exhausted generators are a no-op; if the turn raised, close still
        # runs the generator finally so the session lock cannot leak.
        try:
            gen.close()
        except Exception as exc:
            _diag_note("sse_pump.gen_close", exc)
    return detached
