"""Hermes-style deferred cold-attach: idle+transcript first, pilot build later.

Cold ``_attach_view`` can register a lightweight placeholder runner so the HTTP
response returns before ``ConversationalSession`` construction finishes.
Background build swaps the real pilot in under a ready latch; turn-start paths
must call ``ensure_ready`` / ``wait_ready`` so chats never race a half-built pilot.

Kill switch: ``HARNESS_DEFER_COLD_ATTACH=0`` forces synchronous cold builds.
"""

from __future__ import annotations

import os
import threading
from typing import Any, Callable, Optional, Union


def defer_cold_attach_enabled() -> bool:
    """True unless ``HARNESS_DEFER_COLD_ATTACH`` is an explicit off value."""
    raw = (os.environ.get("HARNESS_DEFER_COLD_ATTACH") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def normalize_transcript_payload(raw: Any) -> dict[str, list]:
    """Normalize disk / live transcript into ``{history, display, job_ids}``."""
    if isinstance(raw, dict):
        return {
            "history": list(raw.get("history") or []),
            "display": list(raw.get("display") or []),
            "job_ids": list(raw.get("job_ids") or []),
        }
    if isinstance(raw, list):
        return {"history": list(raw), "display": [], "job_ids": []}
    return {"history": [], "display": [], "job_ids": []}


def is_deferred_placeholder(runner: Any) -> bool:
    return isinstance(runner, DeferredPilotPlaceholder)


class DeferredPilotPlaceholder:
    """Idle shell registered on cold attach until ConversationalSession is ready.

    Duck-types the registry busy/status surface and transcript export so view
    switch / transcript GET / session.state work without the heavy pilot.
    Turns must ``wait_ready`` (or ``ensure_ready``) before using pilot APIs.
    """

    def __init__(
        self,
        *,
        session_id: str,
        state_dir: str,
        transcript: Any = None,
    ) -> None:
        self.harness_session_id = session_id
        self.state_dir = state_dir
        self._busy = threading.Lock()
        self._state = "idle"
        self._ready = threading.Event()
        self._build_error: Optional[BaseException] = None
        self._real: Any = None
        self._defer_building = True
        self._transcript = normalize_transcript_payload(transcript)
        self._pending_history: Any = transcript
        self._mcp = None
        self._session_store = None
        self._auto_distill = False
        self._on_wiki_ingest = None
        self._history: list = []
        # Zero meters so /api/usage getattr sums stay clean pre-swap.
        self._tokens_used = 0
        self._tokens_in = 0
        self._tokens_out = 0
        self._tokens_cached = 0
        self._worker_cost_usd = 0.0
        self._worker_tokens_in = 0
        self._worker_tokens_out = 0

    @property
    def defer_building(self) -> bool:
        """True while the background ConversationalSession build is in flight."""
        return self._defer_building

    @property
    def build_error(self) -> Optional[BaseException]:
        return self._build_error

    @property
    def real_pilot(self) -> Any:
        return self._real

    def is_ready(self) -> bool:
        return self._ready.is_set()

    def is_turn_busy(self) -> bool:
        return False

    def state(self) -> str:
        return "idle"

    def has_pending_swarms(self) -> bool:
        return False

    def export_transcript_data(self) -> dict[str, list]:
        return {
            "history": list(self._transcript.get("history") or []),
            "display": list(self._transcript.get("display") or []),
            "job_ids": list(self._transcript.get("job_ids") or []),
        }

    def export_history(self) -> list:
        """Same shape as ConversationalSession.export_history (turns only)."""
        return list(self._transcript.get("history") or [])

    def load_history(self, messages: Any) -> None:
        self._pending_history = messages
        self._transcript = normalize_transcript_payload(messages)
        self._history = list(self._transcript.get("history") or [])

    def wait_ready(self, timeout: Optional[float] = None) -> bool:
        return self._ready.wait(timeout)

    def ensure_ready(self, timeout: Optional[float] = 120.0) -> Any:
        """Block until the real pilot is ready; raise on timeout or build failure."""
        if not self.wait_ready(timeout=timeout):
            raise TimeoutError(
                f"deferred pilot build timed out for session {self.harness_session_id!r}"
            )
        if self._build_error is not None:
            raise RuntimeError(
                f"deferred pilot build failed for session {self.harness_session_id!r}: "
                f"{self._build_error}"
            ) from self._build_error
        if self._real is None:
            raise RuntimeError(
                f"deferred pilot ready but missing real runner for {self.harness_session_id!r}"
            )
        return self._real

    def mark_ready(self, real: Any) -> None:
        self._real = real
        self._defer_building = False
        self._ready.set()

    def mark_failed(self, exc: BaseException) -> None:
        self._build_error = exc
        self._defer_building = False
        self._ready.set()


def schedule_deferred_build(
    build_fn: Callable[[], Any],
    *,
    on_done: Callable[[Any], None],
    on_error: Callable[[BaseException], None],
    delay: float = 0.0,
) -> Union[threading.Timer, threading.Thread]:
    """Run ``build_fn`` off the response path (Hermes ``_schedule_agent_build``)."""

    def _run() -> None:
        try:
            real = build_fn()
        except BaseException as exc:  # noqa: BLE001 — surface any build failure
            try:
                on_error(exc)
            except Exception:
                pass
            return
        try:
            on_done(real)
        except BaseException as exc:  # noqa: BLE001
            try:
                on_error(exc)
            except Exception:
                pass

    if delay and delay > 0:
        timer = threading.Timer(delay, _run)
        timer.daemon = True
        timer.start()
        return timer
    thread = threading.Thread(target=_run, name="deferred-pilot-build", daemon=True)
    thread.start()
    return thread
