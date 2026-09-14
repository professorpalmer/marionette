from __future__ import annotations

"""Models-enabled allowlist for product swarms.

Product workers use the agentic adapter. Provider credentials and model names
remain choices inside that adapter; they are not alternate worker adapters.

Rules:
  * Canonicalize allowlisted IDs against the live registry.
  * Fail closed when no exact enabled/live row exists.
"""

from typing import Any, Optional

from .diag import note as _diag

# Adapters Marionette can actually drive for product analysis swarms.
_PRODUCT_WORKER_ADAPTERS = ("agentic",)


def _enabled_or_visible_specs() -> list[str]:
    """Models toggles are the worker allowlist when any row is curated.

    ``enabled_pilots()`` is only the empty-curation fallback. Unioning it
    while Settings has a subset leaks every keyed vendor model (glm-5.2
    winning implement auto-route while toggled off). Cursor-cli Grok still
    counts as intent because it lives on the curated list, even when
    ``enabled_pilots()`` drops it for missing Cursor Agent login.
    """
    try:
        from . import model_visibility as _mv

        curated = [
            str(spec or "").strip()
            for spec in (_mv.get_enabled() or [])
            if str(spec or "").strip()
        ]
        # Empty is meaningful when the user explicitly saved an empty
        # selection. Only an absent setting gets the legacy available-model
        # fallback.
        try:
            stored = _mv._load()
            if isinstance(stored, dict) and "enabled" in stored:
                return curated
        except Exception:
            pass
        if curated:
            # Models toggles are the only discretionary allowlist. Do not
            # union enabled_pilots() here — that function falls back to every
            # keyed vendor model when a curated spec misses avail_set, which
            # is how a toggled-off glm-5.2 won implement auto-route.
            return curated
        pilots = list(_mv.enabled_pilots() or [])
        if not pilots:
            return []
        out: list[str] = []
        seen: set[str] = set()
        for spec in pilots:
            s = str(spec or "").strip()
            if not s or s in seen:
                continue
            seen.add(s)
            out.append(s)
        return out
    except Exception as e:
        _diag("swarm_worker_allowlist.enabled", e)
        return []


def _provider_of_spec(spec: str) -> str:
    raw = (spec or "").strip()
    if not raw:
        return ""
    if ":" in raw:
        return raw.split(":", 1)[0].strip().lower()
    if "/" in raw:
        return raw.split("/", 1)[0].strip().lower()
    return ""


def _model_of_spec(spec: str) -> str:
    raw = (spec or "").strip()
    if not raw:
        return ""
    if ":" in raw:
        return raw.split(":", 1)[1].strip().lower()
    if "/" in raw:
        return raw.split("/", 1)[1].strip().lower()
    return raw.lower()


def _looks_like_cursor_worker_model(provider: str, model: str) -> bool:
    """True when a Models row signals Cursor-plan worker intent (Grok/Composer)."""
    p = (provider or "").strip().lower()
    m = (model or "").strip().lower()
    if p in ("cursor", "cursor-cli", "cursor-sdk"):
        return True
    if m.startswith("cursor-") or m.startswith("cursor/"):
        return True
    if p in ("cursor", "cursor-cli") or "cursor" in p:
        return True
    # Settings labels often keep grok/composer under cursor-cli prefixes.
    if any(token in m for token in ("grok", "composer")) and (
        p.startswith("cursor") or m.startswith("cursor")
    ):
        return True
    return False


def _platform_locked_adapters() -> Optional[frozenset[str]]:
    """``None`` = unrestricted; else enabled adapter names from platform.json."""
    try:
        from puppetmaster.platform_lock import active_allowlist

        return active_allowlist()
    except Exception as e:
        _diag("swarm_worker_allowlist.platform_lock", e)
        return None


def _agentic_eligible() -> bool:
    try:
        from .auto_registry import keyed_agentic_providers

        return bool(keyed_agentic_providers())
    except Exception as e:
        _diag("swarm_worker_allowlist.keyed", e)
        return False


def _cursor_platform_ready() -> bool:
    try:
        from .provider_capabilities import cursor_platform_workers_ready

        return bool(cursor_platform_workers_ready())
    except Exception as e:
        _diag("swarm_worker_allowlist.cursor_ready", e)
        return False


def adapters_from_visibility(specs: Optional[list[str]] = None) -> set[str]:
    """Map Models-enabled / catalog-visible specs onto product worker adapters."""
    from .provider_capabilities import worker_capability

    rows = list(specs) if specs is not None else _enabled_or_visible_specs()
    out: set[str] = set()
    for spec in rows:
        provider = _provider_of_spec(spec)
        model = _model_of_spec(spec)
        if not provider and not model:
            continue
        cap = worker_capability(provider) if provider else "pilot_only"
        if cap == "full_stack":
            # Full-stack HTTP providers sync into the agentic worker catalog.
            # Native openai adapter is only offered when the provider is openai
            # AND agentic is unavailable; otherwise keep agentic as the wire.
            out.add("agentic")
            if provider in ("openai",) and not _agentic_eligible():
                out.add("openai")
        elif cap == "platform_worker" and provider == "cursor":
            out.add("cursor")
        elif _looks_like_cursor_worker_model(provider, model):
            # cursor-cli Grok/Composer toggles are pilot-auth, but they express
            # intent that platform Cursor workers should be allowed when ready.
            out.add("cursor")
    return out


def allowed_model_ids_from_specs(
    specs: Optional[list[str]] = None,
) -> list[str]:
    """Return only exact IDs present in the live agentic registry.

    Provider and model strings are opaque. Matching is deliberately against
    registry payload fields, not aliases or sibling provider IDs.
    """
    from .swarm_model_pin import _usable_registry_rows
    rows = list(specs) if specs is not None else _enabled_or_visible_specs()
    # An explicit specs argument is the caller's Settings snapshot and must
    # override the process-global snapshot used by normal dispatch/tests.
    registry = _usable_registry_rows(adapters={"agentic"}, enabled_specs=rows)
    by_pair: dict[tuple[str, str], str] = {}
    for row in registry:
        defaults = row.get("payload_defaults")
        if not isinstance(defaults, dict):
            continue
        provider = str(defaults.get("provider") or "").strip().lower()
        model = str(row.get("adapter_model_name") or "").strip()
        if not model:
            # Legacy rows used payload_defaults.model before the adapter field
            # became mandatory; retain compatibility only for that same row.
            model = str(defaults.get("model") or "").strip()
        model_id = str(row.get("id") or "").strip()
        if provider and model and model_id:
            by_pair.setdefault((provider, model.lower()), model_id)
    out: list[str] = []
    seen: set[str] = set()
    for spec in rows:
        provider = _provider_of_spec(spec)
        model = _model_of_spec(spec)
        hit = None
        for alias in [model.strip().lower(), *sorted(_flash_lookup_keys(model))]:
            hit = by_pair.get((provider, alias))
            if hit:
                break
        if hit and hit.lower() not in seen:
            seen.add(hit.lower())
            out.append(hit)
    return out


def _flash_lookup_keys(model: str) -> frozenset:
    try:
        from .opencode_go import flash_model_keys
        return flash_model_keys(model)
    except Exception:
        n = (model or "").strip().lower()
        return frozenset({n} if n else ())


def native_codex_available() -> bool:
    """Require the native CLI and a readable, permitting platform policy."""
    import shutil
    try:
        from puppetmaster.platform_lock import is_adapter_enabled
        return bool(is_adapter_enabled("codex") and shutil.which("codex"))
    except Exception as exc:
        _diag("swarm_worker_allowlist.codex", exc)
        return False


def resolve_swarm_worker_allowlist(
    *,
    specs: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Compute payload ``allowed_adapters`` + model-id allowlist for swarms.

    Returns:
      {
        "allowed_adapters": list[str],  # ordered, non-empty when any eligible
        "prefer_plan_billed": bool,
        "primary_adapter": str,         # WorkerSpec.adapter default
        "visibility_adapters": list[str],
        "platform_lock": list[str] | None,
        "allowed_model_ids": list[str], # always explicit; empty fails closed
      }
    """
    allowed_ids = allowed_model_ids_from_specs(specs)
    visibility = {"agentic"} if (_agentic_eligible() or allowed_ids) else set()
    allowed = {"agentic"} if visibility else set()
    lock = _platform_locked_adapters()
    if lock is not None and "agentic" not in lock:
        allowed = set()
    ordered = ["agentic"] if allowed else []

    return {
        "allowed_adapters": ordered,
        "prefer_plan_billed": False,
        "primary_adapter": "agentic",
        "visibility_adapters": sorted(visibility),
        "platform_lock": sorted(lock) if lock is not None else None,
        # Always present, including empty, so an empty eligible set fails
        # closed instead of removing the router constraint.
        "allowed_model_ids": allowed_ids,
    }


def _cursor_intent_from_specs(specs: Optional[list[str]]) -> bool:
    rows = list(specs) if specs is not None else _enabled_or_visible_specs()
    for spec in rows:
        if _looks_like_cursor_worker_model(
            _provider_of_spec(spec), _model_of_spec(spec),
        ):
            return True
    return False
