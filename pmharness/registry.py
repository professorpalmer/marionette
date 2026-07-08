from __future__ import annotations

"""Data-driven driver registry built from pmharness/catalog.json -- the
research artifact listing every open-weights harness candidate with license and
native cost metadata.

Two reach modes:
  - "openrouter" (default): every model through one OpenAI-compatible endpoint
    with one key (OPENROUTER_API_KEY). Best for breadth; study the whole field
    fast. Driver-quality measurement is identical regardless of reach.
  - "native": provider's own endpoint + key. Use for finalists where the cost
    receipt must reflect true native pricing (not OpenRouter markup).

The stub oracle is always available offline with no key.
"""

import json
from pathlib import Path
from typing import Optional

from .drivers.base import Driver
from .drivers.stub import StubDriver
from .drivers.openai_compat import OpenAICompatDriver


_CATALOG_PATH = Path(__file__).resolve().parent / "catalog.json"

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
OPENROUTER_KEY_ENV = "OPENROUTER_API_KEY"


def load_catalog() -> dict:
    with open(_CATALOG_PATH) as f:
        return json.load(f)


def _entry(name: str) -> dict:
    cat = load_catalog()
    for m in cat["models"]:
        if m["name"] == name:
            return m
    raise KeyError(f"unknown model {name!r}; known={[m['name'] for m in cat['models']]}")


def model_names(tier: Optional[str] = None) -> list:
    cat = load_catalog()
    return [m["name"] for m in cat["models"] if tier is None or m["tier"] == tier]


def price(name: str) -> tuple:
    """Native (price_in, price_out) per Mtok for the cost column. Tries the live
    OpenRouter rate first (reflects what the user is actually billed); falls back
    to the eval catalog (exact native rates for benchmarked models); otherwise
    (None, None). For provider:model picker specs like 'anthropic:claude-opus-4-8'
    the live map supplies real published rates when the catalog has no entry."""
    live = _resolve_live_price(name)
    if live is not None:
        return live
    try:
        m = _entry(name)
        pin, pout = m.get("price_in"), m.get("price_out")
        if pin is not None and pout is not None:
            return (pin, pout)
    except Exception:
        pass
    return (None, None)


def resolve_price(name: str, default_in: float = 0.5, default_out: float = 2.0) -> tuple:
    """price() with a numeric fallback, for the cost estimator UI."""
    pin, pout = price(name)
    if pin is None or pout is None:
        return (default_in, default_out)
    return (pin, pout)


# ---- live OpenRouter context-window map (cached) --------------------------
# OpenRouter's /models endpoint returns each model's real context_length with NO
# auth, and is more current than our hand-maintained catalog (which drifts:
# glm-5.2 was 200K in-catalog but is really 1M; gpt-5.4 was missing but is 1.05M).
# We fetch it once, cache to disk with a TTL, and resolve context_window() from
# the live map first, then the catalog, then a sane floor. Stdlib-only; any
# network/parse failure degrades gracefully to the cache, then the catalog.
import json as _json
import os as _os
import re as _re
import threading as _threading
import time as _time
import urllib.request as _urlreq

_CW_FLOOR = 200000               # sane floor for unknown models (was a flat 96K)
_CW_CACHE_TTL = int(_os.environ.get("PMHARNESS_OR_CACHE_TTL", "86400"))  # 24h
_CW_FETCH_TIMEOUT = 6            # seconds; keep short so offline never stalls
_CW_LOCK = _threading.Lock()
_CW_MEM = None                   # in-process memo: dict[slug] -> ctx (or {})
_CW_MEM_AT = 0.0                 # monotonic time the memo was set


def _cw_cache_path() -> str:
    base = _os.path.join(_os.path.expanduser("~"), ".pmharness")
    try:
        _os.makedirs(base, exist_ok=True)
    except Exception:
        pass
    return _os.path.join(base, "or_models_cache.json")


def _cw_norm(slug: str) -> str:
    """Normalize a model id/slug for fuzzy matching: drop the provider prefix,
    lowercase, and strip every non-alphanumeric (so 'claude-opus-4-8',
    'anthropic/claude-opus-4.8' both collapse to 'claudeopus48')."""
    s = (slug or "").lower()
    if "/" in s:
        s = s.split("/", 1)[1]
    return _re.sub(r"[^a-z0-9]", "", s)


# Live OpenRouter PRICE map {slug: (price_in_per_Mtok, price_out_per_Mtok)},
# populated as a side effect of the windows fetch (same /models payload) so the
# cost estimator shows real rates (Opus 4.8 = $5/$25) instead of a placeholder.
_PRICE_MEM: dict = {}


def _fetch_live_windows() -> dict:
    """Fetch {slug: context_length} from OpenRouter (no auth). Also populates the
    module-level live price map from the same payload. Returns {} on any failure;
    caller handles caching + fallback."""
    global _PRICE_MEM
    req = _urlreq.Request(
        "https://openrouter.ai/api/v1/models",
        headers={"User-Agent": "pm-harness"},
    )
    raw = _urlreq.urlopen(req, timeout=_CW_FETCH_TIMEOUT).read()
    data = _json.loads(raw)
    out = {}
    prices = {}
    for m in data.get("data", []):
        mid = m.get("id")
        ctx = m.get("context_length")
        if mid and isinstance(ctx, int) and ctx > 0:
            out[mid] = ctx
        # OpenRouter pricing is per-TOKEN as decimal strings; x1e6 -> per Mtok.
        pr = m.get("pricing") or {}
        try:
            p_in = float(pr.get("prompt"))
            p_out = float(pr.get("completion"))
            if mid and (p_in > 0 or p_out > 0):
                prices[mid] = (p_in * 1.0e6, p_out * 1.0e6)
        except (TypeError, ValueError):
            pass
    if prices:
        _PRICE_MEM = prices
    return out


def _resolve_live_price(name: str):
    """Best live OpenRouter (price_in, price_out) per Mtok for `name`, mirroring
    _resolve_live_window's slug matching. Returns None if no live match."""
    _live_windows()  # ensure the fetch ran (populates _PRICE_MEM as a side effect)
    live = _PRICE_MEM
    if not live:
        return None
    candidates = []
    try:
        ent = _entry(name)
        if ent.get("openrouter"):
            candidates.append(ent["openrouter"])
    except Exception:
        pass
    if ":" in name:
        candidates.append(name.split(":", 1)[1])
    candidates.append(name)
    for c in candidates:
        if c in live:
            return live[c]
    # Fuzzy normalized match, shortest slug wins (base model over -fast/-image).
    targets = {_cw_norm(c) for c in candidates if c}
    best = None
    best_len = 10 ** 9
    for slug, pair in live.items():
        if _cw_norm(slug) in targets and len(slug) < best_len:
            best = pair
            best_len = len(slug)
    return best


def _restore_prices_from_disk(disk: dict) -> None:
    """Restore the live price map from a disk-cached payload (so cost estimates
    work without a fresh network fetch). Tolerates an old cache with no prices."""
    global _PRICE_MEM
    try:
        cached = disk.get("prices")
        if isinstance(cached, dict) and cached and not _PRICE_MEM:
            _PRICE_MEM = {k: (float(v[0]), float(v[1])) for k, v in cached.items()
                          if isinstance(v, (list, tuple)) and len(v) == 2}
    except Exception:
        pass


def _live_windows() -> dict:
    """The live OpenRouter window map, memoized in-process and cached on disk
    with a TTL. Never raises; never blocks more than once per process. Set
    PMHARNESS_OR_LIVE_WINDOWS=0 to disable the live source entirely (tests)."""
    global _CW_MEM, _CW_MEM_AT
    if _os.environ.get("PMHARNESS_OR_LIVE_WINDOWS", "1") == "0":
        return {}
    # In-process memo: attempt the network at most once per process.
    if _CW_MEM is not None:
        return _CW_MEM
    with _CW_LOCK:
        if _CW_MEM is not None:
            return _CW_MEM
        path = _cw_cache_path()
        disk = None
        try:
            with open(path) as f:
                disk = _json.load(f)
        except Exception:
            disk = None
        # Fresh disk cache -> use it, no network.
        if isinstance(disk, dict):
            fetched_at = disk.get("fetched_at", 0)
            windows = disk.get("windows")
            if isinstance(windows, dict) and (_time.time() - fetched_at) < _CW_CACHE_TTL:
                _CW_MEM = {k: int(v) for k, v in windows.items()}
                _restore_prices_from_disk(disk)
                _CW_MEM_AT = _time.monotonic()
                return _CW_MEM
        # Stale or missing -> try one fetch; on failure fall back to stale disk.
        try:
            fresh = _fetch_live_windows()
            if fresh:
                try:
                    with open(path, "w") as f:
                        _json.dump({"fetched_at": _time.time(), "windows": fresh,
                                    "prices": _PRICE_MEM}, f)
                except Exception:
                    pass
                _CW_MEM = fresh
                _CW_MEM_AT = _time.monotonic()
                return _CW_MEM
        except Exception:
            pass
        # Network failed -> use stale disk cache if present, else empty.
        if isinstance(disk, dict) and isinstance(disk.get("windows"), dict):
            _CW_MEM = {k: int(v) for k, v in disk["windows"].items()}
            _restore_prices_from_disk(disk)
        else:
            _CW_MEM = {}
        _CW_MEM_AT = _time.monotonic()
        return _CW_MEM


def _resolve_live_window(name: str) -> int:
    """Best live OpenRouter window for `name` (a catalog name, a 'provider:model'
    spec, a bare slug, or a native model id). 0 if no live match."""
    live = _live_windows()
    if not live:
        return 0
    # Build candidate slugs to try as EXACT keys first.
    candidates = []
    # 1. catalog entry's openrouter slug
    try:
        ent = _entry(name)
        if ent.get("openrouter"):
            candidates.append(ent["openrouter"])
    except Exception:
        pass
    # 2. provider:model spec -> strip the provider prefix
    if ":" in name:
        candidates.append(name.split(":", 1)[1])
    # 3. the raw name itself (may already be a slug)
    candidates.append(name)
    for c in candidates:
        if c in live:
            return int(live[c])
    # Fuzzy: normalized-equality, preferring the shortest matching id so a base
    # model wins over '-fast' / '-image' / '-codex' variants.
    targets = {_cw_norm(c) for c in candidates if c}
    best = 0
    best_len = 10 ** 9
    for slug, ctx in live.items():
        if _cw_norm(slug) in targets and len(slug) < best_len:
            best = int(ctx)
            best_len = len(slug)
    return best


# ---- static offline fallback windows ----------------------------------------
# When the live OpenRouter map is unavailable (offline, slow network, fresh
# packaged app with an empty cache) AND the spec is a provider-prefixed picker
# entry like "anthropic:claude-opus-4-8" (which is NOT a catalog model name),
# resolution used to collapse to the flat floor -- so a 1M-window Opus showed as
# 200K. This static table is keyed by NORMALIZED slug (see _cw_norm) and gives
# well-known frontier families their real windows with zero network dependency.
# Matching is longest-prefix on the normalized slug, so "claudeopus48" matches
# the "claudeopus" family entry. Live map and catalog still win when present.
_STATIC_WINDOWS = {
    # Anthropic Claude: Opus/Sonnet 4.x carry 1M; Haiku 200K.
    "claudeopus": 1000000,
    "claudesonnet": 1000000,
    "claudehaiku": 200000,
    "claude3opus": 200000,
    "claude3sonnet": 200000,
    "claude3haiku": 200000,
    # OpenAI GPT-5 family: 1M+ windows; mini/nano 400K.
    "gpt55": 400000,
    "gpt54mini": 400000,
    "gpt54nano": 400000,
    "gpt54": 1050000,
    "gpt5mini": 400000,
    "gpt5nano": 400000,
    "gpt5": 400000,
    # Google Gemini: 1M-2M windows.
    "gemini35flash": 1000000,
    "gemini31pro": 1000000,
    "gemini31flash": 1000000,
    "gemini25pro": 1000000,
    "gemini25flash": 1000000,
    "gemini": 1000000,
    # Open-weights frontier families.
    "glm52": 1000000,
    "glm47": 128000,
    "glm": 128000,
    "qwen3coder": 262144,
    "qwen": 262144,
    "kimik2": 256000,
    "kimi": 200000,
    "minimaxm2": 1000000,
    "minimax": 1000000,
    "deepseekv4": 128000,
    "deepseek": 128000,
}


def _static_window(name: str) -> int:
    """Best static-table window for `name` (a catalog name, a 'provider:model'
    spec, a bare slug, or a native model id). 0 if no family match. Uses
    longest-prefix match on the normalized slug so 'claudeopus48' resolves via
    the 'claudeopus' family entry."""
    candidates = []
    if ":" in name:
        candidates.append(name.split(":", 1)[1])
    if "/" in name:
        candidates.append(name.split("/", 1)[1])
    candidates.append(name)
    best = 0
    best_keylen = 0
    for c in candidates:
        norm = _cw_norm(c)
        if not norm:
            continue
        for key, win in _STATIC_WINDOWS.items():
            if norm.startswith(key) and len(key) > best_keylen:
                best = win
                best_keylen = len(key)
    return best


def context_window(name: str, default: int = _CW_FLOOR) -> int:
    """The model's real input context window (tokens). Resolves in order:
    live OpenRouter /models map (cached) -> catalog `context_window` ->
    static offline fallback table -> `default` (a 200K floor). Lets the harness
    use each model's true capacity (opus 1M, gpt-5.4 1.05M, glm-5.2 1M) instead
    of a stale catalog value or a flat 96K -- even offline. Never raises."""
    try:
        live = _resolve_live_window(name)
        if live > 0:
            return live
    except Exception:
        pass
    try:
        m = _entry(name)
        w = m.get("context_window")
        if w:
            return int(w)
    except Exception:
        pass
    try:
        static = _static_window(name)
        if static > 0:
            return static
    except Exception:
        pass
    return default


def build(name: str, *, reach: str = "openrouter") -> Driver:
    import os as _os
    _mt = int(_os.environ.get("HARNESS_MAX_TOKENS", "8000"))
    """Construct a Driver for a catalog model.

    reach='openrouter' routes through OpenRouter (one key for the whole field).
    reach='native' uses the provider's own endpoint where defined.
    """
    if name == "stub-oracle":
        return StubDriver()
    if name == "stub-oracle-mt":
        from .drivers.stub_multiturn import StubMultiTurnDriver
        return StubMultiTurnDriver()
    if name == "stub-oracle-v2":
        from .drivers.stub_v2 import StubV2Driver
        return StubV2Driver()

    cat = load_catalog()
    moa_presets = cat.get("moa_presets", {})
    if name in moa_presets or name.startswith("moa-"):
        preset = moa_presets.get(name, moa_presets.get("moa-planner"))
        if not preset:
            raise KeyError(f"MoA preset {name} not found and no default planner preset available")
        from .drivers.moa import MoADriver
        return MoADriver(
            name=name,
            proposers=preset["proposers"],
            aggregator=preset["aggregator"],
            reach=reach,
            builder=build,
        )

    m = _entry(name)

    if reach == "native":
        nat = m.get("native")
        if not nat:
            raise ValueError(
                f"{name} has no native endpoint defined; use reach='openrouter'"
            )
        if nat.get("driver") == "anthropic":
            from .drivers.anthropic import AnthropicDriver
            return AnthropicDriver(
                name=name, model=nat["model"],
                base_url=nat["base_url"], api_key_env=nat["api_key_env"],
                max_tokens=_mt,
            )
        if nat.get("driver") == "gemini":
            from .drivers.gemini import GeminiDriver
            return GeminiDriver(
                name=name, model=nat["model"],
                base_url=nat["base_url"], api_key_env=nat["api_key_env"],
                max_tokens=_mt,
            )
        return OpenAICompatDriver(
            name=name, model=nat["model"], base_url=nat["base_url"],
            api_key_env=nat["api_key_env"], max_tokens=_mt,
        )

    if reach == "openrouter":
        slug = m.get("openrouter")
        if not slug:
            raise ValueError(f"{name} has no OpenRouter slug")
        return OpenAICompatDriver(
            name=name, model=slug, base_url=OPENROUTER_BASE,
            api_key_env=OPENROUTER_KEY_ENV, max_tokens=_mt,
            extra_headers={
                "HTTP-Referer": "https://github.com/professorpalmer/marionette",
                "X-Title": "pm-harness driver eval",
            },
        )

    raise ValueError(f"unknown reach {reach!r}; use 'openrouter' or 'native'")


# Convenience: all driver names (incl. the offline oracle).
def all_driver_names() -> list:
    return ["stub-oracle"] + model_names()


def has_vision(name: str) -> bool:
    """True if the model accepts native image input (HF task image-text-to-text)."""
    return bool(_entry(name).get("vision", False))


def vision_sidecars() -> list:
    """Cheap open VLMs the harness can use to transcribe image -> text artifact so
    a text-only DRIVER can consume it. Vision is a harness capability, not a
    driver requirement."""
    return [m["name"] for m in load_catalog()["models"]
            if m.get("tier") == "vision_sidecar"]
