from __future__ import annotations

"""Provider registry for the swappable PILOT.

The pilot is the model the user converses with; it must span whatever provider
keys the user actually has (Anthropic / OpenAI / OpenRouter / DeepSeek / Gemini /
Z.AI / MiniMax / xAI / Nvidia / ...). This module declares each provider's auth +
endpoint + API shape, detects which are available from the environment, and
builds the right thin driver.

PROVENANCE: the provider PROFILE DATA below (env-var names, base URLs, API modes,
aliases) is adapted from the Hermes Agent project's declarative provider profiles
(`providers/` + `plugins/model-providers/`), MIT License, Copyright (c) Nous Research.
We borrow the declarative profile DATA and shape only -- not Hermes's
transport/agent core, which is coupled to its conversation loop and prompt-cache
machinery the bounded pilot does not need. Our transport stays the thin
OpenAICompatDriver / AnthropicDriver already in pmharness/drivers/.

MIT License text: https://github.com/NousResearch/hermes-agent (LICENSE).
"""

import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class Provider:
    """Declarative provider profile (data adapted from Hermes, MIT)."""
    name: str
    env_vars: tuple              # candidate API-key env vars, first match wins
    base_url: str
    api_mode: str = "chat_completions"   # chat_completions | anthropic_messages
    aliases: tuple = ()
    display_name: str = ""
    # curated pilot-capable models shown when a live catalog fetch isn't done.
    pilot_models: tuple = ()

    def key(self) -> Optional[str]:
        """The first present, non-empty key value across env_vars, or None."""
        for ev in self.env_vars:
            v = os.environ.get(ev, "").strip()
            if v:
                return v
        return None

    def key_env(self) -> Optional[str]:
        for ev in self.env_vars:
            if os.environ.get(ev, "").strip():
                return ev
        return None

    @property
    def available(self) -> bool:
        return self.key() is not None


# ── Provider profiles (data adapted from Hermes model-provider plugins, MIT) ──
# OpenRouter intentionally first: one key fans out to the whole open field.
PROVIDERS = (
    Provider(
        name="openrouter", aliases=("or",),
        env_vars=("OPENROUTER_API_KEY",),
        base_url="https://openrouter.ai/api/v1",
        api_mode="chat_completions", display_name="OpenRouter",
        pilot_models=("qwen/qwen3-coder-30b-a3b-instruct", "z-ai/glm-5.2",
                      "deepseek/deepseek-v4-pro", "moonshotai/kimi-k2.6",
                      "anthropic/claude-opus-4.8", "openai/gpt-5.4"),
    ),
    Provider(
        name="anthropic", aliases=("claude",),
        env_vars=("ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN"),
        base_url="https://api.anthropic.com",
        api_mode="anthropic_messages", display_name="Anthropic",
        pilot_models=("claude-opus-4-8", "claude-sonnet-4-5", "claude-haiku-4-5"),
    ),
    Provider(
        name="openai", aliases=("oai",),
        env_vars=("OPENAI_API_KEY",),
        base_url="https://api.openai.com/v1",
        api_mode="chat_completions", display_name="OpenAI",
        pilot_models=("gpt-5.4", "gpt-5.4-mini"),
    ),
    Provider(
        name="gemini", aliases=("google", "google-gemini"),
        env_vars=("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        api_mode="chat_completions", display_name="Google Gemini",
        # use -latest aliases so the picker tracks Google's current models
        # without pinning a version that may rotate out.
        pilot_models=("gemini-flash-latest", "gemini-pro-latest"),
    ),
    Provider(
        name="deepseek", aliases=("deepseek-chat",),
        env_vars=("DEEPSEEK_API_KEY",),
        base_url="https://api.deepseek.com/v1",
        api_mode="chat_completions", display_name="DeepSeek",
        pilot_models=("deepseek-chat", "deepseek-reasoner"),
    ),
    Provider(
        name="zai", aliases=("glm", "z-ai", "zhipu"),
        env_vars=("GLM_API_KEY", "ZAI_API_KEY", "Z_AI_API_KEY"),
        base_url="https://api.z.ai/api/paas/v4",
        api_mode="chat_completions", display_name="Z.AI (GLM)",
        pilot_models=("glm-5.2", "glm-4.7-flash"),
    ),
    Provider(
        name="minimax", aliases=("mini-max",),
        env_vars=("MINIMAX_API_KEY",),
        base_url="https://api.minimax.io/anthropic",
        api_mode="anthropic_messages", display_name="MiniMax",
        pilot_models=("MiniMax-M3", "MiniMax-M2.7"),
    ),
    Provider(
        name="xai", aliases=("grok", "x-ai"),
        env_vars=("XAI_API_KEY",),
        base_url="https://api.x.ai/v1",
        api_mode="chat_completions", display_name="xAI Grok",
        pilot_models=("grok-4", "grok-4-fast"),
    ),
    Provider(
        name="nvidia", aliases=("nvidia-nim",),
        env_vars=("NVIDIA_API_KEY",),
        base_url="https://integrate.api.nvidia.com/v1",
        api_mode="chat_completions", display_name="NVIDIA NIM",
        pilot_models=("qwen/qwen3-coder-480b", "deepseek-ai/deepseek-v3.1"),
    ),
)

_BY_NAME = {p.name: p for p in PROVIDERS}
for _p in PROVIDERS:
    for _a in _p.aliases:
        _BY_NAME.setdefault(_a, _p)


def get_provider(name: str) -> Optional[Provider]:
    return _BY_NAME.get((name or "").lower())


def available_providers() -> list:
    """Providers with a usable key in the current environment."""
    seen = set()
    out = []
    for p in PROVIDERS:
        if p.name in seen:
            continue
        if p.available:
            out.append(p)
            seen.add(p.name)
    return out


def available_pilots() -> list:
    """[(provider_name, model)] for every provider that has a key, as
    'provider:model' picker entries. OpenRouter expands its open field."""
    entries = []
    for p in available_providers():
        for m in p.pilot_models:
            entries.append(f"{p.name}:{m}")
    return entries


def build_pilot(spec: str, *, max_tokens: int = 1500):
    """Build a thin driver for a pilot spec.

    spec forms:
      'provider:model'  -> explicit provider + model (e.g. 'anthropic:claude-opus-4-8')
      'model'           -> resolved against the first available provider whose
                           pilot_models contains it, else OpenRouter slug.
    Returns a driver exposing .complete(prompt, system=...). Transport is OURS
    (pmharness drivers); only the routing DATA is Hermes-derived.
    """
    from pmharness.drivers.openai_compat import OpenAICompatDriver
    from pmharness.drivers.anthropic import AnthropicDriver

    provider = None
    model = spec
    if ":" in spec:
        pname, model = spec.split(":", 1)
        provider = get_provider(pname)
    if provider is None:
        # resolve a bare model name to a provider that lists it
        for p in available_providers():
            if model in p.pilot_models:
                provider = p
                break
    if provider is None:
        # last resort: OpenRouter (one key, whole field) if present.
        # Translate a catalog short-name (e.g. "qwen3-coder-30b") to its real
        # OpenRouter slug so we don't send an invalid model ID.
        provider = get_provider("openrouter")
        if provider is not None and "/" not in model:
            try:
                from pmharness.registry import _entry as _cat_entry
                slug = _cat_entry(model).get("openrouter")
                if slug:
                    model = slug
            except Exception:
                pass
    if provider is None or not provider.available:
        raise ProviderError(
            f"no provider key available for pilot {spec!r}. Set one of: "
            + ", ".join(sorted({ev for p in PROVIDERS for ev in p.env_vars}))
        )

    key_env = provider.key_env()
    if provider.api_mode == "anthropic_messages":
        # AnthropicDriver appends /messages, so the base must end in the version
        # segment (.../v1 for Anthropic native, .../anthropic for MiniMax).
        burl = provider.base_url
        if burl.rstrip("/").endswith("anthropic.com"):
            burl = burl.rstrip("/") + "/v1"
        return AnthropicDriver(name=spec, model=model, base_url=burl,
                               api_key_env=key_env, max_tokens=max_tokens)
    return OpenAICompatDriver(name=spec, model=model, base_url=provider.base_url,
                              api_key_env=key_env, max_tokens=max_tokens)


class ProviderError(RuntimeError):
    pass
