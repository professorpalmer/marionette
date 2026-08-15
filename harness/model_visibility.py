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
import re
import threading
from typing import Optional

_LOCK = threading.Lock()

# Dotted (glm-5.2, grok-4.6-fast) then hyphen (claude-opus-4-8). Prefix +
# suffix are the family; only a higher (major, minor) is a promotion.
_DOTTED_FAMILY = re.compile(
    r"^(?P<prefix>.*?)(?P<major>\d+)\.(?P<minor>\d+)(?P<suffix>.*)$"
)
_HYPHEN_FAMILY = re.compile(
    r"^(?P<prefix>.*?)(?P<major>\d+)-(?P<minor>\d+)(?P<suffix>.*)$"
)


def bare_model_id(model_id: str) -> str:
    """Strip a vendor namespace (``z-ai/glm-5.3`` → ``glm-5.3``)."""
    raw = str(model_id or "").strip()
    if "/" in raw:
        return raw.rsplit("/", 1)[-1]
    return raw


def parse_family_version(model_id: str):
    """Return ``(prefix, major, minor, suffix)`` or None.

    Matching is on the bare id so ``z-ai/glm-5.2`` and ``glm-5.2`` are the
    same family. ``kimi-k3`` and ``xiaomi/mimo-v2-flash`` do not parse.
    """
    bare = bare_model_id(model_id)
    if not bare:
        return None
    match = _DOTTED_FAMILY.match(bare) or _HYPHEN_FAMILY.match(bare)
    if not match:
        return None
    return (
        match.group("prefix").lower(),
        int(match.group("major")),
        int(match.group("minor")),
        match.group("suffix").lower(),
    )


def _emit_in_known_form(known_id: str, candidate_id: str) -> str:
    """Keep the provider's id shape: prefixed for OpenRouter, bare for vendors."""
    cand_bare = bare_model_id(candidate_id)
    known = str(known_id or "").strip()
    if "/" in known and "/" not in candidate_id:
        return known.rsplit("/", 1)[0] + "/" + cand_bare
    if "/" not in known and "/" in candidate_id:
        return cand_bare
    return str(candidate_id or "").strip() or cand_bare


def promote_newer_family_versions(known_ids, candidate_ids) -> list:
    """Live (or overlay) ids that are a newer X.Y of an already-known family.

    ``glm-5.2`` + live ``glm-5.3`` → ``glm-5.3``. Different families
    (``moonshotai/kimi-k3`` vs ``xiaomi/mimo-v2-flash``) never promote.
    """
    parsed_known = []
    known_set = set()
    for kid in known_ids or []:
        raw = str(kid or "").strip()
        if not raw:
            continue
        known_set.add(raw)
        known_set.add(bare_model_id(raw))
        parsed = parse_family_version(raw)
        if parsed:
            parsed_known.append((raw, parsed))
    if not parsed_known:
        return []
    out = []
    seen = set()
    for cid in candidate_ids or []:
        raw = str(cid or "").strip()
        if not raw:
            continue
        parsed = parse_family_version(raw)
        if not parsed:
            continue
        prefix, major, minor, suffix = parsed
        for known_id, (kprefix, kmajor, kminor, ksuffix) in parsed_known:
            if prefix != kprefix or suffix != ksuffix:
                continue
            if (major, minor) <= (kmajor, kminor):
                continue
            emitted = _emit_in_known_form(known_id, raw)
            if not emitted or emitted in seen or emitted in known_set:
                continue
            if bare_model_id(emitted) in known_set:
                continue
            seen.add(emitted)
            out.append(emitted)
            break
    return out


def inherit_family_spec(model_name: str, slug: str, specs: dict):
    """Newest known spec in the same family at or below this version."""
    target = parse_family_version(model_name) or parse_family_version(slug)
    if not target or not specs:
        return None
    prefix, major, minor, suffix = target
    best = None
    best_ver = None
    for key, spec in specs.items():
        parsed = parse_family_version(key)
        if not parsed:
            continue
        kprefix, kmajor, kminor, ksuffix = parsed
        if kprefix != prefix or ksuffix != suffix:
            continue
        if (kmajor, kminor) <= (major, minor) and (
            best_ver is None or (kmajor, kminor) > best_ver
        ):
            best_ver = (kmajor, kminor)
            best = spec
    return best


def _store_path() -> str:
    base = os.path.join(os.path.expanduser("~"), ".pmharness")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "models.json")


def _load() -> dict:
    try:
        with open(_store_path(), encoding="utf-8", errors="replace") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _save(data: dict) -> None:
    tmp = _store_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
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


def provider_models(p, *, force: bool = False) -> list:
    """Selectable model ids for a provider.

    Keyed providers are live-first: the vendor listing leads, then newer
    family versions discovered via live or an OpenRouter vendor overlay
    (``glm-5.2`` → ``glm-5.3``), then curated ids that listing has not
    published yet. OpenRouter stays live-only once keyed — an empty or
    failed fetch must not be disguised with the static list.
    """
    curated = list(p.pilot_models)
    live = []
    keyed = False
    try:
        key = p.key()
        if key:
            keyed = True
            from .model_fetch import fetch_models
            live = fetch_models(p, key, force=force)
    except Exception:
        live = []
    # OpenRouter is an authoritative remote catalog. Once keyed, an empty or
    # failed response must not be disguised with the curated static list.
    if p.name == "openrouter" and keyed:
        return list(dict.fromkeys(m for m in live if m))
    extras = []
    if keyed:
        try:
            from .model_fetch import vendor_ids_from_openrouter
            extras = vendor_ids_from_openrouter(p.name, force=force)
        except Exception:
            extras = []
    live_set = set(live)
    known = list(dict.fromkeys([m for m in list(curated) + list(live) if m]))
    promoted = promote_newer_family_versions(
        known, list(dict.fromkeys([m for m in list(live) + list(extras) if m])),
    )
    if live:
        ordered = (
            list(live)
            + [m for m in promoted if m not in live_set]
            + [m for m in curated if m not in live_set and m not in set(promoted)]
        )
    else:
        ordered = list(curated)
    seen = set()
    merged = []
    for m in ordered:
        if m and m not in seen:
            seen.add(m)
            merged.append(m)
    return merged


def catalog(available_only: bool = True, *, force: bool = False) -> list:
    """The selectable model catalog as a list of dicts:
        {provider, provider_display, model, spec, available, enabled}

    spec is the 'provider:model' string the picker uses. When available_only is
    True, only providers with a present key are included. Keyed providers are
    live-first with curated backfill; keyed OpenRouter uses live data only.
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
        models = provider_models(p, force=force) if is_avail else list(p.pilot_models)
        from .model_fetch import fetch_status, model_metadata
        status = fetch_status(p.name) if is_avail else {
            "source": "curated", "status": "unavailable", "error": None,
        }
        for m in models:
            spec = f"{p.name}:{m}"
            metadata = model_metadata(p.name, m) if p.name == "openrouter" else None
            pricing = (metadata or {}).get("pricing") or {}
            out.append({
                "provider": p.name,
                "provider_display": p.display_name,
                "model": m,
                "spec": spec,
                "available": is_avail,
                "enabled": spec in enabled,
                "context_window": (metadata or {}).get("context_length"),
                "pricing": pricing,
                "price_in": pricing.get("prompt"),
                "price_out": pricing.get("completion"),
                "source": (metadata or {}).get("source", status.get("source")),
                "status": status.get("status"),
                "provider_metadata": metadata or {},
                "error": status.get("error"),
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
