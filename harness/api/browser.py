"""HTTP bodies for the opt-in Chrome browser relay."""
from __future__ import annotations

from typing import Union

JsonPayload = Union[dict, list]


def get_browser_relay() -> tuple[int, JsonPayload]:
    """GET /api/browser/relay — last recorded tab snapshot (or empty)."""
    from ..browser_relay import last_snapshot, relay_enabled

    return 200, {
        "enabled": relay_enabled(),
        "snapshot": last_snapshot(),
    }


def post_browser_relay(body: dict) -> tuple[int, JsonPayload]:
    """POST /api/browser/relay — record extension / native-host snapshot."""
    from ..browser_relay import record_message, relay_enabled

    if not relay_enabled():
        return 403, {"error": "browser relay is off", "enabled": False}
    payload, err = record_message(body or {})
    if err:
        return 400, {"error": err, "enabled": True}
    return 200, {"ok": True, "enabled": True, "snapshot": payload}
