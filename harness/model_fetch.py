"""Live per-provider model discovery.

The curated ``pilot_models`` tuples in providers.py are a hardcoded fallback that
drifts (Anthropic ships 9 models but the list shows 3; OpenAI has gpt-5.5 but the
list stopped at 5.4). This module fetches each KEYED provider's REAL model catalog
from its own listing endpoint, caches it on disk with a TTL, and merges it with the
curated fallback so the picker reflects what the account can actually use.

Stdlib-only (urllib, json). Every fetch degrades gracefully: any network/auth/parse
failure falls back to the cached list, then to the curated pilot_models. Never raises.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from typing import Optional

_CACHE_TTL = int(os.environ.get("PMHARNESS_MODELS_CACHE_TTL", "86400"))  # 24h
_FETCH_TIMEOUT = 6
_MEM: dict[str, list[str]] = {}
_MEM_AT: dict[str, float] = {}


def _cache_path() -> str:
    base = os.path.join(os.path.expanduser("~"), ".pmharness")
    try:
        os.makedirs(base, exist_ok=True)
    except Exception:
        pass
    return os.path.join(base, "provider_models_cache.json")


def _read_cache() -> dict:
    try:
        with open(_cache_path()) as f:
            return json.load(f)
    except Exception:
        return {}


def _write_cache(data: dict) -> None:
    try:
        path = _cache_path()
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except Exception:
        pass


def _get(url: str, headers: dict) -> dict:
    req = urllib.request.Request(url, headers=headers)
    raw = urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT).read()
    return json.loads(raw)


def _fetch_provider_models(provider, key: str) -> list[str]:
    """Hit the provider's native model-listing endpoint. Returns bare model ids
    (no provider prefix). Empty list on any failure."""
    name = provider.name
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
            return [m["id"] for m in data.get("data", []) if m.get("id")]
        if name in ("openai", "deepseek", "zai", "xai", "nvidia"):
            # OpenAI-compatible /models listing.
            base = provider.base_url.rstrip("/")
            data = _get(base + "/models", {"Authorization": f"Bearer {key}"})
            return [m["id"] for m in data.get("data", []) if m.get("id")]
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
    except Exception:
        return []
    return []


def fetch_models(provider, key: str, *, force: bool = False) -> list[str]:
    """Live model ids for a keyed provider, memoized in-process and cached on
    disk with a TTL. Returns [] on total failure (caller merges with curated)."""
    name = provider.name
    if os.environ.get("PMHARNESS_LIVE_MODELS", "1") == "0":
        return []
    now = time.time()
    if not force and name in _MEM and (time.monotonic() - _MEM_AT.get(name, 0)) < _CACHE_TTL:
        return _MEM[name]
    disk = _read_cache()
    entry = disk.get(name)
    if not force and isinstance(entry, dict):
        fetched_at = entry.get("fetched_at", 0)
        models = entry.get("models")
        if isinstance(models, list) and (now - fetched_at) < _CACHE_TTL:
            _MEM[name] = models
            _MEM_AT[name] = time.monotonic()
            return models
    fresh = _fetch_provider_models(provider, key)
    if fresh:
        disk[name] = {"fetched_at": now, "models": fresh}
        _write_cache(disk)
        _MEM[name] = fresh
        _MEM_AT[name] = time.monotonic()
        return fresh
    # Fetch failed -> stale disk cache if present, else empty.
    if isinstance(entry, dict) and isinstance(entry.get("models"), list):
        _MEM[name] = entry["models"]
        _MEM_AT[name] = time.monotonic()
        return entry["models"]
    return []
