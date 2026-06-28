"""Model visibility: which provider:model specs the user has enabled to appear
in the pilot picker. Mirrors the Cursor/Hermes "toggle models per provider"
UX -- the full catalog is large, so the user curates a short enabled set that
populates the dropdown.

Persisted to ~/.pmharness/models.json as {"enabled": ["provider:model", ...]}.
PM-free and pure-ish (stdlib only) so it unit-tests fast. The catalog of
selectable specs is derived from the provider profiles in providers.py, scoped
to providers whose key is actually present (no point offering models you cannot
call).
"""
from __future__ import annotations

import json
import os
import threading
from typing import Optional

_LOCK = threading.Lock()


def _store_path() -> str:
    base = os.path.join(os.path.expanduser("~"), ".pmharness")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "models.json")


def _load() -> dict:
    try:
        with open(_store_path()) as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _save(data: dict) -> None:
    tmp = _store_path() + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, _store_path())


def get_enabled() -> list:
    """The user's enabled provider:model specs (ordered). Empty list means
    'not yet curated' -- callers should fall back to the full available set."""
    with _LOCK:
        data = _load()
    enabled = data.get("enabled")
    if isinstance(enabled, list):
        return [str(x) for x in enabled if isinstance(x, str) and x.strip()]
    return []


def set_enabled(specs: list) -> list:
    """Replace the enabled set. Returns the normalized stored list."""
    norm = []
    seen = set()
    for s in specs or []:
        s = str(s).strip()
        if s and s not in seen:
            seen.add(s)
            norm.append(s)
    with _LOCK:
        data = _load()
        data["enabled"] = norm
        _save(data)
    return norm


def toggle(spec: str, on: bool) -> list:
    """Enable or disable a single spec; returns the new enabled list."""
    spec = (spec or "").strip()
    if not spec:
        return get_enabled()
    with _LOCK:
        data = _load()
        enabled = data.get("enabled")
        if not isinstance(enabled, list):
            enabled = []
        enabled = [str(x) for x in enabled if isinstance(x, str)]
        if on and spec not in enabled:
            enabled.append(spec)
        elif not on and spec in enabled:
            enabled = [x for x in enabled if x != spec]
        data["enabled"] = enabled
        _save(data)
    return enabled


def provider_models(p) -> list:
    """All selectable model ids for a provider: its LIVE catalog (fetched from
    the provider's own listing endpoint when a key is present) merged with the
    curated pilot_models fallback. Curated entries come first (they are the
    vetted pilot-capable picks), then any additional live models, de-duplicated.
    Falls back to curated-only when the provider has no key or the fetch fails."""
    curated = list(p.pilot_models)
    live = []
    try:
        key = p.key()
        if key:
            from .model_fetch import fetch_models
            live = fetch_models(p, key)
    except Exception:
        live = []
    seen = set()
    merged = []
    for m in curated + live:
        if m and m not in seen:
            seen.add(m)
            merged.append(m)
    return merged


def catalog(available_only: bool = True) -> list:
    """The selectable model catalog as a list of dicts:
        {provider, provider_display, model, spec, available, enabled}

    spec is the 'provider:model' string the picker uses. When available_only is
    True, only providers with a present key are included. Each available
    provider's models are its LIVE catalog merged with the curated fallback.
    """
    from . import providers as prov
    enabled = set(get_enabled())
    avail_names = {p.name for p in prov.available_providers()}
    out = []
    for p in prov.PROVIDERS:
        is_avail = p.name in avail_names
        if available_only and not is_avail:
            continue
        # Live-merged list for keyed providers; curated-only for unkeyed ones
        # (so a not-yet-keyed provider still shows its vetted picks).
        models = provider_models(p) if is_avail else list(p.pilot_models)
        for m in models:
            spec = f"{p.name}:{m}"
            out.append({
                "provider": p.name,
                "provider_display": p.display_name,
                "model": m,
                "spec": spec,
                "available": is_avail,
                "enabled": spec in enabled,
            })
    return out


def enabled_pilots() -> list:
    """The picker's model list: the user's enabled specs filtered to those whose
    provider currently has a key. If the user has not curated anything yet, fall
    back to the full available set (every model from every keyed provider, live
    catalog merged with the curated fallback)."""
    from . import providers as prov
    avail_specs = []
    for p in prov.available_providers():
        for m in provider_models(p):
            avail_specs.append(f"{p.name}:{m}")
    avail_set = set(avail_specs)
    enabled = [s for s in get_enabled() if s in avail_set]
    return enabled if enabled else avail_specs
