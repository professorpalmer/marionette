from __future__ import annotations

"""OpenCode Zen: pay-as-you-go catalog, per-model endpoint routing, free fallback.

Zen (https://opencode.ai/docs/zen/) is a distinct provider from OpenCode Go.
The live ``GET /models`` listing is availability authority. models.dev metadata
is a name/capability overlay only — it must not advertise paid models that the
native listing did not return. Verified free ids survive a transient listing
failure.

PROVENANCE: profile data (env names, base URL, Hermes ``opencode-zen`` →
models.dev ``opencode`` mapping) follows the Hermes Agent OpenCode Zen plugin,
MIT License, Copyright (c) Nous Research. Transport stays Marionette drivers.
"""

from typing import Any, Optional

from .opencode_common import (
    ANTHROPIC_MESSAGES,
    CHAT_COMPLETIONS,
    OPENAI_RESPONSES,
    build_opencode_driver,
    display_name_for as _shared_display_name_for,
    driver_base_url as _shared_driver_base_url,
    normalize_model_id,
    overlay_metadata as _shared_overlay_metadata,
)

PROVIDER_NAME = "opencode-zen"
# Prefer a Zen-specific key, then the generic OpenCode key, then the Go
# subscription key (live probes show the same account key lists Zen).
API_KEY_ENV = "OPENCODE_ZEN_API_KEY"
API_KEY_ENVS = (API_KEY_ENV, "OPENCODE_API_KEY", "OPENCODE_GO_API_KEY")
BASE_URL = "https://opencode.ai/zen/v1"

# Verified free ids from current Zen docs + live listing / Hermes. Do not
# invent slugs. Paid models are never curated — they appear only when live.
CURATED_FREE_MODELS = (
    "x-preview-f-free",
    "big-pickle",
    "mimo-v2.5-free",
    "hy3-free",
    "deepseek-v4-flash-free",
    "laguna-s-2.1-free",
    "nemotron-3-ultra-free",
    "nemotron-3.5-lightning-free",
    "muse-spark-1.2-contributor-free",
)

# Display names for Settings/picker search. Wire specs stay provider:id.
# Go ``ox-alpha-free`` and Zen ``x-preview-f-free`` share Ox Alpha Free in
# SHARED_DISPLAY_NAMES.
DISPLAY_NAMES = {
    "big-pickle": "Big Pickle",
    "mimo-v2.5-free": "MiMo-V2.5 Free",
    "hy3-free": "Hy3 Free",
    "deepseek-v4-flash-free": "DeepSeek V4 Flash Free",
    "laguna-s-2.1-free": "Laguna S 2.1 Free",
    "nemotron-3-ultra-free": "Nemotron 3 Ultra Free",
    "nemotron-3.5-lightning-free": "Nemotron 3.5 Lightning Free",
    "muse-spark-1.2-contributor-free": "Muse Spark 1.2 Contributor Free",
}

# Hermes models.dev provider id for this profile.
MODELS_DEV_PROVIDER = "opencode"

# Current Zen endpoint table (https://opencode.ai/docs/zen/). Conservative
# default is chat/completions for unknown ids. The free variants are
# chat-completions ONLY: the relay serves them on /chat/completions, and the
# Responses dialect silently degrades their output (Ox Alpha Free /
# muse-spark-*-free). Free ids win over the paid-family prefix table.
_ANTHROPIC_MESSAGES_PREFIXES = ("claude-", "qwen")
_OPENAI_RESPONSES_PREFIXES = ("gpt-", "grok-", "muse-spark-")
_CHAT_COMPLETIONS_SUFFIXES = ("-free",)


def is_free_variant(model: Optional[str]) -> bool:
    """True for a Zen free-tier variant id (``...-free``, ``x-preview-f-free``)."""
    bare = normalize_model_id(model).lower()
    if not bare:
        return False
    return bare.endswith(_CHAT_COMPLETIONS_SUFFIXES) or bare in {
        "x-preview-f-free", "big-pickle",
    }


def api_mode_for_model(model: Optional[str]) -> str:
    """Which wire protocol the Zen endpoint table assigns to *model*.

    Free variants are hard-routed to Chat Completions regardless of any paid
    family prefix they share, so the free tier can never be dispatched through
    the degraded Responses mode.
    """
    bare = normalize_model_id(model).lower()
    if not bare:
        return CHAT_COMPLETIONS
    if bare.startswith(_ANTHROPIC_MESSAGES_PREFIXES) and not is_free_variant(bare):
        return ANTHROPIC_MESSAGES
    if is_free_variant(bare):
        return CHAT_COMPLETIONS
    if bare.startswith(_OPENAI_RESPONSES_PREFIXES):
        return OPENAI_RESPONSES
    return CHAT_COMPLETIONS


def driver_base_url(base_url: Optional[str] = None) -> str:
    """The ``/v1`` root every Marionette driver expects for Zen."""
    return _shared_driver_base_url(base_url, default=BASE_URL)


def display_name_for(model: Optional[str], metadata: Optional[dict] = None) -> str:
    """Friendly label for Settings/picker; never replaces the wire id."""
    return _shared_display_name_for(model, metadata, extra_labels=DISPLAY_NAMES)


def is_curated_free_model(model: Optional[str]) -> bool:
    return normalize_model_id(model).lower() in {
        m.lower() for m in CURATED_FREE_MODELS
    }


def overlay_metadata(
    model: Optional[str], *, allow_network: bool = False,
) -> dict[str, Any]:
    """Best-effort models.dev overlay. Never raises; empty on any miss.

    Default is cache-only so chat/picker hot paths never block on models.dev.
    Settings force-refresh may pass ``allow_network=True``.
    """
    return _shared_overlay_metadata(
        PROVIDER_NAME, model, allow_network=allow_network,
    )


def build_driver(
    *,
    spec: str,
    model: str,
    api_key_env: str = API_KEY_ENV,
    max_tokens: int,
    base_url: Optional[str] = None,
):
    """The driver Zen's endpoint table demands for *model*."""
    bare = normalize_model_id(model)
    return build_opencode_driver(
        spec=spec,
        model=bare,
        api_mode=api_mode_for_model(bare),
        api_key_env=api_key_env,
        max_tokens=max_tokens,
        base_url=driver_base_url(base_url),
    )
