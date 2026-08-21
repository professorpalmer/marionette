from __future__ import annotations

"""Best-effort models.dev metadata overlay (stdlib, never on chat hot paths).

Native provider ``/models`` remains availability authority. This module only
supplies name/capability metadata for ids the caller already accepted. A
miss, timeout, or stale cache returns ``{}`` / ``None`` — never raises.
"""

import json
import os
import time
import urllib.request
from typing import Any, Optional

from .diag import note as _diag

MODELS_DEV_URL = "https://models.dev/api.json"
_FETCH_TIMEOUT = 4
_CACHE_TTL = int(os.environ.get("PMHARNESS_MODELS_DEV_CACHE_TTL", "86400"))

# Hermes mapping: Marionette / Hermes provider id → models.dev provider id.
PROVIDER_TO_MODELS_DEV = {
    "opencode-zen": "opencode",
    "opencode": "opencode",
    "opencode-go": "opencode-go",
}

_MEM: dict[str, Any] = {}
_MEM_AT = 0.0


def _cache_path() -> str:
    state_dir = os.environ.get("HARNESS_STATE_DIR")
    base = state_dir if state_dir else os.path.join(os.path.expanduser("~"), ".pmharness")
    try:
        os.makedirs(base, exist_ok=True)
    except Exception:
        pass
    return os.path.join(base, "models_dev_cache.json")


def _read_disk() -> dict[str, Any]:
    try:
        with open(_cache_path(), encoding="utf-8", errors="replace") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_disk(payload: dict[str, Any]) -> None:
    try:
        path = _cache_path()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(payload, f)
        os.replace(tmp, path)
    except Exception as e:
        _diag("models_dev.cache_write", e)


def _registry(*, allow_network: bool) -> dict[str, Any]:
    """models.dev tree, memory then disk then (optional) network. Never raises."""
    global _MEM, _MEM_AT
    now = time.time()
    if _MEM and (now - _MEM_AT) < _CACHE_TTL:
        return _MEM
    disk = _read_disk()
    cached = disk.get("registry") if isinstance(disk.get("registry"), dict) else {}
    fetched_at = float(disk.get("fetched_at") or 0)
    if cached and (now - fetched_at) < _CACHE_TTL:
        _MEM = cached
        _MEM_AT = fetched_at
        return cached
    if not allow_network:
        if cached:
            _MEM = cached
            _MEM_AT = fetched_at or now
        return cached
    try:
        req = urllib.request.Request(
            MODELS_DEV_URL, headers={"User-Agent": "Marionette"},
        )
        raw = urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT).read()
        parsed = json.loads(raw)
        if isinstance(parsed, dict) and parsed:
            _MEM = parsed
            _MEM_AT = now
            _write_disk({"fetched_at": now, "registry": parsed})
            return parsed
    except Exception as e:
        _diag("models_dev.fetch", e)
    if cached:
        _MEM = cached
        _MEM_AT = fetched_at or now
    return cached


def models_dev_provider_id(provider_name: str) -> str:
    key = (provider_name or "").strip().lower()
    return PROVIDER_TO_MODELS_DEV.get(key, key)


def lookup_model(
    provider_name: str,
    model_id: str,
    *,
    allow_network: bool = True,
) -> Optional[dict[str, Any]]:
    """Normalized metadata for one model, or None. Never raises."""
    try:
        mid = (model_id or "").strip()
        if not mid:
            return None
        registry = _registry(allow_network=allow_network)
        if not registry:
            return None
        provider = registry.get(models_dev_provider_id(provider_name))
        if not isinstance(provider, dict):
            return None
        models = provider.get("models")
        if not isinstance(models, dict):
            return None
        raw = models.get(mid) or models.get(mid.lower())
        if raw is None:
            bare = mid.rsplit("/", 1)[-1]
            raw = models.get(bare) or models.get(bare.lower())
        if not isinstance(raw, dict):
            return None
        out: dict[str, Any] = {"id": mid, "source": "models.dev"}
        name = raw.get("name")
        if isinstance(name, str) and name.strip():
            out["name"] = name.strip()
        limit = raw.get("limit") if isinstance(raw.get("limit"), dict) else {}
        context = limit.get("context") or raw.get("context")
        try:
            if context is not None:
                out["context_length"] = int(context)
        except (TypeError, ValueError):
            pass
        for flag in ("tool_call", "reasoning", "attachment", "vision"):
            if flag in raw:
                out[flag] = bool(raw.get(flag))
        return out
    except Exception as e:
        _diag("models_dev.lookup", e)
        return None


def clear_cache_for_tests() -> None:
    global _MEM, _MEM_AT
    _MEM = {}
    _MEM_AT = 0.0
