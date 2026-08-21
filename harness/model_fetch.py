"""Live per-provider model discovery.

The curated ``pilot_models`` tuples in providers.py are a hardcoded fallback that
drifts (Anthropic ships 9 models but the list shows 3; OpenAI ships gpt-5.6
Sol/Terra/Luna while static fallbacks may still list 5.5/5.4). This module
fetches each KEYED provider's REAL model catalog from its own listing endpoint
and caches it on disk with a TTL. OpenRouter records remain rich because its
live catalog is authoritative; other providers can still use curated fallback
behavior in their callers.

Stdlib-only (urllib, json). Every fetch degrades gracefully: any network/auth/parse
failure falls back to the cached list, then returns an empty live result. Never raises.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from typing import Any, Optional

from .diag import note as _diag

_CACHE_TTL = int(os.environ.get("PMHARNESS_MODELS_CACHE_TTL", "86400"))  # 24h
_FETCH_TIMEOUT = 6
_MEM: dict[str, list[str]] = {}
_RECORD_MEM: dict[str, list[dict[str, Any]]] = {}
_MEM_AT: dict[str, float] = {}
# Last failure reason per provider, so an empty picker can explain WHY (bad key
# vs network vs schema change) instead of looking like the account has no models.
_LAST_ERROR: dict[str, str] = {}


def last_fetch_error(provider_name: str) -> Optional[str]:
    """The most recent live-fetch failure reason for a provider, or None if the
    last fetch succeeded. Lets the UI say 'couldn't reach provider / bad key'
    rather than silently showing an empty list."""
    return _LAST_ERROR.get(provider_name)


def invalidate_models_cache(provider_name: Optional[str] = None) -> None:
    """Drop in-memory + on-disk model catalog cache.

    Pass a provider name (e.g. ``cursor-cli``) to invalidate one entry, or
    ``None`` to clear every provider. Call after plan-account login / refresh
    so newly shipped models (Opus 5, …) are not stuck behind the 24h TTL.
    Best-effort: never raises.
    """
    try:
        if provider_name:
            _MEM.pop(provider_name, None)
            _RECORD_MEM.pop(provider_name, None)
            _MEM_AT.pop(provider_name, None)
            _LAST_ERROR.pop(provider_name, None)
            disk = _read_cache()
            if provider_name in disk:
                disk.pop(provider_name, None)
                _write_cache(disk)
            return
        _MEM.clear()
        _RECORD_MEM.clear()
        _MEM_AT.clear()
        _LAST_ERROR.clear()
        _write_cache({})
    except Exception as e:
        _diag("model_fetch.invalidate", e)


def _cache_path() -> str:
    base = os.path.join(os.path.expanduser("~"), ".pmharness")
    try:
        os.makedirs(base, exist_ok=True)
    except Exception:
        pass
    return os.path.join(base, "provider_models_cache.json")


def _read_cache() -> dict:
    try:
        with open(_cache_path(), encoding="utf-8", errors="replace") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_cache(data: dict) -> None:
    """Atomic cache write; tolerate update races that delete ``.tmp`` mid-flight."""
    import tempfile

    try:
        path = _cache_path()
        base = os.path.dirname(path) or "."
        os.makedirs(base, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=base, prefix="models_cache_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                json.dump(data, f)
            os.replace(tmp_path, path)
        except Exception:
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            raise
    except Exception as e:
        _diag("model_fetch.cache_write", e)


def _get(url: str, headers: dict) -> dict:
    req = urllib.request.Request(url, headers=headers)
    raw = urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT).read()
    return json.loads(raw)


def _fetch_codex_oauth_models(access_token: str) -> list[str]:
    """List ChatGPT Codex OAuth models (chatgpt.com backend), Hermes-aligned."""
    token = (access_token or "").strip()
    if not token:
        return []
    data = _get(
        "https://chatgpt.com/backend-api/codex/models?client_version=1.0.0",
        {"Authorization": f"Bearer {token}", "User-Agent": "pm-harness"},
    )
    entries = data.get("models", []) if isinstance(data, dict) else []
    sortable = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        slug = item.get("slug")
        if not isinstance(slug, str) or not slug.strip():
            continue
        visibility = item.get("visibility", "")
        if isinstance(visibility, str) and visibility.strip().lower() in ("hide", "hidden"):
            continue
        priority = item.get("priority")
        rank = int(priority) if isinstance(priority, (int, float)) else 10_000
        sortable.append((rank, slug.strip()))
    sortable.sort(key=lambda x: (x[0], x[1]))
    # Forward-compat: surface GPT-5.6 family when older templates are present.
    ids = [slug for _, slug in sortable]
    present = set(ids)
    forward = (
        ("gpt-5.6-sol", ("gpt-5.5", "gpt-5.4", "gpt-5.3-codex")),
        ("gpt-5.6-terra", ("gpt-5.5", "gpt-5.4", "gpt-5.3-codex")),
        ("gpt-5.6-luna", ("gpt-5.5", "gpt-5.4", "gpt-5.3-codex")),
        ("gpt-5.3-codex-spark", ("gpt-5.3-codex",)),
    )
    for newer, templates in forward:
        if newer in present:
            continue
        if any(t in present for t in templates):
            ids.append(newer)
            present.add(newer)
    return ids


def _normalize_openrouter_record(
    item: dict[str, Any], *, source: str, pricing_per_mtok: bool = False
) -> dict[str, Any] | None:
    model_id = item.get("id")
    if not isinstance(model_id, str) or not model_id.strip():
        return None
    record: dict[str, Any] = {
        "id": model_id.strip(),
        "source": source,
        "status": "available",
    }
    for field in (
        "name", "description", "canonical_slug", "context_length",
        "architecture", "modalities", "supported_parameters",
    ):
        if field in item and item[field] is not None:
            record[field] = item[field]
    pricing = item.get("pricing")
    if isinstance(pricing, dict):
        normalized = {}
        for key in ("prompt", "completion"):
            try:
                value = float(pricing[key])
                normalized[key] = value if pricing_per_mtok else value * 1_000_000
            except (KeyError, TypeError, ValueError):
                pass
        if normalized:
            record["pricing"] = normalized
    return record


def _cached_records(entry: Any) -> list[dict[str, Any]]:
    if not isinstance(entry, dict) or not isinstance(entry.get("models"), list):
        return []
    records = []
    for item in entry["models"]:
        if isinstance(item, str):
            item = {"id": item}
        if isinstance(item, dict):
            record = _normalize_openrouter_record(
                item,
                source="stale-live",
                pricing_per_mtok=bool(item.get("source") or item.get("status")),
            )
            if record:
                records.append(record)
    return records


def _flat_catalog_records(data: Any) -> list[dict[str, Any]]:
    """Id + leftover fields from a listing payload, whichever envelope it used."""
    items: Any = data
    if isinstance(items, dict):
        for envelope in ("data", "models"):
            if envelope in items:
                items = items[envelope]
                break
    if isinstance(items, dict):
        items = [
            {"id": key, **value} if isinstance(value, dict) else {"id": key}
            for key, value in items.items()
        ]
    if not isinstance(items, list):
        return []
    out: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, str):
            model_id = item.strip()
            extra: dict[str, Any] = {}
        elif isinstance(item, dict):
            model_id = str(item.get("id") or item.get("name") or "").strip()
            extra = {
                key: value for key, value in item.items()
                if key not in ("id",) and value is not None
            }
        else:
            continue
        if model_id:
            rec = {"id": model_id}
            rec.update(extra)
            out.append(rec)
    return out


def _flat_catalog_ids(data: Any) -> list[str]:
    """Model ids from a listing payload, whichever envelope it arrived in.

    Resellers are inconsistent here: OpenAI-style ``{"data": [{"id": ...}]}``,
    a ``{"models": ...}`` wrapper, a bare list, and an id-keyed object are all
    in the wild, and an unrecognized envelope has to read as "no catalog" so
    the caller falls back to curated rather than crashing.
    """
    return [record["id"] for record in _flat_catalog_records(data) if record.get("id")]


def _fetch_opencode_records(provider, key: str) -> list[dict[str, Any]]:
    """Live OpenCode Go/Zen listing with names preserved. Sets ``_LAST_ERROR``."""
    name = provider.name
    if name == "opencode-zen" or getattr(provider, "api_mode", "") == "opencode_zen":
        from .opencode_zen import driver_base_url, normalize_model_id
        label = "OpenCode Zen"
        retired = lambda _bare: False
    else:
        from .opencode_go import (
            driver_base_url,
            is_retired_deepseek_go_model,
            normalize_model_id,
        )
        label = "OpenCode Go"
        retired = is_retired_deepseek_go_model

    data = _get(
        driver_base_url(provider.base_url) + "/models",
        {"Authorization": f"Bearer {key}", "User-Agent": "pm-harness"},
    )
    records = []
    for item in _flat_catalog_records(data):
        bare = normalize_model_id(item.get("id"))
        if not bare or retired(bare):
            continue
        record = dict(item)
        record["id"] = bare
        records.append(record)
    if not records:
        _LAST_ERROR[name] = f"{label} /models returned an empty or unusable catalog"
    return records


def _fetch_provider_models(provider, key: str) -> list[Any]:
    """Hit the provider's native model-listing endpoint. Returns bare model ids
    (no provider prefix). Empty list on any failure, with the failure REASON
    recorded (diagnostics log + _LAST_ERROR) so the empty list is explainable."""
    name = provider.name
    _LAST_ERROR.pop(name, None)
    try:
        if name == "anthropic":
            data = _get(
                "https://api.anthropic.com/v1/models",
                {"x-api-key": key, "anthropic-version": "2023-06-01"},
            )
            return [m["id"] for m in data.get("data", []) if m.get("id")]
        if name == "openrouter":
            data = _get(
                "https://openrouter.ai/api/v1/models",
                {"Authorization": f"Bearer {key}", "User-Agent": "pm-harness"},
            )
            items = data.get("data") if isinstance(data, dict) else None
            if not isinstance(items, list) or not items:
                _LAST_ERROR[name] = (
                    "OpenRouter /models returned an empty or unusable catalog"
                )
                return []
            records = [
                record
                for item in items
                if isinstance(item, dict)
                for record in [_normalize_openrouter_record(item, source="live")]
                if record
            ]
            if not records:
                _LAST_ERROR[name] = (
                    "OpenRouter /models returned an empty or unusable catalog"
                )
            return records
        if name in ("opencode-go", "opencode-zen") or getattr(
            provider, "api_mode", "",
        ) in ("opencode_go", "opencode_zen"):
            return [record["id"] for record in _fetch_opencode_records(provider, key)]
        if name in ("openai", "deepseek", "zai", "xai", "nvidia"):
            # OpenAI-compatible /models listing. Accept id or name so a
            # vendor envelope that only stamps `name` still surfaces.
            resolver = getattr(provider, "resolved_base_url", None)
            base = (resolver() if callable(resolver) else provider.base_url).rstrip("/")
            data = _get(base + "/models", {"Authorization": f"Bearer {key}"})
            return _flat_catalog_ids(data)
        if name == "gemini":
            # Gemini native listing (not the OpenAI-compat shim base_url).
            data = _get(
                f"https://generativelanguage.googleapis.com/v1beta/models?key={key}",
                {},
            )
            out = []
            for m in data.get("models", []):
                mid = (m.get("name") or "").replace("models/", "")
                if mid:
                    out.append(mid)
            return out
        if name == "bedrock":
            # IAM/SigV4 against the caller's account — key arg is unused (presence
            # only). Catalog is allow-list specific; do not hardcode Claude.
            from puppetmaster.bedrock import list_chat_model_ids

            return list(list_chat_model_ids(timeout=_FETCH_TIMEOUT))
        if name == "cursor-cli" or getattr(provider, "api_mode", "") == "cursor_cli":
            # Live plan catalog via `agent models` (Composer 2.5, Grok 4.5, …).
            # Results are memoized by fetch_models(); curated is the offline fallback.
            from .cursor_cli_auth import list_models
            return list(list_models(live=True))
        if name == "openai-codex" or getattr(provider, "api_mode", "") == "codex_responses":
            # ChatGPT Codex OAuth model list (Hermes-compatible endpoint).
            return _fetch_codex_oauth_models(key)
    except Exception as e:
        # Preserve the cause: bad key, network down, and a changed provider
        # schema are very different problems and must not collapse to a silent
        # empty list. Callers still get [] and fall back to cache/curated.
        _LAST_ERROR[name] = repr(e)
        _diag("model_fetch.fetch", e, msg=f"provider={name}")
        return []
    return []


# Substrings that mark a model as NOT a chat/pilot model (image/video/audio/
# embedding/moderation/realtime/etc). These pollute the picker -- a pilot must be
# a text chat model. Matched case-insensitively against the bare model id.
_NON_CHAT_MARKERS = (
    "embedding", "embed", "tts", "whisper", "audio", "transcribe", "realtime",
    "image", "imagen", "veo", "lyria", "dall-e", "dalle", "vision-only",
    "moderation", "rerank", "guard", "aqa", "speech", "music", "video",
    "robotics", "computer-use", "-tts", "nano-banana",
)


def _is_chat_model(model_id: str) -> bool:
    m = (model_id or "").lower()
    if not m:
        return False
    return not any(marker in m for marker in _NON_CHAT_MARKERS)


def fetch_model_records(provider, key: str, *, force: bool = False) -> list[dict[str, Any]]:
    """Return normalized live records, using stale records only when needed."""
    name = provider.name
    if os.environ.get("PMHARNESS_LIVE_MODELS", "1") == "0":
        return []
    now = time.time()
    if not force and name in _RECORD_MEM and (
        time.monotonic() - _MEM_AT.get(name, 0)
    ) < _CACHE_TTL:
        return _RECORD_MEM[name]
    disk = _read_cache()
    entry = disk.get(name)
    if not force:
        cached = _cached_records(entry)
        if cached and (now - entry.get("fetched_at", 0)) < _CACHE_TTL:
            _RECORD_MEM[name] = cached
            _MEM[name] = [record["id"] for record in cached]
            _MEM_AT[name] = time.monotonic()
            return cached
    if name in ("opencode-go", "opencode-zen") or getattr(
        provider, "api_mode", "",
    ) in ("opencode_go", "opencode_zen"):
        _LAST_ERROR.pop(name, None)
        try:
            raw = _fetch_opencode_records(provider, key)
        except Exception as e:
            _LAST_ERROR[name] = repr(e)
            _diag("model_fetch.fetch", e, msg=f"provider={name}")
            raw = []
    else:
        raw = _fetch_provider_models(provider, key)
    if name == "openrouter":
        fresh = [
            record for record in raw
            if isinstance(record, dict) and _is_chat_model(record["id"])
        ]
    else:
        fresh = []
        for item in raw:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                if not _is_chat_model(item["id"]):
                    continue
                record = dict(item)
                record.setdefault("source", "live")
                record.setdefault("status", "available")
                fresh.append(record)
            elif isinstance(item, str) and _is_chat_model(item):
                fresh.append({
                    "id": item, "source": "live", "status": "available",
                })
    if fresh:
        disk[name] = {"fetched_at": now, "models": fresh}
        _write_cache(disk)
        _RECORD_MEM[name] = fresh
        _MEM[name] = [record["id"] for record in fresh]
        _MEM_AT[name] = time.monotonic()
        return fresh
    stale = _cached_records(entry)
    if stale:
        _RECORD_MEM[name] = stale
        _MEM[name] = [record["id"] for record in stale]
        _MEM_AT[name] = time.monotonic()
        return stale
    return []


def model_metadata(provider_name: str, slug: str) -> dict[str, Any] | None:
    """Resolve an exact cached/live provider slug; never fuzzy-matches."""
    target = (slug or "").strip()
    for record in _RECORD_MEM.get(provider_name, []):
        if record.get("id") == target or record.get("canonical_slug") == target:
            return record
    for record in _cached_records(_read_cache().get(provider_name)):
        if record.get("id") == target or record.get("canonical_slug") == target:
            return record
    return None


def fetch_status(provider_name: str) -> dict[str, str | None]:
    """Expose the last live-fetch outcome without exposing credentials."""
    records = _RECORD_MEM.get(provider_name) or []
    err = _LAST_ERROR.get(provider_name)
    if err:
        if records:
            source = records[0].get("source", "stale-live")
            status = "stale" if source == "stale-live" else "error"
            return {"source": source, "status": status, "error": err}
        return {"source": "error", "status": "error", "error": err}
    if records:
        source = records[0].get("source", "live")
        status = "available" if source == "live" else "stale"
        return {"source": source, "status": status, "error": None}
    return {"source": "empty", "status": "empty", "error": None}


def fetch_models(provider, key: str, *, force: bool = False) -> list[str]:
    """Live model ids for a keyed provider, memoized in-process and cached on
    disk with a TTL. Returns [] on total failure (caller merges with curated)."""
    return [record["id"] for record in fetch_model_records(provider, key, force=force)]


# Direct-vendor Settings lists can lag the model the account already serves
# (Z.AI Coding Plan listing glm-5.2 while glm-5.3 is live). OpenRouter's
# public catalog is a second discovery source; we only take the vendor
# namespace, never dump the whole OR field.
_OR_VENDOR_PREFIX = {
    "zai": "z-ai/",
    "anthropic": "anthropic/",
    "openai": "openai/",
    "deepseek": "deepseek/",
    "xai": "x-ai/",
    "minimax": "minimax/",
    "gemini": "google/",
}

_OPENROUTER_PUBLIC_CACHE = "openrouter_public"


def _openrouter_cached_ids() -> list[str]:
    records = _RECORD_MEM.get("openrouter") or []
    if records:
        return [record["id"] for record in records if record.get("id")]
    return [
        record["id"]
        for record in _cached_records(_read_cache().get("openrouter"))
        if record.get("id")
    ]


def _fetch_openrouter_public_ids() -> list[str]:
    """Unauthenticated OpenRouter /models; cached like other provider lists."""
    if os.environ.get("PMHARNESS_LIVE_MODELS", "1") == "0":
        return []
    now = time.time()
    disk = _read_cache()
    entry = disk.get(_OPENROUTER_PUBLIC_CACHE)
    cached = _cached_records(entry)
    if cached and (now - entry.get("fetched_at", 0)) < _CACHE_TTL:
        return [record["id"] for record in cached if record.get("id")]
    try:
        data = _get(
            "https://openrouter.ai/api/v1/models",
            {"User-Agent": "pm-harness"},
        )
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return [record["id"] for record in cached if record.get("id")]
        fresh = [
            record
            for item in items
            if isinstance(item, dict)
            for record in [_normalize_openrouter_record(item, source="live")]
            if record and _is_chat_model(record["id"])
        ]
        if fresh:
            disk[_OPENROUTER_PUBLIC_CACHE] = {"fetched_at": now, "models": fresh}
            _write_cache(disk)
            return [record["id"] for record in fresh]
    except Exception as e:
        _diag("model_fetch.openrouter_public", e)
    return [record["id"] for record in cached if record.get("id")]


def vendor_ids_from_openrouter(provider_name: str, *, force: bool = False) -> list[str]:
    """Bare vendor ids from a cached or public OpenRouter catalog.

    Cache-only on a normal load (no extra network). Settings refresh
    (``force=True``) may hit the public listing when no keyed OR cache
    exists so a Z.AI-only install can still see ``glm-5.3``.
    """
    prefix = _OR_VENDOR_PREFIX.get(provider_name)
    if not prefix:
        return []
    try:
        ids = _openrouter_cached_ids()
        if not ids and force:
            ids = _fetch_openrouter_public_ids()
        out = []
        seen = set()
        for mid in ids:
            raw = str(mid or "").strip()
            if not raw.lower().startswith(prefix):
                continue
            bare = raw[len(prefix):]
            if not bare or not _is_chat_model(bare) or bare in seen:
                continue
            seen.add(bare)
            out.append(bare)
        return out
    except Exception as e:
        _diag("model_fetch.or_vendor_overlay", e, msg=f"provider={provider_name}")
        return []
