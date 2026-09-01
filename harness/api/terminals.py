"""Terminal control HTTP route bodies (peeled from ``harness.server``).

Includes SSE ``GET /api/terminal/stream`` via ``stream_terminal`` (writes on
the handler ``wfile``, same pattern as ``harness.api.streams``).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any


@dataclass
class TerminalServices:
    """Explicit deps for terminal HTTP handlers."""

    cfg: Any
    pty: Any


def post_terminal_create(body: dict, svc: TerminalServices) -> tuple[int, dict]:
    """POST /api/terminal/create."""
    try:
        # Reap any dead PTY sessions first so exited/stuck terminals do
        # not pile up across restarts (the Restart button creates a fresh
        # session each time; the old dead ones should be cleaned up).
        svc.pty.reap()
        cwd = svc.cfg.repo or os.path.expanduser("~")
        from harness.pty_manager import clamp_pty_dims

        cols, rows = clamp_pty_dims(body.get("cols", 80), body.get("rows", 24))
        sess = svc.pty.create(cwd=cwd, cols=cols, rows=rows)
        return 200, {"id": sess.id, "cwd": sess._cwd}
    except Exception as e:
        return 500, {"error": str(e)}


def post_terminal_write(body: dict, svc: TerminalServices) -> tuple[int, dict]:
    """POST /api/terminal/write."""
    sess = svc.pty.get(body.get("id", ""))
    if not sess:
        return 404, {"error": "no such terminal"}
    sess.write(body.get("data", ""))
    return 200, {"ok": True}


def post_terminal_resize(body: dict, svc: TerminalServices) -> tuple[int, dict]:
    """POST /api/terminal/resize."""
    sess = svc.pty.get(body.get("id", ""))
    if not sess:
        return 404, {"error": "no such terminal"}
    sess.resize(int(body.get("rows", 24)), int(body.get("cols", 80)))
    return 200, {"ok": True}


def post_terminal_kill(body: dict, svc: TerminalServices) -> tuple[int, dict]:
    """POST /api/terminal/kill."""
    svc.pty.kill(body.get("id", ""))
    return 200, {"ok": True}


def stream_terminal(handler: Any, sid: str, svc: TerminalServices, start_offset: int = 0) -> None:
    """Stream PTY output over SSE (GET /api/terminal/stream).

    Client sends keystrokes via POST /api/terminal/write. Preserves data/exit
    frames and BrokenPipe/ConnectionReset detach handling.
    """
    import base64 as _b64
    from harness.api.redaction import redact_secret_text

    offset = max(0, int(start_offset or 0))
    sess = svc.pty.get(sid)
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler._cors()
    handler.end_headers()

    def send(payload: dict) -> None:
        handler.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
        handler.wfile.flush()

    if not sess:
        try:
            send({"kind": "exit", "offset": offset, "reason": "missing_session"})
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        return
    # Emit kind:exit from finally when the client is still writable so the
    # renderer can distinguish a real process death from a bare stream drop
    # (reattach without killing ConPTY). Skip exit when the client already
    # disconnected (BrokenPipe / ConnectionReset).
    client_writable = True
    reason = "process_exit"
    try:
        while sess.alive():
            data, reported = sess.read_since(offset)
            if data:
                # Some PTY implementations report stale offsets; never move
                # backwards and always account for bytes actually delivered.
                offset = max(offset + len(data), int(reported))
                send({"kind": "data", "b64": _b64.b64encode(data).decode("ascii"), "offset": offset})
            else:
                offset = max(offset, int(reported))
                time.sleep(0.05)
        # flush any final bytes after exit
        data, reported = sess.read_since(offset)
        if data:
            offset = max(offset + len(data), int(reported))
            send({"kind": "data", "b64": _b64.b64encode(data).decode("ascii"), "offset": offset})
        else:
            offset = max(offset, int(reported))
    except (BrokenPipeError, ConnectionResetError):
        client_writable = False
    except Exception as exc:
        reason = "stream_error"
        error = redact_secret_text(str(exc))[:160]
    finally:
        if client_writable:
            payload = {"kind": "exit", "offset": offset, "reason": reason}
            if reason == "stream_error":
                payload["error"] = error
            try:
                send(payload)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
