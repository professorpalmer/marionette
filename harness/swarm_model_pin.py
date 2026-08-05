from __future__ import annotations

"""Resolve run_swarm model pins against the *currently keyed* agentic catalog.

Pilots often pass their session model (``cursor/gpt-5-6-luna``,
``openai-codex:gpt-5.6-luna``). Those aliases remap to keyed agentic rows when
the same model exists for an auth that can drive workers (e.g. Codex OAuth →
``agentic/openai-codex/gpt-5.6-luna`` with ``provider=openai-codex``). Unresolved
pins demote to auto-route across the live union catalog instead of failing.
"""

import re
from typing import Any, Optional

from .diag import note as _diag


def _agentic_registry_rows() -> list[dict]:
    try:
        from .registry_wizard import get_models_file_path
        import json
        import os

        path = get_models_file_path()
        if not path or not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            data = json.load(f)
        models = data.get("models") if isinstance(data, dict) else None
        if not isinstance(models, list):
            return []
        return [
            row
            for row in models
            if isinstance(row, dict) and row.get("adapter") == "agentic"
        ]
    except Exception as e:
        _diag("swarm_model_pin.registry_rows", e)
        return []


def list_available_agentic_worker_models(*, limit: int = 24) -> list[str]:
    """Registry ids the agentic swarm router can actually pick right now."""
    try:
        from .auto_registry import keyed_agentic_providers

        keyed = keyed_agentic_providers()
    except Exception as e:
        _diag("swarm_model_pin.keyed", e)
        keyed = set()
    out: list[str] = []
    seen: set[str] = set()
    for row in _agentic_registry_rows():
        mid = str(row.get("id") or "").strip()
        if not mid or mid in seen:
            continue
        provider = ""
        defaults = row.get("payload_defaults")
        if isinstance(defaults, dict):
            provider = str(defaults.get("provider") or "").strip()
        if keyed and provider and provider not in keyed:
            continue
        seen.add(mid)
        out.append(mid)
        if len(out) >= max(1, int(limit)):
            break
    return out


def swarm_model_pin_hint(*, limit: int = 16) -> str:
    """Short tool-schema suffix listing live worker models (or auto-route)."""
    available = list_available_agentic_worker_models(limit=limit)
    if not available:
        return (
            "Omit model for auto-route among currently keyed agentic providers "
            "(OpenCode Go, OpenRouter, ChatGPT Codex OAuth, …). Session pilot "
            "ids are remapped when a matching worker row exists; otherwise the "
            "harness auto-routes."
        )
    shown = ", ".join(available)
    return (
        "Omit model for auto-route (preferred). Live agentic worker catalog: "
        f"{shown}. Session pilot ids (openai-codex:…, cursor/…, codex/…) remap "
        "to a matching row when present; unknown pins demote to auto-route."
    )


def pin_candidates(pin: str) -> list[str]:
    """Ordered alias candidates for a pilot-supplied swarm model pin."""
    raw = (pin or "").strip()
    if not raw:
        return []
    out: list[str] = []
    seen: set[str] = set()

    def _add(value: str) -> None:
        v = (value or "").strip()
        if not v:
            return
        key = v.lower()
        if key in seen:
            return
        seen.add(key)
        out.append(v)

    _add(raw)

    # provider:model / engine/model → bare model
    bare = raw
    if ":" in bare:
        bare = bare.split(":", 1)[1].strip() or bare
    _add(bare)
    if "/" in bare:
        head, rest = bare.split("/", 1)
        if head.lower() in {
            "cursor",
            "codex",
            "openai",
            "openai-codex",
            "agentic",
            "native",
            "opencode-go",
        }:
            bare = rest.strip() or bare
            _add(bare)

    # Cursor registry uses hyphens in gpt-5-6-*; OpenCode Go uses dots (gpt-5.6-*).
    dotted = re.sub(r"(gpt-\d+)-(\d+)", r"\1.\2", bare, count=1, flags=re.I)
    _add(dotted)
    hyphenated = re.sub(r"(gpt-\d+)\.(\d+)", r"\1-\2", bare, count=1, flags=re.I)
    _add(hyphenated)

    for body in (bare, dotted, hyphenated):
        if not body:
            continue
        _add(f"agentic/{body}")
        # Codex curated rows use a namespaced registry id so they do not
        # collide with OpenCode Go's flat agentic/gpt-5.6-* rows.
        _add(f"openai-codex/{body}")
        _add(f"agentic/openai-codex/{body}")
    return out


def _try_apply_pin(candidate: str) -> Optional[dict]:
    try:
        from puppetmaster.model_registry import (
            AmbiguousModelPinError,
            apply_agentic_model_pin,
        )
    except Exception as e:
        _diag("swarm_model_pin.apply_import", e)
        return None
    try:
        stamped = apply_agentic_model_pin({}, candidate)
    except AmbiguousModelPinError as exc:
        _diag("swarm_model_pin.ambiguous", msg=f"pin={candidate!r} err={exc}")
        return None
    except Exception as e:
        _diag("swarm_model_pin.apply", e, msg=f"pin={candidate!r}")
        return None
    if not isinstance(stamped, dict) or not stamped.get("pinned_model"):
        return None
    return stamped


def resolve_swarm_model_pin(pin: str) -> dict[str, Any]:
    """Resolve a swarm model pin or demote to auto-route.

    Returns:
      {
        "pin_fields": dict,   # merged into worker payload when pinned
        "auto_route": bool,
        "requested": str,
        "resolved": str,      # empty when demoted
        "demoted": bool,
        "reason": str,
      }
    """
    requested = (pin or "").strip()
    empty = {
        "pin_fields": {},
        "auto_route": True,
        "requested": requested,
        "resolved": "",
        "demoted": False,
        "reason": "empty",
    }
    if not requested:
        return empty

    # Refresh catalog against live keys so alias resolution sees OpenCode Go /
    # OpenRouter / etc. as they exist *now*, not a stale peer machine catalog.
    try:
        from .auto_registry import ensure_keyed_provider_registry_health

        ensure_keyed_provider_registry_health()
    except Exception as e:
        _diag("swarm_model_pin.health", e)

    for candidate in pin_candidates(requested):
        stamped = _try_apply_pin(candidate)
        if not stamped:
            continue
        resolved = str(stamped.get("pinned_model") or "").strip()
        return {
            "pin_fields": {**stamped, "auto_route": False},
            "auto_route": False,
            "requested": requested,
            "resolved": resolved,
            "demoted": False,
            "reason": (
                "exact"
                if candidate == requested
                else f"alias:{candidate}"
            ),
        }

    available = list_available_agentic_worker_models(limit=8)
    reason = (
        f"pin {requested!r} not in keyed agentic registry; "
        f"auto-routing among {available or ['(none keyed)']}"
    )
    _diag("swarm_model_pin.demote", msg=reason)
    return {
        "pin_fields": {},
        "auto_route": True,
        "requested": requested,
        "resolved": "",
        "demoted": True,
        "reason": reason,
    }
