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


def stream_terminal(handler: Any, sid: str, svc: TerminalServices) -> None:
    """Stream PTY output over SSE (GET /api/terminal/stream).

    Client sends keystrokes via POST /api/terminal/write. Preserves data/exit
    frames and BrokenPipe/ConnectionReset detach handling.
    """
    sess = svc.pty.get(sid)
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler._cors()
    handler.end_headers()
    if not sess:
        try:
            handler.wfile.write(b"data: {\"kind\": \"exit\"}\n\n")
            handler.wfile.flush()
        except Exception:
            pass
        return
    offset = 0
    try:
        while sess.alive():
            data, offset = sess.read_since(offset)
            if data:
                import base64 as _b64
                payload = json.dumps({
                    "kind": "data",
                    "b64": _b64.b64encode(data).decode("ascii"),
                })
                handler.wfile.write(f"data: {payload}\n\n".encode())
                handler.wfile.flush()
            else:
                time.sleep(0.05)
        # flush any final bytes after exit
        data, offset = sess.read_since(offset)
        if data:
            import base64 as _b64
            payload = json.dumps({
                "kind": "data",
                "b64": _b64.b64encode(data).decode("ascii"),
            })
            handler.wfile.write(f"data: {payload}\n\n".encode())
    except (BrokenPipeError, ConnectionResetError):
        return
    except Exception:
        # Still try to emit exit below so the renderer does not see a bare
        # stream close (EXITED with no prior exit frame).
        pass
    try:
        handler.wfile.write(b"data: {\"kind\": \"exit\"}\n\n")
        handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
