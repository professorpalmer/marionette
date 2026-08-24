"""Opt-in Chrome extension / native-host tab relay.

This is NOT a second browser engine. Pilot CDP still lives in
``harness.browser`` (``puppetmaster.browser_cdp``). The relay only accepts the
same tab snapshot message the unpacked extension and a native messaging host
emit, and records URL / title / optional page text for the harness.

Off by default. Enable with ``PM_BROWSER_RELAY=1``. No marketplace, Sentry,
OTel, or Guardian.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Optional

RELAY_ENV = "PM_BROWSER_RELAY"
MESSAGE_KIND = "tab_snapshot"
MAX_TEXT_CHARS = 65536
MAX_TITLE_CHARS = 2048
MAX_URL_CHARS = 4096
_TRUTHY = ("1", "true", "yes", "on")

_lock = threading.Lock()
_last: Optional[dict[str, Any]] = None


def relay_enabled() -> bool:
    return (os.environ.get(RELAY_ENV) or "").strip().lower() in _TRUTHY


def last_snapshot() -> Optional[dict[str, Any]]:
    with _lock:
        return dict(_last) if _last is not None else None


def clear_snapshot() -> None:
    global _last
    with _lock:
        _last = None


def _clip(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else ("" if value is None else str(value))
    text = text.strip()
    if len(text) > limit:
        return text[:limit]
    return text


def normalize_message(body: Any) -> tuple[Optional[dict[str, Any]], str]:
    """Validate the extension / native-host message. Returns (payload, error)."""
    if not isinstance(body, dict):
        return None, "relay message must be a JSON object"
    kind = body.get("kind", body.get("type", MESSAGE_KIND))
    if kind is None or kind == "":
        kind = MESSAGE_KIND
    if kind != MESSAGE_KIND:
        return None, f"unsupported relay kind: {kind}"
    url = _clip(body.get("url"), MAX_URL_CHARS)
    if not url:
        return None, "url is required"
    lowered = url.lower()
    if not (lowered.startswith("http://") or lowered.startswith("https://")):
        return None, "url must be http(s)"
    title = _clip(body.get("title"), MAX_TITLE_CHARS)
    text = body.get("text")
    page_text = None
    if text is not None and str(text).strip():
        page_text = _clip(text, MAX_TEXT_CHARS)
    tab_id = body.get("tab_id", body.get("tabId"))
    if tab_id is not None and not isinstance(tab_id, (str, int)):
        tab_id = str(tab_id)
    source = _clip(body.get("source"), 64) or "extension"
    if source not in ("extension", "native_host"):
        source = "extension"
    payload: dict[str, Any] = {
        "kind": MESSAGE_KIND,
        "url": url,
        "title": title,
        "text": page_text,
        "tab_id": tab_id,
        "source": source,
        "recorded_at": time.time(),
    }
    return payload, ""


def record_message(body: Any) -> tuple[Optional[dict[str, Any]], str]:
    """Record a snapshot when the relay is enabled."""
    global _last
    if not relay_enabled():
        return None, "browser relay is off"
    payload, err = normalize_message(body)
    if err:
        return None, err
    with _lock:
        _last = payload
    return dict(payload), ""
