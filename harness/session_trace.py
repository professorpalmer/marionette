from __future__ import annotations

"""Opt-in session trace export/upload. Default OFF. No Sentry/OTel."""

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

TRACE_FILENAME = "session_trace.json"
_TRUTHY = ("1", "true", "yes", "on")


def session_trace_export_enabled() -> bool:
    return (os.environ.get("HARNESS_SESSION_TRACE_EXPORT") or "").strip().lower() in _TRUTHY


def session_trace_upload_enabled() -> bool:
    return (os.environ.get("HARNESS_SESSION_TRACE_UPLOAD") or "").strip().lower() in _TRUTHY


def session_trace_upload_url() -> str:
    return (os.environ.get("HARNESS_SESSION_TRACE_UPLOAD_URL") or "").strip()


def _state_dir(session: Any) -> str:
    explicit = str(getattr(session, "state_dir", "") or "")
    if explicit:
        return explicit
    cfg = getattr(session, "config", None)
    return str(getattr(cfg, "state_dir", "") or "")


def build_session_trace(session: Any) -> Dict[str, Any]:
    goal = getattr(session, "_session_goal", None)
    goal_d = goal.to_dict() if goal is not None and hasattr(goal, "to_dict") else {}
    history = getattr(session, "_history", None) or []
    return {
        "exported_at": time.time(),
        "session_id": str(getattr(session, "session_id", "") or ""),
        "goal": goal_d,
        "history_len": len(history) if isinstance(history, list) else 0,
        "tokens_used": int(getattr(session, "_tokens_used", 0) or 0),
        "last_turn_tokens": int(
            getattr(session, "_last_turn_tokens", 0)
            or getattr(session, "_turn_output_tokens", 0)
            or 0
        ),
    }


def export_session_trace(session: Any) -> Optional[Dict[str, Any]]:
    """Write state_dir/session_trace.json when export is opted in. Default no-op."""
    if not session_trace_export_enabled():
        return None
    state_dir = _state_dir(session)
    if not state_dir:
        return None
    payload = build_session_trace(session)
    path = os.path.join(state_dir, TRACE_FILENAME)
    try:
        os.makedirs(state_dir, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(payload, indent=2, ensure_ascii=False))
        os.replace(tmp, path)
    except Exception:
        return None
    uploaded = False
    upload_error = ""
    if session_trace_upload_enabled():
        uploaded, upload_error = _upload_trace(path, payload)
    return {
        "ok": True,
        "path": path,
        "uploaded": uploaded,
        "upload_error": upload_error,
    }


def _upload_trace(path: str, payload: Dict[str, Any]) -> tuple[bool, str]:
    url = session_trace_upload_url()
    if not url:
        return False, "no upload url"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
        return True, ""
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return False, str(exc)


def maybe_export_session_trace(session: Any) -> Optional[Dict[str, Any]]:
    try:
        return export_session_trace(session)
    except Exception:
        return None
