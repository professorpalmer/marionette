"""Live per-provider model discovery.

The curated ``pilot_models`` tuples in providers.py are a hardcoded fallback that
drifts (Anthropic ships 9 models but the list shows 3; OpenAI ships gpt-5.6
Sol/Terra/Luna while static fallbacks may still list 5.5/5.4). This module
fetches each KEYED provider's REAL model catalog
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

from .diag import note as _diag

_CACHE_TTL = int(os.environ.get("PMHARNESS_MODELS_CACHE_TTL", "86400"))  # 24h
_FETCH_TIMEOUT = 6
_MEM: dict[str, list[str]] = {}
_MEM_AT: dict[str, float] = {}
# Last failure reason per provider, so an empty picker can explain WHY (bad key
# vs network vs schema change) instead of looking like the account has no models.
_LAST_ERROR: dict[str, str] = {}


def last_fetch_error(provider_name: str) -> Optional[str]:
    """The most recent live-fetch failure reason for a provider, or None if the
    last fetch succeeded. Lets the UI say 'couldn't reach provider / bad key'
    rather than silently showing an empty list."""
    return _LAST_ERROR.get(provider_name)


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
    try:
        path = _cache_path()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except Exception as e:
        _diag("model_fetch.cache_write", e)


def _get(url: str, headers: dict) -> dict:
    req = urllib.request.Request(url, headers=headers)
    raw = urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT).read()
    return json.loads(raw)


def _fetch_provider_models(provider, key: str) -> list[str]:
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
    # Keep only chat/pilot-capable models (drop image/video/audio/embedding/etc).
    fresh = [m for m in fresh if _is_chat_model(m)]
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
