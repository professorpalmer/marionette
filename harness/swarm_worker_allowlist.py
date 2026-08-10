from __future__ import annotations

"""Settings + platform driven worker adapter allowlist for product swarms.

Product bar: any Models-enabled / catalog-visible worker-capable model must be
reachable as a swarm worker. The bridge must not hardcode ``allowed_adapters=
['agentic']`` when Settings also enables Cursor Grok/Composer (or other
platform-locked adapters).

Rules:
  * Union adapters for Models-enabled (or full catalog-visible when unset)
    worker-capable providers.
  * Intersect with Puppetmaster platform lock when restricted.
  * Prefer ``prefer_plan_billed=False`` whenever any API-billed agentic model
    is eligible so OpenRouter cash models are not starved by $0 plan picks.
  * Do not exclude Cursor when Settings enables Grok/Composer and platform
    cursor workers are ready + unlocked.
"""

from typing import Any, Optional

from .diag import note as _diag

# Adapters Marionette can actually drive for product analysis swarms.
_PRODUCT_WORKER_ADAPTERS = ("agentic", "cursor", "openai")


def _enabled_or_visible_specs() -> list[str]:
    """Models toggles (user intent), unioned with keyed pilot availability.

    ``enabled_pilots()`` only keeps specs whose *pilot* provider is keyed right
    now. That drops ``cursor-cli:cursor-grok-*`` / ``composer-*`` when the user
    has no Cursor Agent login, even though ``CURSOR_API_KEY`` platform workers
    are ready and Settings still shows those models enabled. Worker allowlist
    must honor the curated Models toggles as adapter *intent* — otherwise Luna
    Max cannot orchestrate Grok workers despite both being on in Settings.
    """
    try:
        from . import model_visibility as _mv

        curated = list(_mv.get_enabled() or [])
        pilots = list(_mv.enabled_pilots() or [])
        if not curated and not pilots:
            return []
        # Curated first (user intent), then keyed pilots for any extras.
        out: list[str] = []
        seen: set[str] = set()
        for spec in curated + pilots:
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


def resolve_swarm_worker_allowlist(
    *,
    specs: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Compute payload ``allowed_adapters`` + ``prefer_plan_billed`` for swarms.

    Returns:
      {
        "allowed_adapters": list[str],  # ordered, non-empty when any eligible
        "prefer_plan_billed": bool,
        "primary_adapter": str,         # WorkerSpec.adapter default
        "visibility_adapters": list[str],
        "platform_lock": list[str] | None,
      }
    """
    visibility = adapters_from_visibility(specs)

    # Live credentials also enlarge the union: a keyed agentic provider must
    # stay eligible even when Models curation is empty/stale, and CURSOR_API_KEY
    # keeps platform cursor reachable when Settings enabled Grok/Composer OR
    # when visibility already selected cursor.
    if _agentic_eligible():
        visibility.add("agentic")
    cursor_ready = _cursor_platform_ready()
    if cursor_ready and ("cursor" in visibility or _cursor_intent_from_specs(specs)):
        visibility.add("cursor")

    # Fallback when visibility yielded nothing but credentials exist.
    if not visibility:
        if _agentic_eligible():
            visibility.add("agentic")
        elif cursor_ready:
            visibility.add("cursor")

    lock = _platform_locked_adapters()
    if lock is None:
        allowed = {a for a in visibility if a in _PRODUCT_WORKER_ADAPTERS}
    else:
        allowed = {
            a for a in visibility
            if a in _PRODUCT_WORKER_ADAPTERS and a in lock
        }
        # Platform may still allow openai/codex under alternate names.
        if "codex" in lock and "openai" in visibility:
            allowed.add("openai")

    # Never return an empty allowlist when we know a real adapter is ready —
    # fail open to agentic (or cursor-only fallback) so routing can still run.
    if not allowed:
        if _agentic_eligible() and (lock is None or "agentic" in lock):
            allowed = {"agentic"}
        elif cursor_ready and (lock is None or "cursor" in lock):
            allowed = {"cursor"}
        elif lock:
            allowed = {a for a in _PRODUCT_WORKER_ADAPTERS if a in lock}
        else:
            allowed = {"agentic"}

    ordered = [a for a in _PRODUCT_WORKER_ADAPTERS if a in allowed]
    for a in sorted(allowed):
        if a not in ordered:
            ordered.append(a)

    has_agentic = "agentic" in allowed
    # API-billed agentic eligible → never prefer $0 plan-billed Cursor first.
    prefer_plan = False if has_agentic else ("cursor" in allowed)

    if has_agentic:
        primary = "agentic"
    elif "cursor" in allowed:
        primary = "cursor"
    elif "openai" in allowed:
        primary = "openai"
    else:
        primary = ordered[0]

    return {
        "allowed_adapters": ordered,
        "prefer_plan_billed": prefer_plan,
        "primary_adapter": primary,
        "visibility_adapters": sorted(visibility),
        "platform_lock": sorted(lock) if lock is not None else None,
    }


def _cursor_intent_from_specs(specs: Optional[list[str]]) -> bool:
    rows = list(specs) if specs is not None else _enabled_or_visible_specs()
    for spec in rows:
        if _looks_like_cursor_worker_model(
            _provider_of_spec(spec), _model_of_spec(spec),
        ):
            return True
    return False
