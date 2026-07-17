"""Terminal control HTTP route bodies (peeled from ``harness.server``).

SSE ``/api/terminal/stream`` stays on Handler (needs streaming I/O helpers).
"""

from __future__ import annotations

import os
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
        cols = int(body.get("cols", 80))
        rows = int(body.get("rows", 24))
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
