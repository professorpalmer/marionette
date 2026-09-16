"""User-defined forbidden file patterns."""
from __future__ import annotations

from typing import Any

from ..privacy_paths import (
    load_forbidden_patterns,
    normalize_pattern,
    save_forbidden_patterns,
)


def _state_dir(svc: Any) -> str:
    getter = getattr(svc, "state_dir", None)
    if callable(getter):
        return str(getter() or "")
    return str(getattr(svc, "state_dir_value", "") or "")


def get_privacy(_qs: Any, svc: Any) -> tuple[int, dict]:
    patterns = load_forbidden_patterns(_state_dir(svc))
    return 200, {"ok": True, "forbidden_patterns": patterns}


def post_privacy(body: dict, svc: Any) -> tuple[int, dict]:
    if not isinstance(body, dict):
        return 400, {"ok": False, "error": "invalid body"}
    state_dir = _state_dir(svc)
    current = load_forbidden_patterns(state_dir)
    action = str(body.get("action") or "set").strip().lower()
    try:
        if action == "add":
            current.append(normalize_pattern(body.get("pattern")))
            patterns = save_forbidden_patterns(current, state_dir)
        elif action == "remove":
            victim = normalize_pattern(body.get("pattern"))
            patterns = save_forbidden_patterns(
                [item for item in current if item != victim], state_dir
            )
        elif action == "set":
            raw = body.get("patterns")
            if not isinstance(raw, list):
                return 400, {"ok": False, "error": "patterns must be a list"}
            patterns = save_forbidden_patterns(raw, state_dir)
        else:
            return 400, {"ok": False, "error": "action must be add, remove, or set"}
    except ValueError as exc:
        return 400, {"ok": False, "error": str(exc)}
    refresh = getattr(svc, "refresh_workers", None)
    if callable(refresh):
        refresh(patterns)
    return 200, {"ok": True, "forbidden_patterns": patterns}
