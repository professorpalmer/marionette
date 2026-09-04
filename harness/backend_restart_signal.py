"""Intentional backend-restart signal shared by /api/restart and Electron.

POST /api/restart self-terminates after persisting. Electron's child-exit
handler would otherwise treat that exit as an unexpected crash. Writing this
marker lets Electron classify the exit as an intentional restart, unlink the
owned backend.json marker, and relaunch the whole app (not a Python-only
respawn, and not crash-loop accounting).

A sibling outcome file records whether prepare succeeded and whether the new
process came back. Electron still consumes the signal; boot must not delete it.
"""
from __future__ import annotations

import json
import os
import time
from typing import Optional

SIGNAL_NAME = "backend-restart.json"
OUTCOME_NAME = "backend-restart-outcome.json"
# Match Electron isFreshIntentionalRestartSignal (default 30s).
SIGNAL_MAX_AGE_MS = 30_000
_ALLOWED_OUTCOMES = frozenset({"ok", "failed", "pending"})


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


def _read_json_object(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return data


def _write_json_object(directory: str, name: str, payload: dict) -> str:
    path = os.path.join(directory, name)
    try:
        os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh)
    except Exception:
        pass
    return path


def write_intentional_restart_signal(
    state_dir: Optional[str] = None,
    *,
    pid: Optional[int] = None,
) -> str:
    """Write a fresh restart signal; return the path written (best-effort)."""
    directory = _signal_dir(state_dir)
    payload = {
        "at": int(time.time() * 1000),
        "pid": int(pid if pid is not None else os.getpid()),
        "reason": "api_restart",
    }
    return _write_json_object(directory, SIGNAL_NAME, payload)


def clear_intentional_restart_signal(state_dir: Optional[str] = None) -> None:
    path = os.path.join(_signal_dir(state_dir), SIGNAL_NAME)
    try:
        os.remove(path)
    except FileNotFoundError:
        return
    except Exception:
        pass


def read_intentional_restart_signal(state_dir: Optional[str] = None) -> Optional[dict]:
    """Return the signal object, or None when missing or corrupt."""
    return _read_json_object(os.path.join(_signal_dir(state_dir), SIGNAL_NAME))


def is_fresh_intentional_restart_signal(
    payload: Optional[dict],
    *,
    now_ms: Optional[int] = None,
    max_age_ms: int = SIGNAL_MAX_AGE_MS,
) -> bool:
    """Same freshness rule as Electron ``isFreshIntentionalRestartSignal``."""
    if not payload or not isinstance(payload, dict):
        return False
    try:
        at = int(payload.get("at"))
    except (TypeError, ValueError):
        return False
    if at <= 0:
        return False
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    return now - at <= max_age_ms


def write_restart_outcome(
    state_dir: Optional[str] = None,
    *,
    requested_at,
    requested_pid,
    prepared_ok,
    outcome,
    error: str = "",
) -> str:
    """Write backend-restart-outcome.json. Fail-soft: never raise on IO."""
    directory = _signal_dir(state_dir)
    kind = str(outcome or "").strip()
    err = error or ""
    if kind not in _ALLOWED_OUTCOMES:
        kind = "failed"
        if not err:
            err = "invalid_outcome"
    try:
        requested_at_ms = int(requested_at)
    except (TypeError, ValueError):
        requested_at_ms = 0
    try:
        requested_pid_i = int(requested_pid)
    except (TypeError, ValueError):
        requested_pid_i = 0
    payload = {
        "requested_at": requested_at_ms,
        "requested_pid": requested_pid_i,
        "prepared_ok": bool(prepared_ok),
        "outcome": kind,
        "error": err,
    }
    return _write_json_object(directory, OUTCOME_NAME, payload)


def read_restart_outcome(state_dir: Optional[str] = None) -> Optional[dict]:
    """Return the last restart outcome, or None when missing or corrupt."""
    return _read_json_object(os.path.join(_signal_dir(state_dir), OUTCOME_NAME))


def record_boot_restart_outcome(state_dir: Optional[str] = None) -> Optional[str]:
    """Record outcome ok when a fresh intentional signal is present.

    Leaves the signal in place so Electron can still consume it. Idempotent:
    does not overwrite a non-pending outcome for the same requested_at.
    Pending for that request may be upgraded to ok. Fail-soft.
    """
    try:
        signal = read_intentional_restart_signal(state_dir)
        if not is_fresh_intentional_restart_signal(signal):
            return None
        try:
            requested_at = int(signal.get("at"))
        except (TypeError, ValueError):
            return None
        try:
            requested_pid = int(signal.get("pid"))
        except (TypeError, ValueError):
            requested_pid = 0
        existing = read_restart_outcome(state_dir)
        if existing is not None:
            try:
                existing_at = int(existing.get("requested_at"))
            except (TypeError, ValueError):
                existing_at = None
            if existing_at == requested_at and existing.get("outcome") != "pending":
                return os.path.join(_signal_dir(state_dir), OUTCOME_NAME)
        return write_restart_outcome(
            state_dir,
            requested_at=requested_at,
            requested_pid=requested_pid,
            prepared_ok=True,
            outcome="ok",
        )
    except Exception:
        return None


def get_restart_last(state_dir: Optional[str] = None) -> tuple[int, dict]:
    """GET /api/restart/last — last durable restart outcome, if any."""
    return 200, {"ok": True, "restart_outcome": read_restart_outcome(state_dir)}
