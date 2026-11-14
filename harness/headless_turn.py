from __future__ import annotations

"""Run one chat turn for a session with no browser attached.

The turn goes through the normal ``stream_chat`` path, so the event ring,
transcript checkpoints, title, hooks and ``finalize_turn`` are the same as a
turn the UI starts. The UI can open the session at any time and reattach to
the live turn through ``/api/chat/events?watch=1``.
"""

import json
import threading
from typing import Any, Callable, Optional


_local = threading.local()


def current_outcome() -> str:
    """Outcome of the headless turn on this thread, or "" on any other thread.

    ``finalize_turn`` runs inside ``stream_chat`` on the turn thread, so it can
    read how a headless turn ended.
    """
    handler = getattr(_local, "handler", None)
    return handler.outcome if handler is not None else ""


class _Sink:
    """SSE ``wfile`` that keeps no frames. It records the turn outcome."""

    def __init__(self) -> None:
        self.socket = self
        self.error = ""
        self.stopped = False
        self._tail = b""

    # sse_write sets a socket timeout around each write.
    def settimeout(self, _value: Any) -> None:
        return None

    def gettimeout(self) -> None:
        return None

    def write(self, data: bytes) -> int:
        buf = self._tail + bytes(data or b"")
        frames = buf.split(b"\n\n")
        self._tail = frames.pop()
        for frame in frames:
            self._read_frame(frame)
        return len(data or b"")

    def flush(self) -> None:
        return None

    def _read_frame(self, frame: bytes) -> None:
        for line in frame.split(b"\n"):
            if not line.startswith(b"data: "):
                continue
            try:
                event = json.loads(line[6:].decode("utf-8", "replace"))
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("kind")
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            if kind == "error":
                self.error = str(data.get("error") or data.get("message") or "error")
            elif kind in ("interrupted", "stopped", "turn_stopped"):
                self.stopped = True


class HeadlessHandler:
    """The handler surface ``stream_chat`` writes to."""

    def __init__(self) -> None:
        self.wfile = _Sink()
        self.status: Optional[int] = None
        self.body = ""

    def send_response(self, code: int, _message: Any = None) -> None:
        self.status = code

    def send_header(self, *_args: Any) -> None:
        return None

    def end_headers(self) -> None:
        return None

    def _cors(self) -> None:
        return None

    def _send(self, code: int, body: Any = "", *_args: Any, **_kwargs: Any) -> None:
        self.status = code
        self.body = body if isinstance(body, str) else str(body)

    @property
    def outcome(self) -> str:
        if self.status is not None and self.status >= 400:
            return "failed"
        if self.wfile.stopped:
            return "stopped"
        return "failed" if self.wfile.error else "done"


def start_headless_turn(
    session_id: str,
    message: str,
    *,
    stream: Callable[..., Any],
    prepare: Callable[[str], Any],
    on_refused: Optional[Callable[[str, HeadlessHandler], None]] = None,
    on_error: Optional[Callable[[str, BaseException], None]] = None,
    on_exit: Optional[Callable[[str, str], None]] = None,
    resume: bool = False,
) -> threading.Thread:
    """Start a daemon thread that prepares the runner and streams one turn.

    ``prepare(session_id)`` makes sure the runner exists. ``stream(handler,
    message, session_id=..., resume=...)`` is ``stream_chat`` with its
    services bound. ``on_refused`` runs when the stream answers with an HTTP
    error before the turn starts (no ``finalize_turn`` runs then).
    ``on_exit(session_id, outcome)`` runs last, whatever happened.
    """

    def _run() -> None:
        handler = HeadlessHandler()
        _local.handler = handler
        try:
            try:
                prepare(session_id)
                stream(handler, message, session_id=session_id, resume=resume)
            except BaseException as exc:  # never let a turn thread die silently
                handler.wfile.error = handler.wfile.error or (str(exc) or type(exc).__name__)
                if on_error is not None:
                    on_error(session_id, exc)
                return
            finally:
                _local.handler = None
            if handler.status is not None and handler.status >= 400 and on_refused is not None:
                on_refused(session_id, handler)
        finally:
            if on_exit is not None:
                on_exit(session_id, handler.outcome)

    thread = threading.Thread(target=_run, name=f"headless-turn-{session_id}", daemon=True)
    thread.start()
    return thread
