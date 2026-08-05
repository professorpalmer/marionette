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
import re
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
    # OpenCode Go is subscription-billed; keep nominal rates for router rank.
    "opencode-go": {
        "frontier": (90, 1.0, 3.0, 200000, ["frontier", "reasoning", "code"]),
        "balanced": (80, 0.5, 1.5, 200000, ["balanced", "fast", "code"]),
        "cheap": (70, 0.1, 0.3, 200000, ["cheap", "fast", "code"]),
    },
    # ChatGPT Codex OAuth (plan); nominal rates for capability ranking only.
    "openai-codex": {
        "frontier": (92, 2.5, 10.0, 200000, ["frontier", "reasoning", "code"]),
        "balanced": (85, 1.25, 5.0, 200000, ["balanced", "fast", "code"]),
        "cheap": (72, 0.3, 1.2, 128000, ["cheap", "fast", "code"]),
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
    "moonshotai/kimi-k3": (98, 3.0, 15.0, 1000000, ["frontier", "code", "vision", "agent-loop"]),
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
        ("moonshotai/kimi-k3", "frontier", "moonshotai/kimi-k3"),
        ("z-ai/glm-5.2", "balanced", "z-ai/glm-5.2"),
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
        ("amazon.nova-lite-v1:0", "balanced", "amazon.nova-lite-v1:0"),
        ("amazon.nova-micro-v1:0", "cheap", "amazon.nova-micro-v1:0"),
        ("deepseek.v3.2", "balanced", "deepseek.v3.2"),
        ("zai.glm-4.7-flash", "cheap", "zai.glm-4.7-flash"),
        ("zai.glm-5", "frontier", "zai.glm-5"),
        ("moonshotai.kimi-k2.5", "balanced", "moonshotai.kimi-k2.5"),
        ("qwen.qwen3-coder-30b-a3b-v1:0", "balanced", "qwen.qwen3-coder-30b-a3b-v1:0"),
        ("minimax.minimax-m2.5", "balanced", "minimax.minimax-m2.5"),
        ("meta.llama3-1-8b-instruct-v1:0", "cheap", "meta.llama3-1-8b-instruct-v1:0"),
        ("mistral.mistral-7b-instruct-v0:2", "cheap", "mistral.mistral-7b-instruct-v0:2"),
    ],
    # Populated below from harness.opencode_go.CURATED_MODELS so a Go-only
    # auth setup still has agentic worker rows when live /models is unreachable.
    "opencode-go": [],
    # Populated below from Provider.pilot_models for openai-codex.
    "openai-codex": [],
}


def _opencode_go_curated() -> list[tuple[str, str, str]]:
    """Curated OpenCode Go rows as (model_name, tier, slug) for agentic sync."""
    try:
        from .opencode_go import CURATED_MODELS
    except Exception as e:
        _diag("auto_registry.opencode_go_curated", e)
        return []
    out: list[tuple[str, str, str]] = []
    for name in CURATED_MODELS:
        bare = str(name or "").strip()
        if not bare:
            continue
        n = bare.lower()
        if any(tok in n for tok in ("kimi-k3", "grok-4.5", "gpt-5.6-sol", "glm-5.2")):
            tier = "frontier"
        elif "flash" in n or n.endswith("-plus") or n in ("hy3", "mimo-v2.5"):
            tier = "cheap"
        else:
            tier = "balanced"
        out.append((bare, tier, bare))
    return out


def _openai_codex_curated() -> list[tuple[str, str, str]]:
    """Curated ChatGPT Codex OAuth rows from the pilot catalog.

    Slug is namespaced (``openai-codex/<model>``) so registry ids do not collide
    with OpenCode Go flat ``agentic/gpt-5.6-luna`` rows. Wire model name stays
    the bare Codex id via ``adapter_model_name``.
    """
    try:
        from .providers import get_provider

        provider = get_provider("openai-codex")
        models = list(getattr(provider, "pilot_models", ()) or ()) if provider else []
    except Exception as e:
        _diag("auto_registry.openai_codex_curated", e)
        models = []
    if not models:
        models = (
            "gpt-5.6-sol",
            "gpt-5.6-terra",
            "gpt-5.6-luna",
            "gpt-5.5",
            "gpt-5.4",
            "gpt-5.4-mini",
            "gpt-5.3-codex",
        )
    out: list[tuple[str, str, str]] = []
    for name in models:
        bare = str(name or "").strip()
        if not bare:
            continue
        n = bare.lower()
        if any(tok in n for tok in ("sol", "opus", "o3")):
            tier = "frontier"
        elif any(tok in n for tok in ("mini", "nano", "spark")):
            tier = "cheap"
        else:
            tier = "balanced"
        # (model_name for wire, tier, slug for registry id)
        out.append((bare, tier, f"openai-codex/{bare}"))
    return out


# Bind at import so sync/discovery see curated without a second lookup path.
_CURATED_MODELS["opencode-go"] = _opencode_go_curated()
_CURATED_MODELS["openai-codex"] = _openai_codex_curated()


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

        curated = _CURATED_MODELS.get(provider_name, [])

        # The user's explicit picker curation is authoritative for the
        # discretionary rows.  Marionette's small OpenRouter ladder is the
        # exception: retain those required fallbacks so a picker or discovery
        # refresh cannot strand an otherwise keyed provider.
        enabled = _enabled_picker_models(provider_name)
        if enabled:
            def _slug_for(name: str) -> str:
                if provider_name == "openai-codex" and "/" not in name:
                    return f"openai-codex/{name}"
                return name

            selected = [(m, _tier_of_known(m), _slug_for(m)) for m in enabled]
            if provider_name == "openrouter":
                selected_slugs = {item[2] for item in selected}
                selected.extend(
                    item for item in curated if item[2] not in selected_slugs
                )
            return selected

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
                                     "-exp", "vision", "embedding", "tts", "image",
                                     "upscale", "stability.", "twelvelabs", "pegasus"]):
                return False
            # Bedrock context-window variants (…:24k / …:256k) duplicate the base id.
            if provider_name == "bedrock" and re.search(r":\d+k$", n):
                return False
            return True

        def _bedrock_family(model_id: str) -> str:
            """amazon.nova-… / us.anthropic.claude-… -> provider family key."""
            parts = model_id.split(".")
            if len(parts) >= 3 and parts[0] in (
                "us", "eu", "ap", "global", "jp", "au", "ca", "me", "sa", "af",
            ):
                return parts[1]
            return parts[0] if parts else model_id

        result = []
        seen = set()
        if provider_name == "bedrock":
            # Visit every Bedrock family (Claude is not the only option). Cap
            # per family so DeepSeek/ZAI/Moonshot/Qwen are not starved.
            by_family: dict[str, list[str]] = {}
            for model_id in live_models:
                if not _keep(model_id):
                    continue
                fam = _bedrock_family(model_id)
                by_family.setdefault(fam, []).append(model_id)
            prefer_prefix = ("us", "eu", "ap", "global")

            def _rank(mid: str) -> tuple:
                head = mid.split(".", 1)[0]
                return (0 if head in prefer_prefix else 1, mid)

            priority = [
                "anthropic", "amazon", "deepseek", "zai", "moonshotai",
                "moonshot", "qwen", "meta", "mistral", "minimax", "openai",
                "cohere", "ai21",
            ]
            fam_order = [f for f in priority if f in by_family] + sorted(
                f for f in by_family if f not in priority
            )
            max_per_family = 3
            max_total = 36
            for fam in fam_order:
                for model_id in sorted(by_family[fam], key=_rank)[:max_per_family]:
                    if model_id in seen:
                        continue
                    seen.add(model_id)
                    result.append((model_id, _tier_of(model_id), model_id))
                    if len(result) >= max_total:
                        break
                if len(result) >= max_total:
                    break
        else:
            for model_id in live_models:
                if not _keep(model_id):
                    continue
                if model_id in seen:
                    continue
                seen.add(model_id)
                slug = (
                    f"openai-codex/{model_id}"
                    if provider_name == "openai-codex" and "/" not in model_id
                    else model_id
                )
                result.append((model_id, _tier_of(model_id), slug))
                if len(result) >= 6:  # a handful per provider is plenty
                    break

        if provider_name == "openrouter":
            # Discovery is additive.  It may contribute useful current models,
            # but must never evict the deterministic K3/DeepSeek ladder.
            seen_slugs = {item[2] for item in result}
            result.extend(item for item in curated if item[2] not in seen_slugs)
        elif provider_name == "openai-codex":
            # Keep curated Codex ladder even when live discovery returns a
            # partial list (or a different naming wave).
            seen_slugs = {item[2] for item in result}
            result.extend(item for item in curated if item[2] not in seen_slugs)
        return result if result else curated
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

    # Puppetmaster's router soft-requires `tools` for agentic tool-loop roles
    # (audit/explore/implement). Without it, every agentic model is rejected and
    # routing falls through to $0-marginal plan adapters — so measured savings
    # stay at $0 even when OpenRouter is ready.
    tag_list = list(tags)
    for required in ("tools", "agentic"):
        if required not in tag_list:
            tag_list.append(required)

    return {
        "id": f"agentic/{slug}",
        "adapter": "agentic",
        "adapter_model_name": model_name,
        "capability_score": capability_score,
        "input_per_mtok_usd": input_price,
        "output_per_mtok_usd": output_price,
        "context_window": context_window,
        "tags": tag_list,
        "payload_defaults": {"provider": provider_name},
        # Plan OAuth / subscription providers: $0 marginal; nominal rates above
        # are for router ranking only. Cash API keys stay billing=api.
        "billing": (
            "plan" if provider_name in ("openai-codex", "opencode-go") else "api"
        ),
    }


# harness provider name -> agentic/puppetmaster provider slug. These are
# standalone HTTP targets for the agentic adapter (pilots AND workers).
# cursor-cli stays out for now (Agent CLI wire; wave-2 worker path).
_AGENTIC_PROVIDER_SLUGS = {
    "anthropic": "anthropic",
    "openai": "openai-api",
    "gemini": "gemini",
    "openrouter": "openrouter",
    "deepseek": "deepseek",
    "zai": "zai",
    "xai": "xai",
    "bedrock": "bedrock",
    "opencode-go": "opencode-go",
    "openai-codex": "openai-codex",
}


def keyed_agentic_providers() -> set:
    """Agentic provider slugs that currently hold a usable, connected key."""
    try:
        from .providers import PROVIDERS
        from .keys import get_disconnected
        from .registry_wizard import get_provider_key
    except Exception as e:
        _diag("auto_registry.keyed_providers_import", e)
        return set()
    disconnected = get_disconnected()
    keyed = set()
    for p in PROVIDERS:
        slug = _AGENTIC_PROVIDER_SLUGS.get(p.name)
        if not slug or p.name in disconnected:
            continue
        try:
            if get_provider_key(p):
                keyed.add(slug)
        except Exception as e:
            _diag("auto_registry.keyed_providers", e, msg=f"provider={p.name}")
    return keyed


def _agentic_row_provider(row: dict) -> str:
    defaults = row.get("payload_defaults")
    if isinstance(defaults, dict):
        return str(defaults.get("provider") or "")
    return ""


def prune_unavailable_agentic_rows(keep_providers: set) -> dict:
    """Drop agentic rows whose provider has no usable key. Preserves peers.

    A restarted backend can inherit agentic rows written when a different
    provider was keyed (or by another tool sharing the registry). Those rows
    still carry high capability scores, so the router first-picks a model that
    is guaranteed to 401 — and a keyed OpenRouter ladder never gets a turn.
    Non-agentic rows (plan/cursor/codex peers) are never touched.
    """
    report = {"pruned": 0, "kept": 0, "path": ""}
    try:
        from .registry_wizard import get_models_file_path, write_json_atomic

        path = get_models_file_path()
        report["path"] = path
        if not os.path.exists(path):
            return report
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            data = json.load(f)
        models = data.get("models") if isinstance(data, dict) else None
        if not isinstance(models, list):
            return report
        kept = []
        pruned = 0
        for row in models:
            if not isinstance(row, dict):
                continue
            if row.get("adapter") != "agentic":
                kept.append(row)
                continue
            if _agentic_row_provider(row) in keep_providers:
                kept.append(row)
            else:
                pruned += 1
        report["kept"] = len(kept)
        if pruned:
            data["models"] = kept
            write_json_atomic(path, data)
            report["pruned"] = pruned
    except Exception as e:
        _diag("auto_registry.prune_agentic", e)
    return report


def _openrouter_ladder_ids() -> list:
    """Marionette ladder ids that the OpenRouter catalog is expected to supply."""
    try:
        from .marionette_registry import _LADDER
    except Exception:
        return []
    curated = {f"agentic/{slug}" for _n, _t, slug in _CURATED_MODELS.get("openrouter", [])}
    return [mid for mid, _score, _tags in _LADDER if mid in curated]


def _agentic_ids_present() -> set:
    try:
        from .registry_wizard import get_models_file_path

        path = get_models_file_path()
        if not os.path.exists(path):
            return set()
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            data = json.load(f)
        models = data.get("models") if isinstance(data, dict) else None
        if not isinstance(models, list):
            return set()
        return {
            str(row.get("id") or "")
            for row in models
            if isinstance(row, dict) and row.get("adapter") == "agentic" and row.get("id")
        }
    except Exception as e:
        _diag("auto_registry.agentic_ids", e)
        return set()


def _seed_openrouter_ladder_rows(missing: list) -> int:
    """Append the curated OpenRouter ladder rows the registry is missing."""
    try:
        from .registry_wizard import get_models_file_path, write_json_atomic

        curated_by_id = {
            f"agentic/{slug}": (name, tier, slug)
            for name, tier, slug in _CURATED_MODELS.get("openrouter", [])
        }
        specs = [
            _build_agentic_spec("openrouter", *curated_by_id[mid])
            for mid in missing
            if mid in curated_by_id
        ]
        if not specs:
            return 0
        path = get_models_file_path()
        data = {"models": []}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get("models"), list):
            data = {"models": []}
        data["models"] = list(data["models"]) + specs
        write_json_atomic(path, data)
        return len(specs)
    except Exception as e:
        _diag("auto_registry.seed_openrouter_ladder", e)
        return 0


def ensure_keyed_provider_registry_health() -> dict:
    """Boot gate: agentic rows must match live keys BEFORE route/swarm dispatch.

    Called right after keys load (and again before ``serve_forever``) so a fresh
    or restarted backend with, say, only OpenRouter keyed cannot dispatch against
    a catalog full of rows for providers it has no credential for. Fail-closed:
    with no keyed provider every agentic row is pruned and ``ready`` is False, so
    routing refuses instead of 401-ing mid-swarm.

    Returns a small report; never raises.
    """
    report = {
        "ready": False,
        "providers": [],
        "pruned": 0,
        "seeded_ladder": [],
        "missing_ladder": [],
        "reason": "",
    }
    try:
        keyed = keyed_agentic_providers()
        report["providers"] = sorted(keyed)
        if not keyed:
            report["pruned"] = prune_unavailable_agentic_rows(set()).get("pruned", 0)
            report["reason"] = "no keyed agentic provider"
            _diag("auto_registry.health_fail_closed",
                  msg=f"pruned={report['pruned']}")
            return report
        sync_agentic_registry_safe()
        report["pruned"] = prune_unavailable_agentic_rows(keyed).get("pruned", 0)
        if "openrouter" in keyed:
            required = _openrouter_ladder_ids()
            present = _agentic_ids_present()
            missing = [mid for mid in required if mid not in present]
            # Every ladder row missing means auto_route has nothing Marionette
            # curated to pick; re-seed from the curated set rather than routing
            # blind. A partial gap is normal (picker curation) and left alone.
            if required and len(missing) == len(required):
                if _seed_openrouter_ladder_rows(missing):
                    report["seeded_ladder"] = list(missing)
                    present = _agentic_ids_present()
                    missing = [mid for mid in required if mid not in present]
            report["missing_ladder"] = missing
        if report["pruned"] or report["seeded_ladder"]:
            # Pruning/seeding rewrote the catalog; scores must be re-stamped.
            try:
                from .marionette_registry import apply_marionette_router_ladder

                apply_marionette_router_ladder()
            except Exception as e:
                _diag("auto_registry.health_ladder", e)
        report["ready"] = bool(_agentic_ids_present())
        if not report["ready"]:
            report["reason"] = "no agentic rows for keyed providers"
        _diag(
            "auto_registry.health",
            msg=(
                f"ready={int(report['ready'])} providers={','.join(report['providers'])} "
                f"pruned={report['pruned']} missing_ladder={len(report['missing_ladder'])}"
            ),
        )
    except Exception as e:
        _diag("auto_registry.health", e)
        report["reason"] = str(e)
    return report


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
        
        provider_map = _AGENTIC_PROVIDER_SLUGS

        # Detect live providers with usable keys (get_provider_key rejects
        # disconnects and doctor/test/placeholder tokens).
        # Only providers in provider_map are agentic HTTP targets. cursor-cli
        # stays out (wave-2 Agent CLI worker path); openai-codex is included
        # so Codex-only installs can run swarms on the same OAuth token.
        live_providers = []
        for p in PROVIDERS:
            if p.name in disconnected:
                continue
            if p.name not in provider_map:
                continue
            key = get_provider_key(p)
            if not key:
                continue
            agentic_name = provider_map[p.name]
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
    """Sync agentic rows, restore shared non-agentic peers, re-apply ladder.

    Safe to call at startup or in key-change hooks -- any error is logged
    via diagnostics but never blocks the calling code.

    Ladder + reconcile run *after* sync on every call site so a key paste or
    cold boot cannot leave cursor-cli stamped as agentic, wipe plan peers, or
    clobber Marionette capability scores (which made every tool-loop role fall
    through to $0-marginal plan routing and empty SESSION COST savings).
    openai-codex is a legitimate agentic provider (plan-billed Responses).
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
        return
    try:
        from .marionette_registry import (
            apply_marionette_router_ladder,
            reconcile_shared_models,
        )
        reconcile_shared_models()
        apply_marionette_router_ladder()
    except Exception as e:
        _diag("auto_registry.post_sync_ladder", e)
