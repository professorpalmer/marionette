from __future__ import annotations

"""Resolve run_swarm model pins against the live worker adapter union.

Pilots often pass their session model (``cursor/gpt-5-6-luna``,
``openai-codex:gpt-5.6-luna``, ``cursor/grok-4-5``). Those aliases remap to
keyed agentic rows when a matching worker auth exists, or to platform cursor
rows when Settings/platform allow Cursor workers. Unresolved pins demote to
auto-route across the live union catalog instead of failing.
"""

import re
from typing import Any, Optional

from .diag import note as _diag

# Prefer agentic (API-billed) remaps before platform cursor when both match.
_PIN_ADAPTER_ORDER = ("agentic", "cursor", "openai")


def _registry_rows(*, adapters: Optional[set[str]] = None) -> list[dict]:
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
        allow = adapters
        out: list[dict] = []
        for row in models:
            if not isinstance(row, dict):
                continue
            adapter = str(row.get("adapter") or "").strip().lower()
            if allow is not None and adapter not in allow:
                continue
            out.append(row)
        return out
    except Exception as e:
        _diag("swarm_model_pin.registry_rows", e)
        return []


def _agentic_registry_rows() -> list[dict]:
    return _registry_rows(adapters={"agentic"})


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


def list_available_worker_models(
    *,
    limit: int = 24,
    adapters: Optional[set[str]] = None,
) -> list[str]:
    """Registry ids across the allowed worker adapter union."""
    allow = set(adapters) if adapters is not None else None
    try:
        from .auto_registry import keyed_agentic_providers

        keyed = keyed_agentic_providers()
    except Exception as e:
        _diag("swarm_model_pin.keyed_union", e)
        keyed = set()
    out: list[str] = []
    seen: set[str] = set()
    for row in _registry_rows(adapters=allow):
        mid = str(row.get("id") or "").strip()
        if not mid or mid in seen:
            continue
        adapter = str(row.get("adapter") or "").strip().lower()
        if adapter == "agentic":
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
    try:
        from .swarm_worker_allowlist import resolve_swarm_worker_allowlist

        allow = set(resolve_swarm_worker_allowlist().get("allowed_adapters") or [])
    except Exception:
        allow = {"agentic"}
    available = list_available_worker_models(limit=limit, adapters=allow or None)
    if not available:
        return (
            "Omit model for auto-route among currently keyed worker adapters "
            "(OpenCode Go, OpenRouter, ChatGPT Codex OAuth, Cursor API, …). "
            "Session pilot ids are remapped when a matching worker row exists; "
            "otherwise the harness auto-routes."
        )
    shown = ", ".join(available)
    return (
        "Omit model for auto-route (preferred). Live worker catalog: "
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
            "cursor-cli",
            "codex",
            "openai",
            "openai-codex",
            "agentic",
            "native",
            "opencode-go",
            "opencode-zen",
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
        # Platform cursor registry peers (only useful when bridge allows cursor).
        _add(f"cursor/{body}")
    return out


def _try_apply_pin(candidate: str, *, adapter: str) -> Optional[dict]:
    try:
        from puppetmaster.model_registry import (
            AmbiguousModelPinError,
            apply_model_pin,
        )
    except Exception as e:
        _diag("swarm_model_pin.apply_import", e)
        return None
    try:
        stamped = apply_model_pin({}, candidate, adapter=adapter)
    except AmbiguousModelPinError as exc:
        _diag("swarm_model_pin.ambiguous", msg=f"pin={candidate!r} err={exc}")
        return None
    except Exception as e:
        _diag("swarm_model_pin.apply", e, msg=f"pin={candidate!r} adapter={adapter}")
        return None
    if not isinstance(stamped, dict) or not stamped.get("pinned_model"):
        return None
    stamped = dict(stamped)
    stamped["pinned_adapter"] = adapter
    return stamped


def _allowed_pin_adapters(
    allowed_adapters: Optional[list[str] | set[str] | tuple[str, ...]],
) -> list[str]:
    if allowed_adapters:
        ordered = [a for a in _PIN_ADAPTER_ORDER if a in set(allowed_adapters)]
        for a in allowed_adapters:
            name = str(a or "").strip().lower()
            if name and name not in ordered:
                ordered.append(name)
        return ordered or ["agentic"]
    try:
        from .swarm_worker_allowlist import resolve_swarm_worker_allowlist

        return list(
            resolve_swarm_worker_allowlist().get("allowed_adapters") or ["agentic"]
        )
    except Exception:
        return ["agentic"]


def resolve_swarm_model_pin(
    pin: str,
    *,
    allowed_adapters: Optional[list[str] | set[str] | tuple[str, ...]] = None,
) -> dict[str, Any]:
    """Resolve a swarm model pin or demote to auto-route.

    Tries each adapter in the Settings/platform union (agentic first, then
    cursor, then openai) so a Cursor Grok pin is not rejected just because the
    agentic catalog lacks that id.

    Returns:
      {
        "pin_fields": dict,   # merged into worker payload when pinned
        "auto_route": bool,
        "requested": str,
        "resolved": str,      # empty when demoted
        "demoted": bool,
        "reason": str,
        "adapter": str,       # adapter that accepted the pin (or "")
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
        "adapter": "",
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

    adapters = _allowed_pin_adapters(allowed_adapters)
    for candidate in pin_candidates(requested):
        for adapter in adapters:
            stamped = _try_apply_pin(candidate, adapter=adapter)
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
                "adapter": adapter,
            }

    available = list_available_worker_models(limit=8, adapters=set(adapters))
    reason = (
        f"pin {requested!r} not in keyed worker registry "
        f"(adapters={adapters}); "
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
        "adapter": "",
    }
