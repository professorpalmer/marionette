"""Nous Portal OAuth device-code — Marionette-owned lift from Hermes."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Dict

from .credential_pool import add_oauth_entry
from .diag import note as _diag

DEFAULT_NOUS_PORTAL_URL = "https://portal.nousresearch.com"
DEFAULT_NOUS_INFERENCE_URL = "https://inference-api.nousresearch.com/v1"
DEFAULT_NOUS_CLIENT_ID = "hermes-cli"
DEFAULT_NOUS_SCOPE = "inference:invoke"

_lock = threading.RLock()
_pending: Dict[str, Dict[str, Any]] = {}


def _http_form(url: str, form: Dict[str, str], *, timeout: float = 20.0) -> tuple[int, Any]:
    body = urllib.parse.urlencode(form).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": "marionette-oauth/1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except Exception:
            payload = {"error": raw[:300]}
        return e.code, payload


def start_nous_device_login(*, label: str = "") -> Dict[str, Any]:
    portal = DEFAULT_NOUS_PORTAL_URL.rstrip("/")
    status, payload = _http_form(
        f"{portal}/api/oauth/device/code",
        {"client_id": DEFAULT_NOUS_CLIENT_ID, "scope": DEFAULT_NOUS_SCOPE},
    )
    if status != 200:
        raise RuntimeError(f"Nous device-code request failed (HTTP {status}): {payload}")
    for field in (
        "device_code", "user_code", "verification_uri",
        "expires_in", "interval",
    ):
        if field not in payload:
            raise RuntimeError(f"Nous device-code response missing {field}")
    session_id = uuid.uuid4().hex
    complete = str(
        payload.get("verification_uri_complete")
        or payload["verification_uri"]
    )
    with _lock:
        _pending[session_id] = {
            "provider": "nous",
            "device_code": str(payload["device_code"]),
            "user_code": str(payload["user_code"]),
            "label": (label or "").strip(),
            "started_at": time.time(),
            "expires_in": int(payload.get("expires_in") or 900),
            "status": "pending",
            "portal": portal,
        }
    return {
        "session_id": session_id,
        "provider": "nous",
        "user_code": str(payload["user_code"]),
        "verification_uri": str(payload["verification_uri"]),
        "verification_uri_complete": complete,
        "interval": max(1, int(payload.get("interval") or 1)),
        "expires_in": int(payload.get("expires_in") or 900),
    }


def poll_nous_device_login(session_id: str) -> Dict[str, Any]:
    with _lock:
        sess = _pending.get(session_id)
        if sess is None:
            return {"status": "error", "error": "unknown session_id"}
        if sess["status"] in ("done", "error"):
            return {
                "status": sess["status"],
                "error": sess.get("error"),
                "entry_id": sess.get("entry_id"),
                "provider": "nous",
            }
        if time.time() - float(sess["started_at"]) > float(sess["expires_in"]) + 30:
            sess["status"] = "error"
            sess["error"] = "Login timed out"
            return {"status": "error", "error": sess["error"], "provider": "nous"}
        device_code = sess["device_code"]
        user_code = sess["user_code"]
        label = sess.get("label") or ""
        portal = sess["portal"]

    status, payload = _http_form(
        f"{portal}/api/oauth/token",
        {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": DEFAULT_NOUS_CLIENT_ID,
            "device_code": device_code,
        },
    )
    if status == 200 and payload.get("access_token"):
        expires_in = payload.get("expires_in")
        expires_at_ms = None
        if expires_in is not None:
            try:
                expires_at_ms = int(time.time() * 1000) + int(expires_in) * 1000
            except (TypeError, ValueError):
                expires_at_ms = None
        entry = add_oauth_entry(
            "nous",
            access_token=str(payload["access_token"]),
            refresh_token=payload.get("refresh_token"),
            label=label or "nous",
            expires_at_ms=expires_at_ms,
            source="manual:device_code",
            extra={"base_url": DEFAULT_NOUS_INFERENCE_URL},
        )
        # Mirror into process env + keys.json so classic Nous / OpenAI-compat
        # pilots and Settings has_key see the OAuth token immediately.
        try:
            import os
            from .keys import set_api_key
            os.environ["NOUS_API_KEY"] = entry.access_token
            set_api_key("nous", entry.access_token)
        except Exception as e:
            _diag("oauth_nous.mirror_key", e)
        with _lock:
            s = _pending.get(session_id)
            if s is not None:
                s["status"] = "done"
                s["entry_id"] = entry.id
        return {
            "status": "done",
            "provider": "nous",
            "entry_id": entry.id,
            "label": entry.label,
        }

    err_code = str((payload or {}).get("error") or "")
    if err_code in ("authorization_pending", "slow_down") or status in (400, 401, 403):
        if err_code in ("authorization_pending", "slow_down", ""):
            return {"status": "pending", "provider": "nous", "user_code": user_code}

    err = f"Nous token poll failed (HTTP {status}): {payload}"
    with _lock:
        s = _pending.get(session_id)
        if s is not None:
            s["status"] = "error"
            s["error"] = err
    return {"status": "error", "error": err, "provider": "nous"}


def cancel_nous_device_login(session_id: str) -> Dict[str, Any]:
    with _lock:
        existed = session_id in _pending
        _pending.pop(session_id, None)
    return {"status": "cancelled", "provider": "nous", "cleared": existed}


def clear_pending_for_tests() -> None:
    with _lock:
        _pending.clear()
