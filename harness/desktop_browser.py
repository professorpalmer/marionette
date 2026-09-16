"""Authenticated, bounded client for the currently attached Electron shell."""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Optional, Tuple

_endpoint: Optional[Tuple[int, str]] = None
MAX_RESPONSE_BYTES = 262144


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def configure(port: int, token: str) -> None:
    """Registered by Electron main after spawn OR authenticated backend reuse."""
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("invalid desktop browser port")
    if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{64}", token):
        raise ValueError("invalid desktop browser token")
    global _endpoint
    _endpoint = (port, token)


def configured() -> bool:
    return _endpoint is not None


def available() -> bool:
    return configured()


def call(session_id: str, action: str, arguments: Optional[dict] = None) -> str:
    """No retries or standalone fallback: a failed action may have executed."""
    endpoint = _endpoint
    if endpoint is None:
        return "desktop browser bridge unavailable"
    if not session_id:
        return "desktop browser requires an active conversation"
    if action == "navigate":
        from .url_safety import is_safe_browser_url
        ok, reason = is_safe_browser_url((arguments or {}).get("url", ""))
        if not ok:
            return "browser navigation blocked: " + reason
    port, token = endpoint
    payload = json.dumps({"session_id": session_id, "action": action, "arguments": arguments or {}}).encode()
    request = urllib.request.Request(
        "http://127.0.0.1:%s/browser" % port, data=payload, method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
    )
    try:
        # Never send the local bridge credential through an HTTP proxy.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        try:
            response = opener.open(request, timeout=17)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            return "desktop browser bridge failed: response too large"
        body = json.loads(raw.decode("utf-8"))
        if not isinstance(body, dict) or body.get("ok") is not True:
            error = body.get("error", "invalid response") if isinstance(body, dict) else "invalid response"
            return "desktop browser bridge failed: %s" % error
        result = body.get("result")
        return result if isinstance(result, str) else json.dumps(result, separators=(",", ":"))
    except (urllib.error.URLError, ValueError, OSError) as exc:
        return "desktop browser bridge failed: %s. Inspect the page before retrying an action." % exc
