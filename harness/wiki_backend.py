"""Auto-start the local Portable LLM Wiki backend so the wiki panel connects
without the user launching uvicorn in a terminal.

Marionette is only a *client* of the wiki (it reads WIKI_API_BASE). When that
base points at a local host and the backend is down, this starts it from the
user's wiki checkout -- if one is present -- and waits for /healthz. When no
local wiki install exists (the common case for a fresh public install), it does
nothing and the panel simply shows "not connected", which is correct: we cannot
ship someone else's personal wiki (its data and git remote are theirs).

Discovery order for the backend dir:
  1. $MARIONETTE_WIKI_DIR (explicit override)
  2. ~/portable-llm-wiki/backend (the standard checkout layout)

The backend is spawned in its own session (start_new_session) so it survives
Marionette backend respawns and stays available to other clients (e.g. the
Cursor wiki MCP). A prior instance is detected via /healthz, so we never
double-start.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
import urllib.request
from urllib.parse import urlparse

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
_started_proc = None
_ensure_lock = threading.Lock()


def _wiki_base() -> str:
    return (
        os.environ.get("WIKI_API_BASE")
        or os.environ.get("HARNESS_WIKI_URL")
        or ""
    ).strip().rstrip("/")


def _is_local(base: str) -> bool:
    try:
        return (urlparse(base).hostname or "") in _LOCAL_HOSTS
    except Exception:
        return False


def _healthz(base: str, timeout: float = 2.0) -> bool:
    for path in ("/healthz", "/health"):
        try:
            with urllib.request.urlopen(base + path, timeout=timeout) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            continue
    return False


def _backend_dir() -> str | None:
    candidates = []
    override = os.environ.get("MARIONETTE_WIKI_DIR", "").strip()
    if override:
        candidates.append(override)
    candidates.append(os.path.expanduser("~/portable-llm-wiki/backend"))
    for path in candidates:
        if path and os.path.isfile(os.path.join(path, "app", "main.py")):
            return path
    return None


def _uvicorn_cmd(backend_dir: str, port: int) -> list[str] | None:
    venv_uvicorn = os.path.join(backend_dir, ".venv", "bin", "uvicorn")
    if os.path.isfile(venv_uvicorn):
        prefix = [venv_uvicorn]
    else:
        venv_py = os.path.join(backend_dir, ".venv", "bin", "python")
        py = venv_py if os.path.isfile(venv_py) else (
            shutil.which("python3") or shutil.which("python"))
        if not py:
            return None
        prefix = [py, "-m", "uvicorn"]
    return prefix + ["app.main:app", "--host", "127.0.0.1", "--port", str(port)]


def ensure_wiki_backend_running(wait_secs: float = 12.0) -> dict:
    """Start the local wiki backend if configured, present, and not already up.

    Returns a small status dict; never raises. Safe to call repeatedly.
    """
    global _started_proc
    with _ensure_lock:
        base = _wiki_base()
        if not base or not _is_local(base):
            return {"started": False, "reason": "no local wiki configured"}
        if _healthz(base):
            return {"started": False, "reason": "already running"}

        backend_dir = _backend_dir()
        if not backend_dir:
            return {"started": False, "reason": "no local wiki install found"}

        port = urlparse(base).port or 8000
        cmd = _uvicorn_cmd(backend_dir, port)
        if not cmd:
            return {"started": False, "reason": "uvicorn/python not found"}

        try:
            log_path = os.path.expanduser("~/.pmharness/wiki-backend.log")
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            log = open(log_path, "ab", buffering=0)
        except Exception:
            log = subprocess.DEVNULL

        try:
            _started_proc = subprocess.Popen(
                cmd,
                cwd=backend_dir,
                stdout=log,
                stderr=log,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception as exc:
            return {"started": False, "reason": f"spawn failed: {exc}"}

        deadline = time.monotonic() + wait_secs
        while time.monotonic() < deadline:
            if _healthz(base, timeout=1.5):
                return {"started": True, "reason": "backend up",
                        "dir": backend_dir, "port": port}
            if _started_proc.poll() is not None:
                return {"started": False, "reason": "backend exited during startup"}
            time.sleep(0.5)
        return {"started": False, "reason": "timeout waiting for /healthz"}


def ensure_wiki_backend_async() -> None:
    """Fire ensure_wiki_backend_running on a daemon thread so startup never
    blocks on the wiki health wait."""
    threading.Thread(
        target=lambda: ensure_wiki_backend_running(),
        daemon=True,
    ).start()
