"""xAI / Grok SuperGrok OAuth device-code — Marionette-owned lift from Hermes."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Dict, Optional

from .credential_pool import add_oauth_entry
from .diag import note as _diag

XAI_OAUTH_ISSUER = "https://auth.x.ai"
XAI_OAUTH_DISCOVERY_URL = f"{XAI_OAUTH_ISSUER}/.well-known/openid-configuration"
XAI_OAUTH_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
XAI_OAUTH_SCOPE = "openid profile email offline_access grok-cli:access api:access"
XAI_OAUTH_DEVICE_CODE_URL = f"{XAI_OAUTH_ISSUER}/oauth2/device/code"
DEFAULT_XAI_BASE_URL = "https://api.x.ai/v1"

_lock = threading.RLock()
_pending: Dict[str, Dict[str, Any]] = {}
_token_endpoint_cache: Optional[str] = None


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


def _token_endpoint() -> str:
    global _token_endpoint_cache
    if _token_endpoint_cache:
        return _token_endpoint_cache
    try:
        req = urllib.request.Request(
            XAI_OAUTH_DISCOVERY_URL,
            headers={"Accept": "application/json", "User-Agent": "marionette-oauth/1"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        ep = str(data.get("token_endpoint") or "").strip()
        if ep:
            _token_endpoint_cache = ep
            return ep
    except Exception as e:
        _diag("oauth_xai.discovery", e)
    return f"{XAI_OAUTH_ISSUER}/oauth2/token"


def start_xai_device_login(*, label: str = "") -> Dict[str, Any]:
    status, payload = _http_form(
        XAI_OAUTH_DEVICE_CODE_URL,
        {"client_id": XAI_OAUTH_CLIENT_ID, "scope": XAI_OAUTH_SCOPE},
    )
    if status != 200:
        raise RuntimeError(f"xAI device-code request failed (HTTP {status}): {payload}")
    for field in (
        "device_code", "user_code", "verification_uri",
        "verification_uri_complete", "expires_in", "interval",
    ):
        if field not in payload:
            raise RuntimeError(f"xAI device-code response missing {field}")
    session_id = uuid.uuid4().hex
    with _lock:
        _pending[session_id] = {
            "provider": "xai-oauth",
            "device_code": str(payload["device_code"]),
            "user_code": str(payload["user_code"]),
            "label": (label or "").strip(),
            "started_at": time.time(),
            "expires_in": int(payload.get("expires_in") or 900),
            "interval": max(1, int(payload.get("interval") or 5)),
            "status": "pending",
            "token_endpoint": _token_endpoint(),
        }
    return {
        "session_id": session_id,
        "provider": "xai-oauth",
        "user_code": str(payload["user_code"]),
        "verification_uri": str(payload["verification_uri"]),
        "verification_uri_complete": str(payload["verification_uri_complete"]),
        "interval": int(payload.get("interval") or 5),
        "expires_in": int(payload.get("expires_in") or 900),
    }


def poll_xai_device_login(session_id: str) -> Dict[str, Any]:
    with _lock:
        sess = _pending.get(session_id)
        if sess is None:
            return {"status": "error", "error": "unknown session_id"}
        if sess["status"] in ("done", "error"):
            return {
                "status": sess["status"],
                "error": sess.get("error"),
                "entry_id": sess.get("entry_id"),
                "provider": "xai-oauth",
            }
        if time.time() - float(sess["started_at"]) > float(sess["expires_in"]) + 30:
            sess["status"] = "error"
            sess["error"] = "Login timed out"
            return {"status": "error", "error": sess["error"], "provider": "xai-oauth"}
        device_code = sess["device_code"]
        user_code = sess["user_code"]
        label = sess.get("label") or ""
        token_ep = sess["token_endpoint"]

    status, payload = _http_form(
        token_ep,
        {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": XAI_OAUTH_CLIENT_ID,
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
            "xai-oauth",
            access_token=str(payload["access_token"]),
            refresh_token=payload.get("refresh_token"),
            label=label or "xai-oauth",
            expires_at_ms=expires_at_ms,
            source="manual:device_code",
            extra={"base_url": DEFAULT_XAI_BASE_URL},
        )
        # Also mirror into xai API key env for OpenAI-compat pilot path.
        try:
            import os
            from .keys import set_api_key
            os.environ["XAI_API_KEY"] = entry.access_token
            set_api_key("xai", entry.access_token)
        except Exception as e:
            _diag("oauth_xai.mirror_key", e)
        with _lock:
            s = _pending.get(session_id)
            if s is not None:
                s["status"] = "done"
                s["entry_id"] = entry.id
        return {
            "status": "done",
            "provider": "xai-oauth",
            "entry_id": entry.id,
            "label": entry.label,
        }

    err_code = str((payload or {}).get("error") or "")
    if status in (400, 401, 403) and err_code in (
        "authorization_pending", "slow_down",
    ):
        return {
            "status": "pending",
            "provider": "xai-oauth",
            "user_code": user_code,
        }
    # Some servers return 200 with pending? Unlikely. Treat other errors as hard.
    if err_code == "authorization_pending" or err_code == "slow_down":
        return {"status": "pending", "provider": "xai-oauth", "user_code": user_code}

    err = f"xAI token poll failed (HTTP {status}): {payload}"
    with _lock:
        s = _pending.get(session_id)
        if s is not None:
            s["status"] = "error"
            s["error"] = err
    return {"status": "error", "error": err, "provider": "xai-oauth"}


def cancel_xai_device_login(session_id: str) -> Dict[str, Any]:
    with _lock:
        existed = session_id in _pending
        _pending.pop(session_id, None)
    return {"status": "cancelled", "provider": "xai-oauth", "cleared": existed}


def clear_pending_for_tests() -> None:
    with _lock:
        _pending.clear()
