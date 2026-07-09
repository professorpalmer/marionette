"""Per-session runner registry with a concurrent-session lease.

Active VIEW is which session the UI attaches to. Other sessions may keep
executing under a lease cap. Evict idle runners first when the lease is full;
raise LeaseExhaustedError only when every slot is busy and a new runner is
required.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Optional

DEFAULT_MAX_CONCURRENT_SESSIONS = 3


class LeaseExhaustedError(Exception):
    """Raised when a new runner is needed but every lease slot is busy."""


def _max_concurrent_from_env(default: int = DEFAULT_MAX_CONCURRENT_SESSIONS) -> int:
    raw = (os.environ.get("HARNESS_MAX_CONCURRENT_SESSIONS") or "").strip()
    if not raw:
        return default
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, n)


def _is_busy(runner: Any) -> bool:
    """Duck-typed busy check: ``_busy.locked()`` or ``_state != idle``."""
    busy = getattr(runner, "_busy", None)
    if busy is not None:
        locked = getattr(busy, "locked", None)
        if callable(locked) and locked():
            return True
    state = getattr(runner, "_state", None)
    if state is not None and state != "idle":
        return True
    return False


class SessionRunnerRegistry:
    """dict[session_id -> runner] with a concurrent-session lease."""

    def __init__(
        self,
        max_concurrent_sessions: Optional[int] = None,
        on_drop: Optional[Callable[[str, Any], None]] = None,
    ) -> None:
        self._max = (
            max_concurrent_sessions
            if max_concurrent_sessions is not None
            else _max_concurrent_from_env()
        )
        self._runners: dict[str, Any] = {}
        self._order: list[str] = []
        self._active_view_id: Optional[str] = None
        # Optional hook (e.g. fold boot cost meters) when a runner leaves the
        # registry via drop/evict. Rebuild/swap pass notify=False to skip it.
        self._on_drop = on_drop

    @property
    def max_concurrent_sessions(self) -> int:
        return self._max

    @property
    def active_view_id(self) -> Optional[str]:
        return self._active_view_id

    def get(self, session_id: str) -> Optional[Any]:
        return self._runners.get(session_id)

    def get_or_create(
        self,
        session_id: str,
        factory: Callable[[], Any],
    ) -> Any:
        """Return the runner for ``session_id``, creating under the lease if needed.

        When at capacity, idle runners are evicted oldest-first. If every slot
        holds a busy runner and ``session_id`` is new, raises
        ``LeaseExhaustedError``.
        """
        existing = self._runners.get(session_id)
        if existing is not None:
            return existing

        if len(self._runners) >= self._max:
            self._evict_idle_oldest_first()

        if len(self._runners) >= self._max:
            raise LeaseExhaustedError(
                "session runner lease exhausted: all concurrent sessions are busy"
            )

        runner = factory()
        self._runners[session_id] = runner
        self._order.append(session_id)
        return runner

    def drop(self, session_id: str, *, notify: bool = True) -> Optional[Any]:
        """Remove ``session_id`` from the registry.

        When ``notify`` is True (default), invoke ``on_drop`` with the removed
        runner so callers can fold process-lifetime meters. Rebuild/swap that
        replace the SAME view's runner and copy meters must pass
        ``notify=False`` to avoid double-counting.
        """
        runner = self._runners.pop(session_id, None)
        if runner is None:
            return None
        try:
            self._order.remove(session_id)
        except ValueError:
            pass
        if self._active_view_id == session_id:
            self._active_view_id = None
        if notify and self._on_drop is not None:
            try:
                self._on_drop(session_id, runner)
            except Exception:
                pass
        return runner

    def set_active_view(self, session_id: str) -> None:
        self._active_view_id = session_id

    def status(self, session_id: str) -> str:
        runner = self._runners.get(session_id)
        if runner is None:
            return "missing"
        return "running" if _is_busy(runner) else "idle"

    def statuses(self) -> dict[str, str]:
        return {sid: self.status(sid) for sid in self._order if sid in self._runners}

    def ids(self) -> list[str]:
        return list(self._order)

    def runners(self) -> list[Any]:
        """Live runners in insertion order (for process-lifetime boot meters)."""
        return [self._runners[sid] for sid in self._order if sid in self._runners]

    def __len__(self) -> int:
        return len(self._runners)

    def _evict_idle_oldest_first(self) -> None:
        """Drop idle runners in insertion order until under capacity."""
        for sid in list(self._order):
            if len(self._runners) < self._max:
                break
            runner = self._runners.get(sid)
            if runner is None:
                continue
            if _is_busy(runner):
                continue
            self.drop(sid)
