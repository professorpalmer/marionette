"""OpenAI Codex (ChatGPT plan) device-code OAuth — Marionette-owned lift.

Flow mirrors Hermes ``_codex_device_code_login`` (auth.openai.com deviceauth)
but is stdlib-only and split into start/poll steps so the harness HTTP
server never blocks for the full browser wait.
"""

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

CODEX_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_OAUTH_ISSUER = "https://auth.openai.com"
CODEX_OAUTH_TOKEN_URL = f"{CODEX_OAUTH_ISSUER}/oauth/token"
CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
CODEX_DEVICE_URL = f"{CODEX_OAUTH_ISSUER}/codex/device"

_lock = threading.RLock()
_pending: Dict[str, Dict[str, Any]] = {}


def _http_json(
    method: str,
    url: str,
    *,
    data: Any = None,
    form: Optional[Dict[str, str]] = None,
    timeout: float = 20.0,
) -> tuple[int, Any]:
    headers = {"Accept": "application/json", "User-Agent": "marionette-oauth/1"}
    body = None
    if form is not None:
        body = urllib.parse.urlencode(form).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
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


def start_codex_device_login(*, label: str = "") -> Dict[str, Any]:
    """Request a device code. Returns session_id + user_code for the UI."""
    status, payload = _http_json(
        "POST",
        f"{CODEX_OAUTH_ISSUER}/api/accounts/deviceauth/usercode",
        data={"client_id": CODEX_OAUTH_CLIENT_ID},
    )
    if status == 429:
        raise RuntimeError(
            "OpenAI is rate-limiting Codex login (HTTP 429). Wait a minute and retry."
        )
    if status != 200:
        raise RuntimeError(f"Device code request failed (HTTP {status}): {payload}")
    user_code = str(payload.get("user_code") or "")
    device_auth_id = str(payload.get("device_auth_id") or "")
    interval = max(3, int(payload.get("interval") or 5))
    if not user_code or not device_auth_id:
        raise RuntimeError("Device code response missing required fields")
    session_id = uuid.uuid4().hex
    with _lock:
        _pending[session_id] = {
            "provider": "openai-codex",
            "user_code": user_code,
            "device_auth_id": device_auth_id,
            "interval": interval,
            "label": (label or "").strip(),
            "started_at": time.time(),
            "status": "pending",
            "error": None,
            "entry_id": None,
        }
    return {
        "session_id": session_id,
        "provider": "openai-codex",
        "user_code": user_code,
        "verification_uri": CODEX_DEVICE_URL,
        "verification_uri_complete": f"{CODEX_DEVICE_URL}?user_code={urllib.parse.quote(user_code)}",
        "interval": interval,
        "expires_in": 15 * 60,
    }


def poll_codex_device_login(session_id: str) -> Dict[str, Any]:
    """One poll step. When approved, exchanges tokens and pools the credential."""
    with _lock:
        sess = _pending.get(session_id)
        if sess is None:
            return {"status": "error", "error": "unknown session_id"}
        if sess["status"] in ("done", "error"):
            return {
                "status": sess["status"],
                "error": sess.get("error"),
                "entry_id": sess.get("entry_id"),
                "provider": "openai-codex",
            }
        if time.time() - float(sess["started_at"]) > 15 * 60:
            sess["status"] = "error"
            sess["error"] = "Login timed out after 15 minutes"
            return {"status": "error", "error": sess["error"], "provider": "openai-codex"}
        device_auth_id = sess["device_auth_id"]
        user_code = sess["user_code"]
        label = sess.get("label") or ""

    status, payload = _http_json(
        "POST",
        f"{CODEX_OAUTH_ISSUER}/api/accounts/deviceauth/token",
        data={
            "device_auth_id": device_auth_id,
            "user_code": user_code,
        },
    )
    if status in (403, 404):
        return {
            "status": "pending",
            "provider": "openai-codex",
            "user_code": user_code,
            "verification_uri": CODEX_DEVICE_URL,
        }
    if status != 200:
        detail = ""
        if isinstance(payload, dict):
            detail = str(
                payload.get("error_description")
                or payload.get("detail")
                or payload.get("message")
                or payload.get("error")
                or payload
            )[:400]
        err = f"Device auth poll failed (HTTP {status})"
        if detail:
            err = f"{err}: {detail}"
        low = (detail or "").lower()
        if any(
            s in low
            for s in (
                "device",
                "not enabled",
                "disabled",
                "not allowed",
                "access code",
                "chatgpt",
            )
        ):
            err += (
                " — In ChatGPT settings, enable device / Codex login codes, "
                "then click Sign in again."
            )
        with _lock:
            # Drop the dead session so the next Sign-in starts clean.
            _pending.pop(session_id, None)
        return {"status": "error", "error": err, "provider": "openai-codex", "retryable": True}

    authorization_code = str(payload.get("authorization_code") or "")
    code_verifier = str(payload.get("code_verifier") or "")
    if not authorization_code or not code_verifier:
        err = "Device auth response missing authorization_code or code_verifier"
        with _lock:
            s = _pending.get(session_id)
            if s is not None:
                s["status"] = "error"
                s["error"] = err
        return {"status": "error", "error": err, "provider": "openai-codex"}

    tok_status, tok = _http_json(
        "POST",
        CODEX_OAUTH_TOKEN_URL,
        form={
            "grant_type": "authorization_code",
            "code": authorization_code,
            "redirect_uri": f"{CODEX_OAUTH_ISSUER}/deviceauth/callback",
            "client_id": CODEX_OAUTH_CLIENT_ID,
            "code_verifier": code_verifier,
        },
    )
    if tok_status != 200 or not tok.get("access_token"):
        err = f"Token exchange failed (HTTP {tok_status}): {tok}"
        with _lock:
            s = _pending.get(session_id)
            if s is not None:
                s["status"] = "error"
                s["error"] = err
        return {"status": "error", "error": err, "provider": "openai-codex"}

    expires_in = tok.get("expires_in")
    expires_at_ms = None
    if expires_in is not None:
        try:
            expires_at_ms = int(time.time() * 1000) + int(expires_in) * 1000
        except (TypeError, ValueError):
            expires_at_ms = None

    try:
        entry = add_oauth_entry(
            "openai-codex",
            access_token=str(tok["access_token"]),
            refresh_token=tok.get("refresh_token"),
            label=label or "chatgpt-codex",
            expires_at_ms=expires_at_ms,
            source="manual:device_code",
            extra={"base_url": CODEX_BASE_URL},
        )
    except Exception as e:
        _diag("oauth_codex.pool_add", e)
        with _lock:
            s = _pending.get(session_id)
            if s is not None:
                s["status"] = "error"
                s["error"] = str(e)
        return {"status": "error", "error": str(e), "provider": "openai-codex"}

    with _lock:
        s = _pending.get(session_id)
        if s is not None:
            s["status"] = "done"
            s["entry_id"] = entry.id
            s["error"] = None
    return {
        "status": "done",
        "provider": "openai-codex",
        "entry_id": entry.id,
        "label": entry.label,
    }


def cancel_codex_device_login(session_id: str) -> Dict[str, Any]:
    """Drop a pending device-login session so Sign-in can start clean."""
    with _lock:
        existed = session_id in _pending
        _pending.pop(session_id, None)
    return {
        "status": "cancelled",
        "provider": "openai-codex",
        "cleared": existed,
    }


def clear_pending_for_tests() -> None:
    with _lock:
        _pending.clear()
