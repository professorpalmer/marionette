from __future__ import annotations

"""Auto-registry: build and refresh the agentic model registry automatically.

The swarm uses HARNESS_SWARM_ADAPTER=agentic, which routes via Puppetmaster's
key-aware router over the 'agentic' entries in ~/.puppetmaster/models.json.
This module automatically syncs those entries from whatever provider API keys
the user has, without requiring any hand-editing of models.json.

Users never have to remember/reset/curate models.json -- it stays fresh based
on their connected provider keys. A background refresh force-fetches the live
catalog shortly after boot and every six hours so dated OpenRouter snapshots
(e.g. ``deepseek/deepseek-v4-pro-0813``) land without a restart. Set
``HARNESS_REGISTRY_AUTO_REFRESH=0`` to disable.
"""

import json
import os
import re
import threading
import time
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
    "opencode-zen": {
        "frontier": (90, 1.0, 3.0, 200000, ["frontier", "reasoning", "code"]),
        "balanced": (80, 0.5, 1.5, 200000, ["balanced", "fast", "code"]),
        "cheap": (70, 0.0, 0.0, 200000, ["cheap", "fast", "code"]),
    },
    # ChatGPT Codex OAuth (plan); nominal rates for capability ranking only.
    "openai-codex": {
        "frontier": (92, 2.5, 10.0, 200000, ["frontier", "reasoning", "code"]),
        "balanced": (85, 1.25, 5.0, 200000, ["balanced", "fast", "code"]),
        "cheap": (72, 0.3, 1.2, 128000, ["cheap", "fast", "code"]),
    },
    "nous": {
        "frontier": (88, 1.0, 3.0, 128000, ["frontier", "reasoning", "code"]),
        "balanced": (80, 0.5, 1.5, 128000, ["balanced", "code"]),
        "cheap": (70, 0.1, 0.3, 128000, ["cheap", "fast", "code"]),
    },
    "minimax": {
        "frontier": (86, 1.0, 4.0, 200000, ["frontier", "code", "reasoning"]),
        "balanced": (78, 0.3, 1.2, 200000, ["balanced", "code"]),
        "cheap": (70, 0.1, 0.4, 200000, ["cheap", "fast", "code"]),
    },
    "nvidia": {
        "frontier": (84, 0.5, 1.5, 128000, ["frontier", "code"]),
        "balanced": (76, 0.2, 0.6, 128000, ["balanced", "code"]),
        "cheap": (68, 0.05, 0.2, 128000, ["cheap", "fast", "code"]),
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
    "z-ai/glm-5.3": (86, 1.0, 3.5, 1000000, ["quality", "code", "reasoning", "long-context"]),
    "z-ai/glm-5.2": (86, 1.0, 3.5, 1000000, ["quality", "code", "reasoning", "long-context"]),
    "glm-5.3": (86, 1.0, 3.5, 1000000, ["quality", "code", "reasoning", "long-context"]),
    "anthropic/claude-opus-4.8": (99, 5.0, 25.0, 1000000, ["frontier", "reasoning", "code", "vision", "long-context"]),
    # Codex pair: Sol must outrank Luna or live price overlay leaves Sol
    # strictly dominated at the shared balanced-85 template.
    "gpt-5.6-luna": (76, 1.25, 5.0, 200000, ["balanced", "fast", "code", "vision"]),
    "openai-codex/gpt-5.6-luna": (76, 1.25, 5.0, 200000, ["balanced", "fast", "code", "vision"]),
    "gpt-5.6-sol": (85, 2.5, 10.0, 200000, ["frontier", "reasoning", "code", "vision"]),
    "openai-codex/gpt-5.6-sol": (85, 2.5, 10.0, 200000, ["frontier", "reasoning", "code", "vision"]),
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
        ("z-ai/glm-5.3", "frontier", "z-ai/glm-5.3"),
        ("z-ai/glm-5.2", "balanced", "z-ai/glm-5.2"),
    ],
    "deepseek": [
        ("deepseek-chat", "balanced", "deepseek-chat"),
        ("deepseek-reasoner", "balanced", "deepseek-reasoner"),
    ],
    "zai": [
        ("glm-5.3", "frontier", "glm-5.3"),
        ("glm-5.2", "balanced", "glm-5.2"),
        ("glm-4.7-flash", "cheap", "glm-4.7-flash"),
    ],
    "xai": [
        ("grok-4", "frontier", "grok-4"),
        ("grok-4-fast", "balanced", "grok-4-fast"),
    ],
    "nous": [
        ("Hermes-4-70B", "frontier", "Hermes-4-70B"),
        ("Hermes-3-Llama-3.1-70B", "balanced", "Hermes-3-Llama-3.1-70B"),
    ],
    "minimax": [
        ("MiniMax-M3", "frontier", "MiniMax-M3"),
        ("MiniMax-M2.7", "balanced", "MiniMax-M2.7"),
    ],
    "nvidia": [
        ("qwen/qwen3-coder-480b", "frontier", "qwen/qwen3-coder-480b"),
        ("deepseek-ai/deepseek-v3.1", "balanced", "deepseek-ai/deepseek-v3.1"),
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
    "opencode-zen": [],
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
        if any(tok in n for tok in ("kimi-k3", "grok-4.6", "grok-4.5", "gpt-5.6-sol", "glm-5.3", "glm-5.2")):
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


def _opencode_zen_curated() -> list[tuple[str, str, str]]:
    """Verified Zen free-model rows. Paid models come from live listing only."""
    try:
        from .opencode_zen import CURATED_FREE_MODELS
    except Exception as e:
        _diag("auto_registry.opencode_zen_curated", e)
        return []
    out: list[tuple[str, str, str]] = []
    for name in CURATED_FREE_MODELS:
        bare = str(name or "").strip()
        if not bare:
            continue
        n = bare.lower()
        tier = "frontier" if n == "x-preview-f-free" else "cheap"
        out.append((bare, tier, bare))
    return out


# Bind at import so sync/discovery see curated without a second lookup path.
_CURATED_MODELS["opencode-go"] = _opencode_go_curated()
_CURATED_MODELS["opencode-zen"] = _opencode_zen_curated()
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


_DATED_SNAPSHOT_SUFFIX = re.compile(r"-\d{4,8}$")
# Dated/noisy preview snapshots only — ``x-preview-f-free`` must survive.
_DATED_PREVIEW_SNAPSHOT = re.compile(
    r"-(?:preview|exp)-\d{2,8}(?:[-_]\d{2,8})*(?:[_-]|$)", re.I,
)


def is_dated_or_noisy_preview(name: str) -> bool:
    """True for dated preview/exp snapshots, not every id containing '-preview'."""
    return bool(_DATED_PREVIEW_SNAPSHOT.search(str(name or "")))


def _strip_dated_suffix(name: str) -> str:
    """``deepseek/deepseek-v4-pro-0813`` -> ``deepseek/deepseek-v4-pro``."""
    n = (name or "").strip()
    match = _DATED_SNAPSHOT_SUFFIX.search(n)
    return n[: match.start()] if match else ""


def _newest_dated_snapshot(base: str, live_models: list[str]) -> str:
    """Prefer the newest dated sibling of *base* from a live catalog.

    Rolling ``deepseek/deepseek-v4-pro`` plus live ``…-0813`` returns the
    dated wire id. Exact match is the fallback when no dated sibling exists.
    """
    base_l = (base or "").strip().lower()
    if not base_l:
        return base
    best = ""
    best_key = (-1, "")
    for mid in live_models:
        n = (mid or "").strip()
        if not n:
            continue
        nl = n.lower()
        if nl == base_l:
            if best_key < (0, ""):
                best = n
                best_key = (0, "")
            continue
        prefix = base_l + "-"
        if not nl.startswith(prefix):
            continue
        suffix = nl[len(prefix):]
        if not re.fullmatch(r"\d{4,8}", suffix):
            continue
        key = (1, suffix)
        if key > best_key:
            best = n
            best_key = key
    return best or base


def _is_dated_or_exact_sibling(candidate: str, bases: list[str]) -> bool:
    cl = (candidate or "").strip().lower()
    if not cl:
        return False
    for base in bases:
        bl = (base or "").strip().lower()
        if not bl:
            continue
        if cl == bl:
            return True
        prefix = bl + "-"
        if cl.startswith(prefix) and re.fullmatch(r"\d{4,8}", cl[len(prefix):]):
            return True
    return False


def _promote_openrouter_snapshots(
    selected: list[tuple[str, str, str]],
    live_models: list[str],
) -> list[tuple[str, str, str]]:
    """Rewrite wire names to the newest dated sibling; keep stable slugs."""
    if not live_models:
        return selected
    promoted: list[tuple[str, str, str]] = []
    for model_name, tier, slug in selected:
        wire = _newest_dated_snapshot(model_name, live_models)
        if wire == model_name and slug != model_name:
            wire = _newest_dated_snapshot(slug, live_models)
        promoted.append((wire, tier, slug))
    return promoted


def _in_live_catalog(name: str, slug: str, live_models: list[str]) -> bool:
    """True when *name* or *slug* is in the live listing, including dated siblings."""
    if not live_models:
        return False
    live = [str(m or "").strip() for m in live_models if str(m or "").strip()]
    live_set = {m.lower() for m in live}
    for key in (name, slug):
        raw = str(key or "").strip()
        if not raw:
            continue
        if raw.lower() in live_set:
            return True
        promoted = _newest_dated_snapshot(raw, live)
        if promoted.lower() in live_set:
            return True
        family = _strip_dated_suffix(raw)
        if family and family.lower() in live_set:
            return True
    return False


def _known_spec_for(model_name: str, slug: str):
    """Static economics for a wire id, rolling slug, dated sibling, or
    a newer dotted/hyphen family bump (``glm-5.3`` inherits ``glm-5.2``)."""
    for key in (model_name, slug):
        spec = _KNOWN_MODEL_SPECS.get(key)
        if spec:
            return spec
    for key in (model_name, slug):
        family = _strip_dated_suffix(key)
        if family:
            spec = _KNOWN_MODEL_SPECS.get(family)
            if spec:
                return spec
    try:
        from .model_visibility import inherit_family_spec
        return inherit_family_spec(model_name, slug, _KNOWN_MODEL_SPECS)
    except Exception:
        return None


def _with_family_promotions(
    result: list,
    curated: list,
    live_models: list,
    tier_of,
    *,
    slug_fn=None,
) -> list:
    """Append live ids that are a newer X.Y of a curated or already-selected family."""
    try:
        from .model_visibility import promote_newer_family_versions
    except Exception:
        return list(result)
    known = []
    seen = set()
    for name, _tier, slug in list(result) + list(curated):
        known.append(name)
        known.append(slug)
        seen.add(name)
        seen.add(slug)
    extra = []
    for newer in promote_newer_family_versions(known, live_models):
        if newer in seen:
            continue
        seen.add(newer)
        slug = slug_fn(newer) if slug_fn else newer
        extra.append((newer, tier_of(newer), slug))
    return list(result) + extra


def _get_provider_models_from_discovery(
    provider_name: str,
    provider_key: str,
    force: bool = False,
) -> list[tuple[str, str, str]]:
    """Model set for a provider, in priority order: the user's enabled picker
    models, then live discovery, then the curated fallback.

    Returns: list of (model_name, tier, slug) tuples. For OpenRouter, *slug*
    stays the rolling family id so the Marionette ladder is stable; *model_name*
    is the newest dated live snapshot when one exists.
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

        # Models toggles are the only discretionary allowlist. Do not union
        # curated/live extras onto an explicit picker set — that is how
        # MiniMax / Xiaomi MIMO leaked into Autopilot when they were off.
        enabled = _enabled_picker_models(provider_name)
        if enabled:
            def _slug_for(name: str) -> str:
                if provider_name == "openai-codex" and "/" not in name:
                    return f"openai-codex/{name}"
                return name

            # Settings toggles are the worker allowlist. Keep every enabled
            # id even when this live fetch omitted it; do not add extras.
            selected = [(m, _tier_of_known(m), _slug_for(m)) for m in enabled]
            live_models = fetch_models(provider, provider_key, force=force)
            if live_models and provider_name == "openrouter":
                selected = _promote_openrouter_snapshots(selected, live_models)
            return selected

        # Per-provider only: another provider's Models toggles must not empty
        # this keyed Full stack catalog. Fall through to curated ∩ live.

        # Try live discovery
        live_models = fetch_models(provider, provider_key, force=force)
        if not live_models:
            return list(curated)
        
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

        # Curate rather than dump: skip clearly-superseded/older generations
        # so a daily-driver registry stays small. Dated family snapshots
        # (...-0813) are promoted onto curated OpenRouter rows, not dumped.
        def _keep(name: str) -> bool:
            n = name.lower()
            if is_dated_or_noisy_preview(n):
                return False
            if any(x in n for x in ["gemini-2.0", "gemini-1", "gemma-3",
                                     "vision", "embedding", "tts", "image",
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
            # Curated ladder intersect live catalog. Dated siblings promote
            # onto stable slugs. Newer family versions of those curated
            # rows (glm-5.2 → glm-5.3) come in from live. Do not dump the
            # first N live extras — that is how MIMO entered Autopilot.
            curated_promoted = _promote_openrouter_snapshots(
                list(curated), live_models,
            )
            result = [
                item for item in curated_promoted
                if _in_live_catalog(item[0], item[2], live_models)
            ]
            result = _with_family_promotions(result, curated, live_models, _tier_of)
            return result
        elif provider_name == "openai-codex":
            # Keep curated Codex ladder even when live discovery returns a
            # partial list (or a different naming wave).
            seen_slugs = {item[2] for item in result}
            result.extend(item for item in curated if item[2] not in seen_slugs)
        elif provider_name == "opencode-go":
            # Live listing is authoritative: do not keep MIMO (or anything
            # else) that the Go workspace does not actually serve. After a
            # successful live fetch, return curated∩live/promoted even when
            # empty — never fall through to stale curated rows.
            result = [
                item for item in curated
                if _in_live_catalog(item[0], item[2], live_models)
            ]
            return _with_family_promotions(result, curated, live_models, _tier_of)
        elif provider_name == "opencode-zen":
            # Live is availability authority. Curated free rows that are
            # actually served stay; paid models.dev ids never join. Newly
            # discovered live ids appear in Models Settings, not Autopilot.
            # Same as Go: an empty intersection is the answer, not curated.
            result = [
                item for item in curated
                if _in_live_catalog(item[0], item[2], live_models)
            ]
            return _with_family_promotions(result, curated, live_models, _tier_of)
        else:
            result = _with_family_promotions(
                result, curated, live_models, _tier_of,
            )
            if provider_name != "bedrock":
                # Curated seeds that /models has not listed yet (Coding Plan
                # serving glm-5.3 while the listing still stops at 5.2).
                seen_ids = {item[0] for item in result} | {item[2] for item in result}
                result.extend(
                    item for item in curated
                    if item[0] not in seen_ids and item[2] not in seen_ids
                )
        return result if result else list(curated)
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


def _zai_coding_plan_billing() -> bool:
    """GLM Coding Plan is subscription credits — $0 marginal like OpenCode Go."""
    try:
        from .providers import zai_uses_coding_plan
        return bool(zai_uses_coding_plan())
    except Exception:
        return True


def _build_agentic_spec(provider_name: str, model_name: str, tier: str, slug: str) -> dict:
    """Build a single agentic ModelSpec dict. Known models get their real
    benchmark-anchored numbers; unknown ones fall back to the tier template.

    After static resolution, best-effort overlays live OpenRouter prices from
    pmharness.registry (disk-cached, 6s fetch, never raises). Capability score,
    context window, and tags stay static. HARNESS_LIVE_PRICES=0 skips overlay.
    """
    global _LIVE_PRICE_APPLIED, _LIVE_PRICE_FALLBACK
    known = _known_spec_for(model_name, slug)
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
        model_name, input_price, output_price
    )
    if not applied and model_name != slug:
        input_price, output_price, applied = _overlay_live_prices(
            slug, input_price, output_price
        )
    if applied:
        _LIVE_PRICE_APPLIED += 1
    else:
        _LIVE_PRICE_FALLBACK += 1

    # Verified Zen free ids: $0 measured rates even at frontier/balanced tiers.
    # Capability score and billing=api stay honest — do not downgrade tier.
    if provider_name == "opencode-zen":
        try:
            from .opencode_zen import is_curated_free_model

            if is_curated_free_model(model_name) or is_curated_free_model(slug):
                input_price = 0.0
                output_price = 0.0
        except Exception as e:
            _diag("auto_registry.zen_free_price", e, msg=f"model={model_name}")

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
            "plan"
            if provider_name in ("openai-codex", "opencode-go")
            or (provider_name == "zai" and _zai_coding_plan_billing())
            else "api"
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
    "opencode-zen": "opencode-zen",
    "openai-codex": "openai-codex",
    "nous": "nous",
    "minimax": "minimax",
    "nvidia": "nvidia",
    # cursor-cli stays out: agent-login pilot is not CURSOR_API_KEY / agentic HTTP.
    # Platform cursor workers use the separate CURSOR_API_KEY pool (see bridge).
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
        if "openrouter" in keyed and not _enabled_picker_models("openrouter"):
            required = _openrouter_ladder_ids()
            present = _agentic_ids_present()
            missing = [mid for mid in required if mid not in present]
            # Every ladder row missing means auto_route has nothing Marionette
            # curated to pick; re-seed from the curated set rather than routing
            # blind. A partial gap is normal (picker curation) and left alone.
            # Never seed when the user already toggled an OpenRouter subset.
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
            models = _get_provider_models_from_discovery(
                provider_name, key, force=force,
            )
            if not models:
                if (
                    _enabled_picker_models(provider_name)
                    or provider_name in ("opencode-go", "opencode-zen")
                ):
                    # Go/Zen discovery already used curated only when live
                    # itself was empty/failed. Do not re-seed stale rows.
                    models = []
                else:
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


def sync_agentic_registry_safe(force: bool = False) -> None:
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
        result = sync_agentic_registry(force=force)
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


_REFRESH_BOOT_DELAY_DEFAULT = 8
_REFRESH_INTERVAL_DEFAULT = 6 * 3600
_refresh_thread: Optional[threading.Thread] = None
_refresh_lock = threading.Lock()


def _refresh_env_disabled() -> bool:
    return os.environ.get("HARNESS_REGISTRY_AUTO_REFRESH", "1").strip().lower() in (
        "0",
        "false",
        "no",
        "off",
    )


def _refresh_boot_delay_seconds() -> float:
    raw = os.environ.get("HARNESS_REGISTRY_REFRESH_BOOT_DELAY", "").strip()
    if not raw:
        return float(_REFRESH_BOOT_DELAY_DEFAULT)
    try:
        return max(0.0, float(raw))
    except ValueError:
        return float(_REFRESH_BOOT_DELAY_DEFAULT)


def _refresh_interval_seconds() -> float:
    raw = os.environ.get("HARNESS_REGISTRY_REFRESH_SECONDS", "").strip()
    if not raw:
        return float(_REFRESH_INTERVAL_DEFAULT)
    try:
        return max(60.0, float(raw))
    except ValueError:
        return float(_REFRESH_INTERVAL_DEFAULT)


def _refresh_registry_once() -> None:
    """Force-fetch live catalogs and restamp the Marionette ladder."""
    try:
        sync_agentic_registry_safe(force=True)
    except Exception as e:
        _diag("auto_registry.auto_refresh", e)


def _registry_auto_refresh_loop() -> None:
    delay = _refresh_boot_delay_seconds()
    if delay > 0:
        time.sleep(delay)
    while True:
        _refresh_registry_once()
        time.sleep(_refresh_interval_seconds())


def start_registry_auto_refresh() -> bool:
    """Start the background catalog refresh daemon. Returns True if started.

    Boot health stays on the cached catalog so GUI bind is not blocked by a
    6s OpenRouter fetch. This thread force-refreshes shortly after bind, then
    every six hours, so dated snapshots appear without a restart.
    """
    if _refresh_env_disabled():
        return False
    global _refresh_thread
    with _refresh_lock:
        if _refresh_thread is not None and _refresh_thread.is_alive():
            return False
        thread = threading.Thread(
            target=_registry_auto_refresh_loop,
            name="registry-refresh",
            daemon=True,
        )
        _refresh_thread = thread
        thread.start()
        _diag("auto_registry.auto_refresh_started",
              msg=f"delay={_refresh_boot_delay_seconds()}s "
                  f"interval={_refresh_interval_seconds()}s")
        return True
