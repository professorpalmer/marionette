from __future__ import annotations

"""In-session /loop — interval or self-paced mailbox wakes.

Distinct from host cron (Schedule) and from SessionGoal / Goal Mode.
Uses SessionActionStore WakePolicy.on_idle. No TUI chrome.
"""

import hashlib
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional

from .session_actions import (
    ActionKind,
    DeliveryPolicy,
    SessionAction,
    SessionActionStore,
    WakePolicy,
)


class LoopMode(str, Enum):
    INTERVAL = "interval"
    SELF_PACED = "self_paced"


class SessionLoopError(Exception):
    """Named illegal-loop error for start/tick guards."""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = str(code or "illegal_loop")
        super().__init__(message or self.code)


def response_digest(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def normalize_loop_mode(requested: Optional[str]) -> Optional[str]:
    if requested is None:
        return None
    mode = str(requested).strip().lower().replace("-", "_")
    if not mode:
        return None
    for item in LoopMode:
        if item.value == mode:
            return item.value
    return None


@dataclass
class SessionLoop:
    enabled: bool = False
    mode: Optional[LoopMode] = None
    prompt: str = ""
    interval_seconds: Optional[float] = None
    until: Optional[float] = None
    started_at: float = 0.0
    last_fired_at: Optional[float] = None
    last_idle: Optional[bool] = None
    last_response_digest: Optional[str] = None

    def start(
        self,
        mode: Any,
        prompt: str,
        *,
        interval_seconds: Optional[float] = None,
        until: Optional[float] = None,
        now: Optional[float] = None,
    ) -> "SessionLoop":
        if isinstance(mode, LoopMode):
            resolved = mode
        else:
            normalized = normalize_loop_mode(mode if isinstance(mode, str) else None)
            if normalized is None:
                raise SessionLoopError(
                    "unknown_loop_mode",
                    "unknown loop mode: %s" % (mode,),
                )
            resolved = LoopMode(normalized)
        cleaned = (prompt or "").strip()
        if not cleaned:
            raise SessionLoopError("missing_prompt", "loop prompt is required")
        interval = None
        if resolved is LoopMode.INTERVAL:
            try:
                interval = float(interval_seconds)
            except (TypeError, ValueError):
                raise SessionLoopError(
                    "interval_requires_seconds",
                    "interval loop requires interval_seconds",
                )
            if interval <= 0:
                raise SessionLoopError(
                    "interval_requires_seconds",
                    "interval_seconds must be > 0",
                )
        deadline = None
        if until not in (None, ""):
            try:
                deadline = float(until)
            except (TypeError, ValueError):
                raise SessionLoopError(
                    "invalid_until",
                    "until must be a unix timestamp",
                )
        stamp = time.time() if now is None else float(now)
        self.enabled = True
        self.mode = resolved
        self.prompt = cleaned
        self.interval_seconds = interval
        self.until = deadline
        self.started_at = stamp
        self.last_fired_at = None
        self.last_idle = None
        self.last_response_digest = None
        return self

    def stop(self) -> "SessionLoop":
        self.enabled = False
        return self

    def note_response(self, text: str) -> bool:
        """Record the last assistant digest. Same digest stops the loop."""
        if not self.enabled:
            return False
        digest = response_digest(text)
        if self.last_response_digest and self.last_response_digest == digest:
            self.stop()
            return False
        self.last_response_digest = digest
        return True

    def due(self, *, idle: bool, now: float) -> bool:
        if not self.enabled or self.mode is None:
            return False
        if self.until is not None and now >= self.until:
            self.stop()
            return False
        if not idle:
            return False
        if self.mode is LoopMode.SELF_PACED:
            return self.last_idle is False or self.last_idle is None
        if self.interval_seconds is None:
            return False
        anchor = (
            self.last_fired_at
            if self.last_fired_at is not None
            else self.started_at
        )
        return (now - anchor) >= float(self.interval_seconds)

    def mark_fired(self, now: float) -> None:
        self.last_fired_at = float(now)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "mode": None if self.mode is None else self.mode.value,
            "prompt": str(self.prompt),
            "interval_seconds": self.interval_seconds,
            "until": self.until,
            "started_at": float(self.started_at),
            "last_fired_at": self.last_fired_at,
            "last_response_digest": self.last_response_digest,
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "SessionLoop":
        loop = cls()
        if not isinstance(data, dict):
            return loop
        mode = normalize_loop_mode(data.get("mode"))
        loop.enabled = bool(data.get("enabled"))
        loop.mode = LoopMode(mode) if mode else None
        loop.prompt = str(data.get("prompt") or "")
        raw_interval = data.get("interval_seconds")
        if raw_interval in (None, ""):
            loop.interval_seconds = None
        else:
            try:
                loop.interval_seconds = float(raw_interval)
            except (TypeError, ValueError):
                loop.interval_seconds = None
        raw_until = data.get("until")
        if raw_until in (None, ""):
            loop.until = None
        else:
            try:
                loop.until = float(raw_until)
            except (TypeError, ValueError):
                loop.until = None
        digest = data.get("last_response_digest")
        loop.last_response_digest = (
            str(digest) if digest not in (None, "") else None
        )
        try:
            loop.started_at = float(data.get("started_at") or 0.0)
        except (TypeError, ValueError):
            loop.started_at = 0.0
        raw_fired = data.get("last_fired_at")
        if raw_fired in (None, ""):
            loop.last_fired_at = None
        else:
            try:
                loop.last_fired_at = float(raw_fired)
            except (TypeError, ValueError):
                loop.last_fired_at = None
        return loop


def session_loop_of(session: Any) -> SessionLoop:
    loop = getattr(session, "_loop_state", None)
    if isinstance(loop, SessionLoop):
        return loop
    loop = SessionLoop()
    try:
        session._loop_state = loop
    except Exception:
        pass
    return loop


def _action_store_of(session: Any) -> Optional[SessionActionStore]:
    store = getattr(session, "_session_actions", None)
    if isinstance(store, SessionActionStore):
        return store
    getter = getattr(session, "_action_store", None)
    if callable(getter):
        got = getter()
        if isinstance(got, SessionActionStore):
            return got
    return None


def start_session_loop(
    session: Any,
    mode: Any,
    prompt: str,
    *,
    interval_seconds: Optional[float] = None,
    until: Optional[float] = None,
    now: Optional[float] = None,
) -> SessionLoop:
    return session_loop_of(session).start(
        mode,
        prompt,
        interval_seconds=interval_seconds,
        until=until,
        now=now,
    )


def note_session_loop_response(session: Any, text: str) -> bool:
    loop = getattr(session, "_loop_state", None)
    if not isinstance(loop, SessionLoop):
        return True
    return loop.note_response(text)


def stop_session_loop(session: Any) -> SessionLoop:
    return session_loop_of(session).stop()


def session_loop_snapshot(session: Any) -> Dict[str, Any]:
    return session_loop_of(session).to_dict()


def fire_session_loop(
    loop: SessionLoop,
    store: SessionActionStore,
    *,
    idle: bool,
    now: float,
    enqueue_prompt: Any = None,
) -> Optional[SessionAction]:
    """Admit a mailbox wake when due. Playlist enqueue is optional."""
    should_fire = loop.due(idle=idle, now=now)
    loop.last_idle = bool(idle)
    if not should_fire:
        return None
    action = store.admit(
        ActionKind.MAILBOX,
        loop.prompt,
        delivery=DeliveryPolicy.WHEN_RUN_IDLE,
        wake=WakePolicy.ON_IDLE,
    )
    if callable(enqueue_prompt) and loop.prompt:
        try:
            enqueue_prompt(loop.prompt)
        except Exception:
            pass
    loop.mark_fired(now)
    return action


def tick_session_loop(
    session: Any,
    *,
    idle: bool,
    now: Optional[float] = None,
) -> bool:
    """Fire a due loop into the session store (and prompt playlist when present)."""
    loop = getattr(session, "_loop_state", None)
    if not isinstance(loop, SessionLoop) or not loop.enabled:
        return False
    store = _action_store_of(session)
    if store is None:
        return False
    stamp = time.time() if now is None else float(now)
    enqueue = getattr(session, "enqueue_prompt", None)
    action = fire_session_loop(
        loop, store, idle=idle, now=stamp, enqueue_prompt=enqueue
    )
    return action is not None
