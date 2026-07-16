"""Anthropic Claude Pro/Max PKCE OAuth — Marionette-owned lift from Hermes.

Browser opens claude.ai authorize URL; user pastes ``code#state`` back.
Token exchange hits platform.claude.com (console fallback). Token-endpoint
UA must NOT be ``claude-code/`` (Anthropic 429s that prefix).
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Dict, Optional

from .credential_pool import add_oauth_entry
from .diag import note as _diag

_OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
_OAUTH_TOKEN_URLS = (
    "https://platform.claude.com/v1/oauth/token",
    "https://console.anthropic.com/v1/oauth/token",
)
_OAUTH_TOKEN_USER_AGENT = "axios/1.7.9"
_OAUTH_REDIRECT_URI = "https://console.anthropic.com/oauth/code/callback"
_OAUTH_SCOPES = "org:create_api_key user:profile user:inference"

_lock = threading.RLock()
_pending: Dict[str, Dict[str, Any]] = {}


def _generate_pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def is_anthropic_oauth_token(key: str) -> bool:
    """True for OAuth/setup tokens that need Bearer auth (not x-api-key)."""
    if not key:
        return False
    if key.startswith("sk-ant-api"):
        return False
    if key.startswith("sk-ant-"):
        return True
    if key.startswith("eyJ"):
        return True
    if key.startswith("cc-"):
        return True
    return False


def start_anthropic_pkce_login(*, label: str = "") -> Dict[str, Any]:
    verifier, challenge = _generate_pkce()
    oauth_state = secrets.token_urlsafe(32)
    params = {
        "code": "true",
        "client_id": _OAUTH_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": _OAUTH_REDIRECT_URI,
        "scope": _OAUTH_SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": oauth_state,
    }
    auth_url = "https://claude.ai/oauth/authorize?" + urllib.parse.urlencode(params)
    session_id = uuid.uuid4().hex
    with _lock:
        _pending[session_id] = {
            "provider": "anthropic",
            "verifier": verifier,
            "state": oauth_state,
            "label": (label or "").strip(),
            "started_at": time.time(),
            "status": "pending",
        }
    return {
        "session_id": session_id,
        "provider": "anthropic",
        "auth_url": auth_url,
        "flow": "pkce_paste",
        "expires_in": 10 * 60,
        "hint": "Authorize in the browser, then paste the code (code#state) to complete.",
    }


def complete_anthropic_pkce_login(session_id: str, auth_code: str) -> Dict[str, Any]:
    """Exchange pasted authorization code for tokens and pool them."""
    with _lock:
        sess = _pending.get(session_id)
        if sess is None:
            return {"status": "error", "error": "unknown session_id"}
        if sess.get("status") == "done":
            return {
                "status": "done",
                "provider": "anthropic",
                "entry_id": sess.get("entry_id"),
            }
        if time.time() - float(sess["started_at"]) > 10 * 60:
            sess["status"] = "error"
            return {"status": "error", "error": "Login timed out", "provider": "anthropic"}
        verifier = sess["verifier"]
        expected_state = sess["state"]
        label = sess.get("label") or ""

    raw = (auth_code or "").strip()
    if not raw:
        return {"status": "error", "error": "authorization code required", "provider": "anthropic"}
    splits = raw.split("#")
    code = splits[0].strip()
    received_state = splits[1].strip() if len(splits) > 1 else ""
    if received_state and received_state != expected_state:
        return {"status": "error", "error": "OAuth state mismatch", "provider": "anthropic"}
    state_for_exchange = received_state or expected_state

    exchange_data = json.dumps({
        "grant_type": "authorization_code",
        "client_id": _OAUTH_CLIENT_ID,
        "code": code,
        "state": state_for_exchange,
        "redirect_uri": _OAUTH_REDIRECT_URI,
        "code_verifier": verifier,
    }).encode("utf-8")

    result = None
    last_err: Optional[Exception] = None
    for endpoint in _OAUTH_TOKEN_URLS:
        req = urllib.request.Request(
            endpoint,
            data=exchange_data,
            headers={
                "Content-Type": "application/json",
                "User-Agent": _OAUTH_TOKEN_USER_AGENT,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            break
        except Exception as exc:
            last_err = exc
            _diag("oauth_anthropic.token_exchange", exc)
            continue

    if result is None:
        err = f"Token exchange failed: {last_err}"
        with _lock:
            s = _pending.get(session_id)
            if s is not None:
                s["status"] = "error"
                s["error"] = err
        return {"status": "error", "error": err, "provider": "anthropic"}

    access_token = str(result.get("access_token") or "")
    refresh_token = result.get("refresh_token")
    expires_in = int(result.get("expires_in") or 3600)
    if not access_token:
        return {"status": "error", "error": "No access_token in response", "provider": "anthropic"}

    expires_at_ms = int(time.time() * 1000) + expires_in * 1000
    try:
        entry = add_oauth_entry(
            "anthropic",
            access_token=access_token,
            refresh_token=refresh_token if isinstance(refresh_token, str) else None,
            label=label or "claude-max",
            expires_at_ms=expires_at_ms,
            source="manual:pkce",
        )
    except Exception as e:
        return {"status": "error", "error": str(e), "provider": "anthropic"}

    # Mirror into process env so classic AnthropicDriver key_env paths see it.
    try:
        import os
        os.environ["ANTHROPIC_API_KEY"] = access_token
        from .keys import set_api_key
        # Avoid wiping pool via set_api_key's add — already pooled; just keys.json.
        # Use set_api_key for has_key UI consistency.
        set_api_key("anthropic", access_token)
    except Exception as e:
        _diag("oauth_anthropic.mirror_key", e)

    with _lock:
        s = _pending.get(session_id)
        if s is not None:
            s["status"] = "done"
            s["entry_id"] = entry.id
    return {
        "status": "done",
        "provider": "anthropic",
        "entry_id": entry.id,
        "label": entry.label,
    }


def cancel_anthropic_pkce_login(session_id: str) -> Dict[str, Any]:
    """Drop a pending PKCE session so Sign-in can start clean."""
    with _lock:
        existed = session_id in _pending
        _pending.pop(session_id, None)
    return {
        "status": "cancelled",
        "provider": "anthropic",
        "cleared": existed,
    }


def clear_pending_for_tests() -> None:
    with _lock:
        _pending.clear()
