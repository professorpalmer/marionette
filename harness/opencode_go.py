from __future__ import annotations

"""OpenCode Go: flat-namespace catalog, per-model endpoint routing, and the
model-specific request quirks its relay requires.

OpenCode Go (https://opencode.ai/docs/go/) is a subscription RESELLER of open
coding models, not a vendor router. Two consequences shape this module:

1. The namespace is FLAT. Every model is published under a bare id whose dots
   are significant (``kimi-k2.7-code``, ``deepseek-v4-flash``). An id copied
   from an aggregator (``moonshotai/kimi-k3``) or from an OpenCode config
   (``opencode-go/kimi-k3``) is rejected by the relay, so it has to be reduced
   to the bare id before it goes on the wire.

2. The WIRE PROTOCOL VARIES PER MODEL. The published endpoint table routes
   MiniMax and Qwen to Anthropic Messages, GPT/Grok/Muse to OpenAI Responses,
   and everything else to OpenAI Chat Completions. Every other profile in
   providers.py can name a single provider-wide ``api_mode``; Go cannot, so the
   driver is chosen per model here instead.

PROVENANCE: the reasoning and completion-ceiling behaviors below are adapted
from the Hermes Agent OpenCode Go provider plugin
(``plugins/model-providers/opencode-zen/``), MIT License, Copyright (c) Nous
Research. Transport stays Marionette's own pmharness drivers.
"""

from typing import Any, Optional

from .opencode_common import (
    ANTHROPIC_MESSAGES,
    CHAT_COMPLETIONS,
    OPENAI_RESPONSES,
    USER_AGENT,
    build_opencode_driver,
    display_name_for as _shared_display_name_for,
    driver_base_url as _shared_driver_base_url,
    normalize_model_id,
    overlay_metadata as _shared_overlay_metadata,
)

PROVIDER_NAME = "opencode-go"
API_KEY_ENV = "OPENCODE_GO_API_KEY"
BASE_URL = "https://opencode.ai/zen/go/v1"

# Curated fallback shown when the live /models endpoint is unreachable, in the
# order the published Go catalog lists it. DeepSeek V4 Flash is the current
# DeepSeek-V4-Flash-0731 build -- the older v3/v2 slugs were never Go models
# and must not reappear as aliases here.
CURATED_MODELS = (
    "grok-4.5",
    "glm-5.3",
    "glm-5.2",
    "glm-5.1",
    "gpt-5.6-luna",
    "kimi-k3",
    "kimi-k2.7-code",
    "kimi-k2.6",
    "mimo-v2.5",
    "mimo-v2.5-pro",
    "minimax-m3",
    "minimax-m2.7",
    "qwen3.7-max",
    "qwen3.7-plus",
    "qwen3.6-plus",
    "deepseek-v4-pro",
    "deepseek-v4-flash",
    "hy3",
)

# Endpoint table (https://opencode.ai/docs/go/). Prefix matching rather than an
# exact allow-list so a newly added sibling (qwen3.8-plus, minimax-m4) routes
# correctly before the curated list catches up. Grok and Muse Spark use
# /responses; unknown ids stay on chat/completions.
_ANTHROPIC_MESSAGES_PREFIXES = ("minimax-", "qwen")
_OPENAI_RESPONSES_PREFIXES = ("gpt-", "grok-", "muse-spark-")

# Per-model completion ceiling. The relay's default of 262144 exceeds what
# Xiaomi actually serves for MiMo Pro (131072) and the request 400s, so the cap
# has to be sent explicitly instead of left to the relay default.
_MODEL_MAX_TOKENS = {
    "mimo-v2.5-pro": 131072,
}

# Reasoning-effort levels that mean "spend the most this model allows", and the
# levels these relays accept verbatim in ``reasoning_effort``.
_MAXED_EFFORTS = frozenset({"xhigh", "max", "ultra"})
_NATIVE_EFFORTS = frozenset({"low", "medium", "high"})


def is_retired_deepseek_go_model(model: Optional[str]) -> bool:
    """True when *model* is a retired DeepSeek V2/V3 slug the Go relay dropped."""
    bare = normalize_model_id(model).lower()
    return bare.startswith("deepseek-v2") or bare.startswith("deepseek-v3")


def api_mode_for_model(model: Optional[str]) -> str:
    """Which wire protocol the Go endpoint table assigns to *model*."""
    bare = normalize_model_id(model).lower()
    if not bare:
        return CHAT_COMPLETIONS
    if bare.startswith(_ANTHROPIC_MESSAGES_PREFIXES):
        return ANTHROPIC_MESSAGES
    if bare.startswith(_OPENAI_RESPONSES_PREFIXES):
        return OPENAI_RESPONSES
    return CHAT_COMPLETIONS


def driver_base_url(base_url: Optional[str] = None) -> str:
    """The ``/v1`` root every Marionette driver expects for Go."""
    return _shared_driver_base_url(base_url, default=BASE_URL)


def max_tokens_for_model(model: Optional[str], requested: Optional[int]) -> int:
    """Output ceiling for *model*, clamped to what the upstream vendor serves."""
    cap = _MODEL_MAX_TOKENS.get(normalize_model_id(model).lower())
    if not requested or requested <= 0:
        return cap or 0
    if cap is None:
        return int(requested)
    return min(int(requested), cap)


def temperature_for_model(model: Optional[str], requested: float = 0.0):
    """Temperature override the Go relay requires for *model*, else None.

    Kimi's Go endpoints currently accept only ``1``; sending Marionette's
    normal deterministic ``0`` is rejected as ``invalid temperature``. Every
    other family ships the caller's requested value untouched -- returning
    None means "no override", so the request shape does not vary as a side
    effect of family dispatch.
    """
    bare = normalize_model_id(model).lower()
    if bare.startswith("kimi-"):
        return 1.0
    return None


def _is_glm_5_3(bare: str) -> bool:
    """GLM-5.3 across the alias spellings config files carry."""
    return any(token in bare for token in ("glm-5.3", "glm-5-3", "glm-5p3"))


def _is_glm_5_2(bare: str) -> bool:
    """GLM-5.2 across the alias spellings config files carry."""
    if _is_glm_5_3(bare):
        return False
    return any(token in bare for token in ("glm-5.2", "glm-5-2", "glm-5p2"))


def _is_kimi_k2(bare: str) -> bool:
    return bare.startswith("kimi-k2")


def _is_deepseek_thinking(bare: str) -> bool:
    """DeepSeek builds that expose a thinking toggle (V4+, and the reasoner)."""
    if bare.startswith("deepseek-v") and not bare.startswith("deepseek-v3"):
        return True
    return bare == "deepseek-reasoner"


def reasoning_body_extras(model: Optional[str], effort: Optional[str] = None) -> dict:
    """Extra request-body fields Go needs to honor a reasoning level.

    Each family speaks a different dialect and rejects the others, so the
    mapping is per-family rather than one shared knob:

    - GLM-5.3 requires ``thinking.type=enabled`` and accepts
      ``reasoning_effort`` of ``low`` / ``high`` / ``max``. UI ``none``
      maps to ``low`` — omitting thinking fails the request.
    - GLM-5.2 exposes ``reasoning_effort`` with only ``high`` and ``max``
      enabled, so Marionette's richer scale collapses onto those two.
    - Kimi K2 and DeepSeek accept ``thinking`` (a binary toggle) OR
      ``reasoning_effort``, never both -- sending both is an HTTP 400.
    - MiMo has no reasoning knob; its quirk is the completion ceiling in
      :func:`max_tokens_for_model`.

    An unrecognized model returns ``{}`` so the relay default stands.

    The default effort (``low``, from :mod:`harness.reasoning_effort`) is NOT
    injected as a side effect of family dispatch: a user who never touched
    the reasoning setting gets the relay's own default request shape. Only
    GLM-5.3 keeps an explicit ``low`` mapping, because omitting ``thinking``
    fails that request outright.
    """
    from .reasoning_effort import current_reasoning_effort, normalize_reasoning_effort

    bare = normalize_model_id(model).lower()
    if not bare:
        return {}
    requested_explicit = effort is not None
    level = normalize_reasoning_effort(
        effort if requested_explicit else current_reasoning_effort()
    )
    if not requested_explicit and level == "low" and not _is_glm_5_3(bare):
        return {}

    if _is_glm_5_3(bare):
        if level in ("none", "low"):
            effort = "low"
        elif level in _MAXED_EFFORTS:
            effort = "max"
        else:
            effort = "high"
        return {"thinking": {"type": "enabled"}, "reasoning_effort": effort}

    if _is_glm_5_2(bare):
        if level == "none":
            return {}
        return {"reasoning_effort": "max" if level in _MAXED_EFFORTS else "high"}

    if _is_kimi_k2(bare):
        if level == "none":
            return {"thinking": {"type": "disabled"}}
        if level in _MAXED_EFFORTS:
            return {"reasoning_effort": "high"}
        if level in _NATIVE_EFFORTS:
            return {"reasoning_effort": level}
        return {"thinking": {"type": "enabled"}}

    if _is_deepseek_thinking(bare):
        if level == "none":
            return {"thinking": {"type": "disabled"}}
        if level in _MAXED_EFFORTS:
            return {"reasoning_effort": "max"}
        if level in _NATIVE_EFFORTS:
            return {"reasoning_effort": level}
        return {"thinking": {"type": "enabled"}}

    return {}


def build_driver(
    *,
    spec: str,
    model: str,
    api_key_env: str = API_KEY_ENV,
    max_tokens: int,
    base_url: Optional[str] = None,
):
    """The driver Go's endpoint table demands for *model*, keyed to *spec*.

    Callers pass the raw picker model (possibly namespaced); the bare id is what
    reaches the relay.
    """
    bare = normalize_model_id(model)
    return build_opencode_driver(
        spec=spec,
        model=bare,
        api_mode=api_mode_for_model(bare),
        api_key_env=api_key_env,
        max_tokens=max_tokens_for_model(bare, max_tokens),
        base_url=driver_base_url(base_url),
        temperature=temperature_for_model(bare),
        extra_body=reasoning_body_extras(bare),
    )


def display_name_for(model: Optional[str], metadata: Optional[dict] = None) -> str:
    """Friendly label for Settings/picker; never replaces the wire id."""
    return _shared_display_name_for(model, metadata)


def overlay_metadata(
    model: Optional[str], *, allow_network: bool = False,
) -> dict[str, Any]:
    """Best-effort models.dev overlay. Never raises; empty on any miss."""
    return _shared_overlay_metadata(
        PROVIDER_NAME, model, allow_network=allow_network,
    )
