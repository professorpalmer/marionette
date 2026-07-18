"""Intentional backend-restart signal shared by /api/restart and Electron.

POST /api/restart self-terminates after persisting. Electron's child-exit
handler would otherwise treat that exit as an unexpected crash. Writing this
marker lets Electron classify the exit as an intentional restart, unlink the
owned backend.json marker, and respawn without crash-loop accounting.
"""
from __future__ import annotations

import json
import os
import time
from typing import Optional

SIGNAL_NAME = "backend-restart.json"


def _signal_dir(explicit: Optional[str] = None) -> str:
    if explicit:
        return explicit
    env = (os.environ.get("HARNESS_STATE_DIR") or "").strip()
    if env:
        return env
    root = os.path.expanduser("~/.pmharness")
    durable = os.path.join(root, "state")
    if os.path.isdir(durable):
        return durable
    return root


def write_intentional_restart_signal(
    state_dir: Optional[str] = None,
    *,
    pid: Optional[int] = None,
) -> str:
    """Write a fresh restart signal; return the path written (best-effort)."""
    directory = _signal_dir(state_dir)
    path = os.path.join(directory, SIGNAL_NAME)
    payload = {
        "at": int(time.time() * 1000),
        "pid": int(pid if pid is not None else os.getpid()),
        "reason": "api_restart",
    }
    try:
        os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh)
    except Exception:
        pass
    return path


def clear_intentional_restart_signal(state_dir: Optional[str] = None) -> None:
    path = os.path.join(_signal_dir(state_dir), SIGNAL_NAME)
    try:
        os.remove(path)
    except FileNotFoundError:
        return
    except Exception:
        pass
