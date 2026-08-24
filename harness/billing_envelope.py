"""Monthly billing envelope: cap + optional auto-reload intent.

Persists under HARNESS_STATE_DIR (or ~/.pmharness). Does not charge a card.
Spend figures are whatever the caller already computed — no second cost math.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from .secure_files import restrict_to_owner

_FILENAME = "billing_envelope.json"
_lock = threading.RLock()


def _state_dir() -> Path:
    explicit = (os.environ.get("HARNESS_STATE_DIR") or "").strip()
    if explicit:
        return Path(explicit)
    return Path(os.path.expanduser("~/.pmharness"))


def envelope_path() -> Path:
    return _state_dir() / _FILENAME


def month_key(now: Optional[datetime] = None) -> str:
    stamp = now or datetime.now(timezone.utc)
    return stamp.strftime("%Y-%m")


def _empty(month: str) -> Dict[str, Any]:
    return {
        "month": month,
        "spent_usd": 0.0,
        "cap_usd": None,
        "auto_reload": {"enabled": False, "amount_usd": 0.0},
        "last_reload": None,
    }


def load_envelope() -> Dict[str, Any]:
    path = envelope_path()
    month = month_key()
    if not path.is_file():
        return _empty(month)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return _empty(month)
    if not isinstance(data, dict):
        return _empty(month)
    stored_month = str(data.get("month") or "")
    if stored_month != month:
        data = _empty(month)
    else:
        try:
            data["spent_usd"] = float(data.get("spent_usd") or 0.0)
        except (TypeError, ValueError):
            data["spent_usd"] = 0.0
        cap = data.get("cap_usd")
        if cap is not None:
            try:
                data["cap_usd"] = float(cap)
            except (TypeError, ValueError):
                data["cap_usd"] = None
        reload_cfg = data.get("auto_reload")
        if not isinstance(reload_cfg, dict):
            data["auto_reload"] = {"enabled": False, "amount_usd": 0.0}
        else:
            try:
                amount = float(reload_cfg.get("amount_usd") or 0.0)
            except (TypeError, ValueError):
                amount = 0.0
            data["auto_reload"] = {
                "enabled": bool(reload_cfg.get("enabled")),
                "amount_usd": amount,
            }
    return data


def save_envelope(data: Dict[str, Any]) -> Dict[str, Any]:
    path = envelope_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(data)
    payload["month"] = str(payload.get("month") or month_key())
    with _lock:
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        restrict_to_owner(str(path))
    return payload


def set_envelope(*, cap_usd: Any = None, auto_reload_enabled: Any = None, auto_reload_amount: Any = None) -> Dict[str, Any]:
    with _lock:
        data = load_envelope()
        if cap_usd is not None:
            if cap_usd == "" or cap_usd is False:
                data["cap_usd"] = None
            else:
                data["cap_usd"] = float(cap_usd)
        reload_cfg = dict(data.get("auto_reload") or {})
        if auto_reload_enabled is not None:
            reload_cfg["enabled"] = bool(auto_reload_enabled)
        if auto_reload_amount is not None:
            reload_cfg["amount_usd"] = float(auto_reload_amount)
        data["auto_reload"] = {
            "enabled": bool(reload_cfg.get("enabled")),
            "amount_usd": float(reload_cfg.get("amount_usd") or 0.0),
        }
        if data["auto_reload"]["enabled"] and data["auto_reload"]["amount_usd"] > 0:
            data["last_reload"] = {"intent": True, "amount_usd": data["auto_reload"]["amount_usd"]}
        return save_envelope(data)


def sync_spent(spent_usd: float) -> Dict[str, Any]:
    """Record caller-computed spend for the current month. No new cost math."""
    with _lock:
        data = load_envelope()
        try:
            data["spent_usd"] = max(0.0, float(spent_usd))
        except (TypeError, ValueError):
            data["spent_usd"] = 0.0
        return save_envelope(data)


def envelope_public(data: Optional[Dict[str, Any]] = None, *, spent_usd: Optional[float] = None) -> Dict[str, Any]:
    env = dict(data or load_envelope())
    if spent_usd is not None:
        try:
            env["spent_usd"] = max(0.0, float(spent_usd))
        except (TypeError, ValueError):
            pass
    cap = env.get("cap_usd")
    spent = float(env.get("spent_usd") or 0.0)
    remaining = None
    blocked = False
    if cap is not None:
        remaining = round(float(cap) - spent, 6)
        blocked = spent >= float(cap)
    reload_cfg = env.get("auto_reload") if isinstance(env.get("auto_reload"), dict) else {}
    return {
        "month": env.get("month") or month_key(),
        "spent_usd": round(spent, 6),
        "cap_usd": None if cap is None else float(cap),
        "remaining_usd": remaining,
        "blocked": blocked,
        "auto_reload": {
            "enabled": bool(reload_cfg.get("enabled")),
            "amount_usd": float(reload_cfg.get("amount_usd") or 0.0),
        },
        "last_reload": env.get("last_reload"),
    }



def snapshot(data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    public = envelope_public(data)
    return {
        "month_key": public["month"],
        "spent_usd": public["spent_usd"],
        "cap": public["cap_usd"],
        "remaining": public["remaining_usd"],
        "blocked": public["blocked"],
        "auto_reload": {
            "enabled": public["auto_reload"]["enabled"],
            "amount": public["auto_reload"]["amount_usd"],
        },
        "last_reload": public["last_reload"],
    }


def observe_spend(spent_usd: float) -> Dict[str, Any]:
    return snapshot(sync_spent(spent_usd))


def apply_settings(body: Dict[str, Any]) -> Dict[str, Any]:
    body = body or {}
    cap = body.get("cap_usd", body.get("cap"))
    enabled = body.get("auto_reload_enabled")
    if enabled is None:
        raw = body.get("auto_reload")
        if isinstance(raw, dict):
            enabled = raw.get("enabled")
            if body.get("auto_reload_amount_usd") is None and body.get("amount_usd") is None:
                body = dict(body)
                body["auto_reload_amount_usd"] = raw.get("amount_usd", raw.get("amount"))
        elif raw is not None:
            enabled = raw
    amount = body.get("auto_reload_amount_usd", body.get("amount_usd"))
    env = set_envelope(cap_usd=cap, auto_reload_enabled=enabled, auto_reload_amount=amount)
    return snapshot(env)
