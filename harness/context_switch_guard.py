"""Latch that blocks destructive commands after a context switch.

After a session / repo / profile / workspace switch, the new workspace is
unconfirmed. ``classify_command`` danger verdicts are then blocked until
the operator confirms the new workspace. Safe (non-danger) commands still
run while the latch is armed.

Process-wide and PM-free so unit tests stay hermetic.
"""
from __future__ import annotations

import os
import threading
from typing import Any

_lock = threading.Lock()
_armed: bool = False
_kind: str = ""
_old: str = ""
_new: str = ""


def _norm(path: str) -> str:
    raw = (path or "").strip()
    if not raw:
        return ""
    try:
        return os.path.normcase(os.path.realpath(raw))
    except Exception:
        return raw


def reset_for_tests() -> None:
    """Drop latch state (test helper)."""
    global _armed, _kind, _old, _new
    with _lock:
        _armed = False
        _kind = ""
        _old = ""
        _new = ""


def note_switch(kind: str, old: str = "", new: str = "") -> dict[str, Any]:
    """Arm the latch after a context switch.

    Same-root no-ops (normalized old == new, both non-empty) do not arm.
    An empty old (first bind) still arms so the new workspace is confirmed.
    """
    global _armed, _kind, _old, _new
    kind_s = (kind or "").strip() or "switch"
    old_s = (old or "").strip()
    new_s = (new or "").strip()
    old_n = _norm(old_s)
    new_n = _norm(new_s)
    if old_n and new_n and old_n == new_n:
        return snapshot()
    with _lock:
        _armed = True
        _kind = kind_s
        _old = old_s
        _new = new_s
    return snapshot()


def is_armed() -> bool:
    with _lock:
        return _armed


def snapshot() -> dict[str, Any]:
    with _lock:
        return {
            "armed": _armed,
            "kind": _kind,
            "old": _old,
            "new": _new,
        }


def confirm_workspace(workspace_root: str = "") -> dict[str, Any]:
    """Clear the latch. Optional workspace_root is recorded as the confirmed root."""
    global _armed, _kind, _old, _new
    root = (workspace_root or "").strip()
    with _lock:
        if root:
            _new = root
        _armed = False
        _kind = ""
        _old = ""
    return snapshot()
