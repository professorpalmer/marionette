"""Lifecycle-hooks HTTP route bodies (peeled from ``harness.server``)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Callable, Union

from .redaction import redact_api_secrets


@dataclass
class HooksServices:
    """Explicit deps for hooks HTTP handlers."""

    parse_bool: Callable[[Any], bool]


JsonPayload = Union[dict, list]


def get_hooks() -> tuple[int, JsonPayload]:
    """GET /api/hooks."""
    from .. import hooks as _hk
    return 200, redact_api_secrets({
        "hooks": _hk.get_hooks(),
        "events": _hk.ALLOWED_EVENTS,
    })


def post_hooks_add(body: dict) -> tuple[int, JsonPayload]:
    """POST /api/hooks/add."""
    from .. import hooks as _hk
    event = (body.get("event") or "").strip()
    command = (body.get("command") or "").strip()
    if event not in _hk.ALLOWED_EVENTS:
        return 400, {"error": f"Invalid event. Allowed: {_hk.ALLOWED_EVENTS}"}
    if not command:
        return 400, {"error": "Command cannot be empty"}
    hooks = _hk.get_hooks()
    new_hook = {
        "id": uuid.uuid4().hex[:12],
        "event": event,
        "command": command,
        "enabled": True,
    }
    hooks.append(new_hook)
    _hk.save_hooks(hooks)
    return 200, new_hook


def post_hooks_update(body: dict, svc: HooksServices) -> tuple[int, JsonPayload]:
    """POST /api/hooks/update."""
    from .. import hooks as _hk
    hid = (body.get("id") or "").strip()
    if not hid:
        return 400, {"error": "missing hook id"}
    hooks = _hk.get_hooks()
    hook = next((h for h in hooks if h["id"] == hid), None)
    if not hook:
        return 404, {"error": "hook not found"}
    if "enabled" in body:
        hook["enabled"] = svc.parse_bool(body["enabled"])
    if "command" in body:
        cmd = (body["command"] or "").strip()
        if not cmd:
            return 400, {"error": "Command cannot be empty"}
        hook["command"] = cmd
    _hk.save_hooks(hooks)
    return 200, hook


def post_hooks_remove(body: dict) -> tuple[int, JsonPayload]:
    """POST /api/hooks/remove."""
    from .. import hooks as _hk
    hid = (body.get("id") or "").strip()
    if not hid:
        return 400, {"error": "missing hook id"}
    hooks = _hk.get_hooks()
    hooks = [h for h in hooks if h["id"] != hid]
    _hk.save_hooks(hooks)
    return 200, {"ok": True}
