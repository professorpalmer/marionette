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

cursor-cli is Marionette-native (no Hermes profile): local `agent` CLI
stream-json + `agent login` session auth — not CURSOR_API_KEY pool rotate.

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
    # A cheap vision-capable model on this provider. The vision sidecar uses it
    # to transcribe images with the SAME key the user already configured, so
    # image input works without a dedicated GEMINI/OPENROUTER key. Empty string
    # means this provider has no first-class vision model wired in this rig.
    vision_model: str = ""

    def _is_disconnected(self) -> bool:
        """True if the user explicitly disconnected this provider. Authoritative
        over the environment -- so a shell-exported key (re-injected by the
        desktop app's login-shell env capture) cannot resurrect a provider the
        user turned off. This is the single source of truth, checked at the key
        lookup itself so every downstream consumer (.available, get_provider_key,
        get_api_key_status, the picker) honors it regardless of scrub timing."""
        try:
            from .keys import get_disconnected
            return self.name in get_disconnected()
        except Exception:
            return False

    def key(self) -> Optional[str]:
        """The first present, non-empty key value across env_vars, or None.
        Returns None for an explicitly-disconnected provider even when its key
        is present in the environment."""
        if self._is_disconnected():
            return None
        if self.name == "bedrock":
            # Bearer OR (access key + secret); never treat a lone access key as
            # configured. Prefer the bearer token for the opaque "key" value.
            try:
                from .keys import bedrock_credential_token
                return bedrock_credential_token()
            except Exception:
                return None
        if self.name == "cursor-cli":
            # Sentinel only: real auth is `agent login` / `agent status`.
            try:
                from .cursor_cli_auth import login_token_if_ready
                return login_token_if_ready()
            except Exception:
                return None
        for ev in self.env_vars:
            v = os.environ.get(ev, "").strip()
            if v:
                return v
        try:
            from .credential_pool import peek_token, peek_token_for_env
            for ev in self.env_vars:
                tok = peek_token_for_env(ev)
                if tok:
                    return tok
            tok = peek_token(self.name)
            if tok:
                return tok
        except Exception:
            pass
        return None

    def key_env(self) -> Optional[str]:
        if self._is_disconnected():
            return None
        if self.name == "bedrock":
            if (os.environ.get("AWS_BEARER_TOKEN_BEDROCK") or "").strip():
                return "AWS_BEARER_TOKEN_BEDROCK"
            if ((os.environ.get("AWS_ACCESS_KEY_ID") or "").strip()
                    and (os.environ.get("AWS_SECRET_ACCESS_KEY") or "").strip()):
                return "AWS_ACCESS_KEY_ID"
            return None
        if self.name == "cursor-cli":
            return "CURSOR_CLI_LOGIN" if self.key() else None
        for ev in self.env_vars:
            if os.environ.get(ev, "").strip():
                return ev
        # Pool-only credentials still need a stable env name for drivers
        # (includes OAuth sibling pools, e.g. xai-oauth for XAI_API_KEY).
        try:
            from .credential_pool import credential_satisfied, has_healthy_credential
            for ev in self.env_vars:
                if credential_satisfied(ev):
                    return ev
            if has_healthy_credential(self.name) and self.env_vars:
                return self.env_vars[0]
        except Exception:
            pass
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
                      "anthropic/claude-opus-4.8", "openai/gpt-5.6-sol",
                      "openai/gpt-5.6-terra", "openai/gpt-5.5"),
        vision_model="qwen/qwen3-vl-30b-a3b-instruct",
    ),
    Provider(
        name="anthropic", aliases=("claude",),
        env_vars=("ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN"),
        base_url="https://api.anthropic.com",
        api_mode="anthropic_messages", display_name="Anthropic",
        pilot_models=("claude-opus-4-8", "claude-sonnet-4-5", "claude-haiku-4-5"),
        vision_model="claude-haiku-4-5",
    ),
    Provider(
        name="openai", aliases=("oai",),
        env_vars=("OPENAI_API_KEY",),
        base_url="https://api.openai.com/v1",
        api_mode="chat_completions", display_name="OpenAI",
        # GPT-5.6 Sol/Terra/Luna (GA 2026-07-09). Some accounts still see
        # limited-preview 404 until OpenAI finishes rollout; live probe
        # via model_fetch still wins when /v1/models lists them.
        pilot_models=(
            "gpt-5.6-sol",
            "gpt-5.6-terra",
            "gpt-5.6-luna",
            "gpt-5.5",
            "gpt-5.4",
            "gpt-5.4-mini",
        ),
        vision_model="gpt-5.6-luna",
    ),
    Provider(
        name="openai-codex", aliases=("codex-plan", "chatgpt-codex"),
        env_vars=("OPENAI_CODEX_TOKEN",),
        base_url="https://chatgpt.com/backend-api/codex",
        api_mode="codex_responses", display_name="ChatGPT Codex (OAuth)",
        # ChatGPT Codex OAuth catalog (Hermes-aligned). Live fetch from
        # chatgpt.com/backend-api/codex/models merges on top when signed in.
        pilot_models=(
            "gpt-5.6-sol",
            "gpt-5.6-sol-pro",
            "gpt-5.6-terra",
            "gpt-5.6-terra-pro",
            "gpt-5.6-luna",
            "gpt-5.6-luna-pro",
            "gpt-5.5",
            "gpt-5.4",
            "gpt-5.4-mini",
            "gpt-5.3-codex",
            "gpt-5.3-codex-spark",
            "o3",
            "o4-mini",
        ),
        vision_model="gpt-5.4",
    ),
    # Cursor Agent CLI plan pilot. Distinct from platform which('cursor') and
    # from CURSOR_API_KEY credential pools. Availability = agent binary + login.
    Provider(
        name="cursor-cli", aliases=("cursor-agent",),
        env_vars=("CURSOR_CLI_LOGIN",),
        base_url="",
        api_mode="cursor_cli", display_name="Cursor CLI (plan)",
        pilot_models=(
            "composer-2.5",
            "composer-2.5-fast",
            "cursor-grok-4.5-high",
            "cursor-grok-4.5-medium",
            "cursor-grok-4.5-low",
            "claude-opus-4-8-thinking-high",
            "claude-opus-4-8-high",
            "gpt-5.5-high",
            "gpt-5.6-sol-medium",
            "gpt-5.4-high",
            "claude-4.5-sonnet-thinking",
            "claude-4.5-sonnet",
            "sonnet-4",
            "sonnet-4-thinking",
            "gpt-5",
            "composer-1",
        ),
    ),
    Provider(
        name="nous", aliases=("nousresearch",),
        env_vars=("NOUS_API_KEY",),
        base_url="https://inference-api.nousresearch.com/v1",
        api_mode="chat_completions", display_name="Nous Portal (OAuth)",
        pilot_models=("Hermes-3-Llama-3.1-70B", "Hermes-4-70B"),
    ),
    Provider(
        name="gemini", aliases=("google", "google-gemini"),
        env_vars=("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        api_mode="gemini_native", display_name="Google Gemini",
        # use -latest aliases so the picker tracks Google's current models
        # without pinning a version that may rotate out.
        pilot_models=("gemini-3.5-flash", "gemini-flash-latest", "gemini-pro-latest"),
        vision_model="gemini-flash-latest",
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
        vision_model="grok-4-fast",
    ),
    Provider(
        name="nvidia", aliases=("nvidia-nim",),
        env_vars=("NVIDIA_API_KEY",),
        base_url="https://integrate.api.nvidia.com/v1",
        api_mode="chat_completions", display_name="NVIDIA NIM",
        pilot_models=("qwen/qwen3-coder-480b", "deepseek-ai/deepseek-v3.1"),
    ),
    # AWS Bedrock: auth is multi-env (bearer OR access-key pair + region).
    # Interactive pilot uses BedrockDriver (puppetmaster.bedrock.bedrock_chat);
    # Marionette stores credentials and injects AWS_* / BEDROCK_* into the env.
    Provider(
        name="bedrock", aliases=("aws-bedrock", "aws"),
        env_vars=("AWS_BEARER_TOKEN_BEDROCK", "AWS_ACCESS_KEY_ID"),
        base_url="https://bedrock-runtime.us-east-1.amazonaws.com",
        api_mode="bedrock", display_name="AWS Bedrock",
        pilot_models=(
            # Curated fallback only — live ListFoundationModels/ListInferenceProfiles
            # replaces this when AWS creds work (see harness.model_fetch).
            "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            "us.anthropic.claude-haiku-4-5-20251001-v1:0",
            "amazon.nova-micro-v1:0",
            "amazon.nova-lite-v1:0",
            "deepseek.v3.2",
            "zai.glm-4.7-flash",
            "moonshotai.kimi-k2.5",
            "qwen.qwen3-coder-30b-a3b-v1:0",
            "meta.llama3-1-8b-instruct-v1:0",
            "mistral.mistral-7b-instruct-v0:2",
            "minimax.minimax-m2.5",
        ),
    ),
)

_BY_NAME = {p.name: p for p in PROVIDERS}
for _p in PROVIDERS:
    for _a in _p.aliases:
        _BY_NAME.setdefault(_a, _p)


def get_provider(name: str) -> Optional[Provider]:
    return _BY_NAME.get((name or "").lower())


def available_providers() -> list:
    """Providers with a usable key in the current environment, EXCLUDING any the
    user explicitly disconnected (authoritative over a shell-exported key)."""
    try:
        from .keys import get_disconnected
        disconnected = get_disconnected()
    except Exception:
        disconnected = set()
    seen = set()
    out = []
    for p in PROVIDERS:
        if p.name in seen:
            continue
        if p.name in disconnected:
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
        # Honor an explicit BEDROCK_MODEL_ID as an extra pilot entry when set.
        if p.name == "bedrock":
            extra = (os.environ.get("BEDROCK_MODEL_ID") or "").strip()
            if not extra:
                try:
                    from .keys import get_bedrock_status
                    extra = (get_bedrock_status().get("model_id") or "").strip()
                except Exception:
                    extra = ""
            if extra:
                spec = f"bedrock:{extra}"
                if spec not in entries:
                    entries.append(spec)

    # NOTE: MoA presets are deliberately NOT offered as pilots. MoA is a
    # planner/review virtual model (Mixture-of-Agents) and cannot act as the
    # tool-calling executor -- selecting it as the pilot produced a hard runtime
    # error ("MoA is a planner/review virtual-model and cannot be used as the
    # tool-calling executor"). It remains usable where a planner/reviewer fits,
    # just not as the interactive driver.
    return entries


def _finalize_driver(driver):
    """Optional cassette record/replay wrapper (HARNESS_CASSETTE_MODE)."""
    from pmharness.drivers.cassette import maybe_wrap_cassette

    return maybe_wrap_cassette(driver)


def build_pilot(spec: str, *, max_tokens: int | None = None):
    """Build a thin driver for a pilot spec.

    spec forms:
      'provider:model'  -> explicit provider + model (e.g. 'anthropic:claude-opus-4-8')
      'model'           -> resolved against the first available provider whose
                           pilot_models contains it, else OpenRouter slug.
    Returns a driver exposing .complete(prompt, system=...). Transport is OURS
    (pmharness drivers); only the routing DATA is Hermes-derived.
    """
    # Output-token ceiling. Default to HARNESS_MAX_TOKENS (8000) so large edit_file
    # / write_file tool calls are NOT truncated mid-arguments -- a 1500-token cap
    # silently cut off big tool-call JSON, which is why edit_file "lost" its args.
    if max_tokens is None:
        try:
            max_tokens = int(os.environ.get("HARNESS_MAX_TOKENS", "").strip() or "8000")
        except (ValueError, TypeError):
            max_tokens = 8000

    preset_name = spec
    if spec.startswith("moa:"):
        preset_name = spec[4:]

    try:
        from pmharness.registry import load_catalog
        moa_presets = load_catalog().get("moa_presets", {})
    except Exception:
        moa_presets = {}

    if preset_name in moa_presets or spec.startswith("moa-") or preset_name.startswith("moa-"):
        if not os.environ.get("OPENROUTER_API_KEY", "").strip():
            raise ProviderError(
                f"no provider key available for pilot {spec!r}. Set: OPENROUTER_API_KEY"
            )
        from pmharness.registry import build as registry_build
        return _finalize_driver(registry_build(preset_name, reach="openrouter"))

    from pmharness.drivers.openai_compat import OpenAICompatDriver
    from pmharness.drivers.anthropic import AnthropicDriver

    provider = None
    model = spec
    if ":" in spec:
        pname, model = spec.split(":", 1)
        provider = get_provider(pname)
    if provider is None:
        # Resolve a bare model name to a provider that lists it. Prefer
        # openai-codex over openai when both advertise the same id so a
        # ChatGPT plan OAuth burn isn't shadowed by OPENAI_API_KEY.
        candidates = [p for p in available_providers() if model in p.pilot_models]
        for preferred in ("openai-codex",):
            for p in candidates:
                if p.name == preferred:
                    provider = p
                    break
            if provider is not None:
                break
        if provider is None and candidates:
            provider = candidates[0]
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

    key_env = provider.key_env() or ""
    if provider.api_mode == "gemini_native":
        from pmharness.drivers.gemini import GeminiDriver
        burl = provider.base_url
        kwargs = {}
        if burl and burl.rstrip("/").endswith("v1beta"):
            kwargs["base_url"] = burl
        return _finalize_driver(
            GeminiDriver(name=spec, model=model, api_key_env=key_env, max_tokens=max_tokens, **kwargs)
        )

    if provider.api_mode == "anthropic_messages":
        # AnthropicDriver appends /messages, so the base must end in the version
        # segment (.../v1 for Anthropic native, .../anthropic for MiniMax).
        burl = provider.base_url
        if burl.rstrip("/").endswith("anthropic.com"):
            burl = burl.rstrip("/") + "/v1"
        return _finalize_driver(
            AnthropicDriver(name=spec, model=model, base_url=burl,
                            api_key_env=key_env, max_tokens=max_tokens)
        )
    if provider.api_mode == "bedrock":
        from pmharness.drivers.bedrock import BedrockDriver
        return _finalize_driver(
            BedrockDriver(name=spec, model=model, max_tokens=max_tokens)
        )
    if provider.api_mode == "codex_responses":
        from pmharness.drivers.codex_responses import CodexResponsesDriver
        return _finalize_driver(
            CodexResponsesDriver(
                name=spec,
                model=model,
                base_url=provider.base_url,
                api_key_env=key_env or "OPENAI_CODEX_TOKEN",
                max_tokens=max_tokens,
            )
        )
    if provider.api_mode == "cursor_cli":
        # Workspace trust / --workspace follow the open project (HARNESS_REPO).
        cwd = (os.environ.get("HARNESS_REPO") or "").strip() or None
        # Warm ACP is opt-in (HARNESS_CURSOR_ACP=1). Default stays per-turn
        # `agent --print` until the persistent process is proven across turns.
        from pmharness.drivers.cursor_acp import CursorAcpDriver, cursor_acp_enabled
        if cursor_acp_enabled():
            return _finalize_driver(
                CursorAcpDriver(
                    name=spec, model=model, max_tokens=max_tokens, cwd=cwd,
                )
            )
        from pmharness.drivers.cursor_cli import CursorCliDriver
        return _finalize_driver(
            CursorCliDriver(
                name=spec, model=model, max_tokens=max_tokens, cwd=cwd,
            )
        )
    return _finalize_driver(
        OpenAICompatDriver(name=spec, model=model, base_url=provider.base_url,
                           api_key_env=key_env, max_tokens=max_tokens)
    )


class ProviderError(RuntimeError):
    pass
