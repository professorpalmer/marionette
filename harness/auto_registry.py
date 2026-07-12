from __future__ import annotations

"""Auto-registry: build and refresh the agentic model registry automatically.

The swarm uses HARNESS_SWARM_ADAPTER=agentic, which routes via Puppetmaster's
key-aware router over the 'agentic' entries in ~/.puppetmaster/models.json.
This module automatically syncs those entries from whatever provider API keys
the user has, without requiring any hand-editing of models.json.

Users never have to remember/reset/curate models.json -- it stays fresh based
on their connected provider keys.
"""

import json
import os
from typing import Optional

from .diag import note as _diag


# Benchmark-anchored capability scores and pricing tiers per provider.
# These are curated templates that ensure sensible ranking without user tuning.
# Format: provider -> tier -> (capability_score, input_per_mtok_usd, output_per_mtok_usd, context_window, tags)
_AGENTIC_TEMPLATES = {
    "anthropic": {
        "frontier": (92, 3.0, 15.0, 200000, ["frontier", "reasoning", "analysis"]),
        "balanced": (85, 3.0, 15.0, 200000, ["balanced", "fast", "vision"]),
        "cheap": (70, 0.8, 4.0, 200000, ["cheap", "fast", "vision"]),
    },
    "openai-api": {
        "frontier": (90, 2.5, 10.0, 128000, ["frontier", "reasoning"]),
        "balanced": (85, 0.15, 0.6, 128000, ["balanced", "fast", "vision"]),
        "cheap": (70, 0.15, 0.6, 128000, ["cheap", "fast", "vision"]),
    },
    "gemini": {
        "frontier": (82, 1.25, 5.0, 1000000, ["frontier", "long-context"]),
        "balanced": (75, 0.075, 0.3, 1000000, ["balanced", "fast", "vision", "long-context"]),
        "cheap": (65, 0.075, 0.3, 1000000, ["cheap", "fast", "vision", "long-context"]),
    },
    "openrouter": {
        "frontier": (90, 3.0, 15.0, 200000, ["frontier", "reasoning"]),
        "balanced": (80, 0.5, 1.5, 128000, ["balanced", "fast"]),
        "cheap": (70, 0.08, 0.24, 128000, ["cheap", "fast"]),
    },
    "deepseek": {
        "balanced": (80, 0.14, 0.28, 64000, ["balanced", "reasoning"]),
        "cheap": (75, 0.14, 0.28, 64000, ["cheap", "reasoning"]),
    },
    "zai": {
        "balanced": (78, 0.5, 1.5, 128000, ["balanced", "fast"]),
        "cheap": (70, 0.5, 1.5, 128000, ["cheap", "fast"]),
    },
    "xai": {
        "frontier": (85, 5.0, 15.0, 131072, ["frontier", "reasoning"]),
        "balanced": (80, 2.0, 10.0, 131072, ["balanced", "fast", "vision"]),
    },
    "bedrock": {
        "frontier": (92, 3.0, 15.0, 200000, ["frontier", "reasoning", "analysis"]),
        "balanced": (85, 3.0, 15.0, 200000, ["balanced", "fast", "vision"]),
        "cheap": (70, 0.8, 4.0, 200000, ["cheap", "fast", "vision"]),
    },
}

# Benchmark-anchored per-model overrides (mid-2026 OpenRouter data). The tier
# templates above are coarse; for models we know, stamp real capability,
# marketplace pricing, and context so the router's cost math is honest.
# Format: slug -> (capability_score, input_usd, output_usd, context_window, tags)
_KNOWN_MODEL_SPECS = {
    "deepseek/deepseek-v4-flash": (66, 0.09, 0.18, 1000000, ["cheap", "fast", "code", "reading", "long-context"]),
    "deepseek/deepseek-v4-pro": (80, 0.435, 0.87, 1000000, ["balanced", "code", "reasoning", "long-context"]),
    "minimax/minimax-m3": (79, 0.098, 1.21, 1000000, ["balanced", "code", "vision", "long-context"]),
    "moonshotai/kimi-k2.6": (78, 0.66, 3.41, 262144, ["balanced", "code", "vision", "agent-loop"]),
    "z-ai/glm-5.2": (86, 1.0, 3.5, 1000000, ["quality", "code", "reasoning", "long-context"]),
    "anthropic/claude-opus-4.8": (99, 5.0, 25.0, 1000000, ["frontier", "reasoning", "code", "vision", "long-context"]),
}

# Per-provider model discovery: maps provider name to a list of model descriptors.
# Each descriptor: (model_name, tier, slug_for_id)
# We prefer the user's enabled picker models, then live discovery via
# model_fetch; these are the last-resort curated sets.
_CURATED_MODELS = {
    "anthropic": [
        ("claude-opus-4-8", "frontier", "claude-opus-4-8"),
        ("claude-sonnet-4-5", "balanced", "claude-sonnet-4-5"),
        ("claude-haiku-4-5", "cheap", "claude-haiku-4-5"),
    ],
    "openai-api": [
        ("gpt-5.4", "frontier", "gpt-5.4"),
        ("gpt-5.4-mini", "balanced", "gpt-5.4-mini"),
        ("gpt-4o-mini", "cheap", "gpt-4o-mini"),
    ],
    "gemini": [
        ("gemini-3.5-flash", "frontier", "gemini-3.5-flash"),
        ("gemini-flash-latest", "balanced", "gemini-flash-latest"),
        ("gemini-pro-latest", "balanced", "gemini-pro-latest"),
    ],
    "openrouter": [
        ("deepseek/deepseek-v4-flash", "cheap", "deepseek/deepseek-v4-flash"),
        ("minimax/minimax-m3", "balanced", "minimax/minimax-m3"),
        ("deepseek/deepseek-v4-pro", "balanced", "deepseek/deepseek-v4-pro"),
        ("moonshotai/kimi-k2.6", "balanced", "moonshotai/kimi-k2.6"),
        ("z-ai/glm-5.2", "frontier", "z-ai/glm-5.2"),
    ],
    "deepseek": [
        ("deepseek-chat", "balanced", "deepseek-chat"),
        ("deepseek-reasoner", "balanced", "deepseek-reasoner"),
    ],
    "zai": [
        ("glm-5.2", "balanced", "glm-5.2"),
        ("glm-4.7-flash", "cheap", "glm-4.7-flash"),
    ],
    "xai": [
        ("grok-4", "frontier", "grok-4"),
        ("grok-4-fast", "balanced", "grok-4-fast"),
    ],
    "bedrock": [
        ("us.anthropic.claude-sonnet-4-5-20250929-v1:0", "balanced",
         "us.anthropic.claude-sonnet-4-5-20250929-v1:0"),
        ("us.anthropic.claude-haiku-4-5-20251001-v1:0", "cheap",
         "us.anthropic.claude-haiku-4-5-20251001-v1:0"),
    ],
}


def _enabled_picker_models(provider_name: str) -> list[str]:
    """The user's enabled picker models for one provider (model ids without the
    'provider:' prefix). The Models UI is the user's curation surface -- when
    they toggled a set there, the agentic registry must mirror it exactly, not
    an arbitrary discovery sample."""
    try:
        from . import model_visibility as _mv
        prefix = f"{provider_name}:"
        return [s[len(prefix):] for s in _mv.get_enabled() if s.startswith(prefix)]
    except Exception as e:
        _diag("auto_registry.enabled_picker", e, msg=f"provider={provider_name}")
        return []


def _get_provider_models_from_discovery(provider_name: str, provider_key: str) -> list[tuple[str, str, str]]:
    """Model set for a provider, in priority order: the user's enabled picker
    models, then live discovery, then the curated fallback.

    Returns: list of (model_name, tier, slug) tuples.
    """
    try:
        from .providers import get_provider
        from .model_fetch import fetch_models
        
        provider = get_provider(provider_name)
        if not provider:
            return []

        def _tier_of_known(name: str) -> str:
            spec = _KNOWN_MODEL_SPECS.get(name)
            if spec and spec[0] >= 85:
                return "frontier"
            if spec and spec[0] < 70:
                return "cheap"
            return "balanced"

        # The user's explicit picker curation wins outright.
        enabled = _enabled_picker_models(provider_name)
        if enabled:
            return [(m, _tier_of_known(m), m) for m in enabled]

        # Try live discovery
        live_models = fetch_models(provider, provider_key, force=False)
        if not live_models:
            # No live models, use curated
            return _CURATED_MODELS.get(provider_name, [])
        
        # Classify each live model into a tier. Order matters: check the
        # frontier "opus/pro/ultra" markers BEFORE the cheap "flash/mini/lite"
        # markers so e.g. gemini-2.5-PRO is frontier/balanced, not lumped with
        # flash. "lite"/"nano" are always the cheapest. This keeps the router's
        # capability ordering correct (pro > flash > flash-lite).
        def _tier_of(name: str) -> str:
            n = name.lower()
            if any(x in n for x in ["lite", "nano", "haiku", "-8b", "flash-lite"]):
                return "cheap"
            if any(x in n for x in ["opus", "ultra", "-pro", "pro-", "pro"]):
                return "frontier"
            if any(x in n for x in ["flash", "mini", "fast", "gemma"]):
                return "cheap"
            return "balanced"

        # Curate rather than dump: skip clearly-superseded/dated snapshots and
        # older generations so a daily-driver registry stays small and current.
        def _keep(name: str) -> bool:
            n = name.lower()
            if any(x in n for x in ["gemini-2.0", "gemini-1", "gemma-3", "-preview",
                                     "-exp", "vision", "embedding", "tts", "image"]):
                return False
            # drop dated snapshot suffixes like -20250929 when an aliased
            # (undated) variant of the same family will also be present.
            return True

        result = []
        seen = set()
        for model_id in live_models:
            if not _keep(model_id):
                continue
            if model_id in seen:
                continue
            seen.add(model_id)
            result.append((model_id, _tier_of(model_id), model_id))
            if len(result) >= 6:  # a handful per provider is plenty
                break

        return result if result else _CURATED_MODELS.get(provider_name, [])
    except Exception as e:
        _diag("auto_registry.discovery", e, msg=f"provider={provider_name}")
        return _CURATED_MODELS.get(provider_name, [])


def _live_prices_enabled() -> bool:
    """HARNESS_LIVE_PRICES=0 disables OpenRouter price overlay (default on)."""
    return os.environ.get("HARNESS_LIVE_PRICES", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _overlay_live_prices(slug: str, input_price: float, output_price: float) -> tuple:
    """Best-effort live OpenRouter price overlay via pmharness.registry.

    Returns (input, output, applied) where applied is True when live rates
    replaced the static numbers. Never raises; any miss/exception keeps static.
    """
    if not _live_prices_enabled():
        return input_price, output_price, False
    try:
        from pmharness.registry import price as _or_price

        live = _or_price(slug)
        if (
            isinstance(live, (tuple, list))
            and len(live) >= 2
            and live[0] is not None
            and live[1] is not None
            and float(live[0]) > 0
            and float(live[1]) > 0
        ):
            return float(live[0]), float(live[1]), True
    except Exception as e:
        _diag("auto_registry.live_price", e, msg=f"slug={slug}")
    return input_price, output_price, False


# Module-level counter for one aggregate diag line per sync (not per-model spam).
_LIVE_PRICE_APPLIED = 0
_LIVE_PRICE_FALLBACK = 0


def _reset_live_price_stats() -> None:
    global _LIVE_PRICE_APPLIED, _LIVE_PRICE_FALLBACK
    _LIVE_PRICE_APPLIED = 0
    _LIVE_PRICE_FALLBACK = 0


def _build_agentic_spec(provider_name: str, model_name: str, tier: str, slug: str) -> dict:
    """Build a single agentic ModelSpec dict. Known models get their real
    benchmark-anchored numbers; unknown ones fall back to the tier template.

    After static resolution, best-effort overlays live OpenRouter prices from
    pmharness.registry (disk-cached, 6s fetch, never raises). Capability score,
    context window, and tags stay static. HARNESS_LIVE_PRICES=0 skips overlay.
    """
    global _LIVE_PRICE_APPLIED, _LIVE_PRICE_FALLBACK
    known = _KNOWN_MODEL_SPECS.get(slug)
    if known:
        capability_score, input_price, output_price, context_window, tags = known
    else:
        templates = _AGENTIC_TEMPLATES.get(provider_name, {})
        template = templates.get(tier)
        if not template:
            # Fallback to balanced if tier not found
            template = templates.get("balanced", (75, 1.0, 3.0, 100000, ["balanced"]))
        capability_score, input_price, output_price, context_window, tags = template

    input_price, output_price, applied = _overlay_live_prices(
        slug, input_price, output_price
    )
    if applied:
        _LIVE_PRICE_APPLIED += 1
    else:
        _LIVE_PRICE_FALLBACK += 1

    return {
        "id": f"agentic/{slug}",
        "adapter": "agentic",
        "adapter_model_name": model_name,
        "capability_score": capability_score,
        "input_per_mtok_usd": input_price,
        "output_per_mtok_usd": output_price,
        "context_window": context_window,
        "tags": list(tags),
        "payload_defaults": {"provider": provider_name},
        "billing": "api"
    }


def sync_agentic_registry(force: bool = False) -> dict:
    """Sync the agentic entries in ~/.puppetmaster/models.json based on provider keys.
    
    This function:
    1. Detects which provider keys are present (respects disconnected set)
    2. For each live provider, produces agentic ModelSpec dicts
    3. Writes ONLY the agentic entries, preserving non-agentic entries
    4. Is idempotent and safe to call repeatedly
    
    Args:
        force: If True, bypass caches and force fresh discovery
        
    Returns:
        dict with 'synced': bool, 'providers': list of synced providers, 
        'models_count': int, 'error': optional error message
    """
    try:
        from .providers import PROVIDERS
        from .keys import get_disconnected
        from .registry_wizard import get_models_file_path, write_json_atomic, get_provider_key
        
        # Get disconnected providers
        disconnected = get_disconnected()
        
        # Map provider names to their correct agentic provider identifier
        provider_map = {
            "anthropic": "anthropic",
            "openai": "openai-api",
            "gemini": "gemini",
            "openrouter": "openrouter",
            "deepseek": "deepseek",
            "zai": "zai",
            "xai": "xai",
            "bedrock": "bedrock",
        }
        
        # Detect live providers with keys
        live_providers = []
        for p in PROVIDERS:
            if p.name in disconnected:
                continue
            key = get_provider_key(p)
            if key:
                agentic_name = provider_map.get(p.name, p.name)
                live_providers.append((p.name, agentic_name, key))
        
        # Build agentic specs for each live provider
        new_agentic_specs = []
        synced_providers = []
        _reset_live_price_stats()
        for provider_name, agentic_name, key in live_providers:
            models = _get_provider_models_from_discovery(provider_name, key)
            if not models:
                # Even with no discovery, use curated fallback
                models = _CURATED_MODELS.get(agentic_name, [])
            
            if models:
                synced_providers.append(agentic_name)
                # Add each model as an agentic spec
                for model_name, tier, slug in models:
                    spec = _build_agentic_spec(agentic_name, model_name, tier, slug)
                    new_agentic_specs.append(spec)

        _diag(
            "auto_registry.live_prices",
            msg=(
                f"live={_LIVE_PRICE_APPLIED} static_fallback={_LIVE_PRICE_FALLBACK} "
                f"enabled={int(_live_prices_enabled())}"
            ),
        )        
        # Read existing models.json
        models_path = get_models_file_path()
        existing_models = {"models": []}
        if os.path.exists(models_path):
            try:
                with open(models_path, 'r', encoding="utf-8", errors="replace") as f:
                    existing_models = json.load(f)
            except Exception as e:
                _diag("auto_registry.read_existing", e)
        
        # Preserve non-agentic entries
        non_agentic = []
        if isinstance(existing_models.get("models"), list):
            for model in existing_models["models"]:
                if not isinstance(model, dict):
                    continue
                # Keep anything that's not an agentic adapter
                if model.get("adapter") != "agentic":
                    non_agentic.append(model)
        
        # Merge: non-agentic entries first, then new agentic specs
        final_models = non_agentic + new_agentic_specs
        
        # Write atomically
        write_json_atomic(models_path, {"models": final_models})
        
        return {
            "synced": True,
            "providers": synced_providers,
            "models_count": len(new_agentic_specs),
        }
        
    except Exception as e:
        _diag("auto_registry.sync", e)
        return {
            "synced": False,
            "providers": [],
            "models_count": 0,
            "error": str(e)
        }


def sync_agentic_registry_safe() -> None:
    """Wrapper for sync_agentic_registry that never raises.
    
    Safe to call at startup or in key-change hooks -- any error is logged
    via diagnostics but never blocks the calling code.
    """
    try:
        result = sync_agentic_registry()
        if result.get("synced"):
            _diag("auto_registry.sync_ok", 
                  msg=f"synced {result['models_count']} models from {', '.join(result['providers']) or 'none'}")
        else:
            _diag("auto_registry.sync_failed", 
                  msg=f"error: {result.get('error', 'unknown')}")
    except Exception as e:
        _diag("auto_registry.sync_safe", e)
