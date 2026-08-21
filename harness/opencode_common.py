from __future__ import annotations

"""Shared OpenCode Go/Zen helpers: flat ids, /v1 base healing, driver factory.

Go and Zen are distinct providers with distinct catalogs. This module holds
only the pieces that are identical on the wire so neither profile copies the
other.
"""

import urllib.parse
from typing import Any, Optional

USER_AGENT = "Marionette"

# Verified friendly names shared across Go and Zen wire ids.
SHARED_DISPLAY_NAMES = {
    "ox-alpha-free": "Ox Alpha Free",
    "x-preview-f-free": "Ox Alpha Free",
}

CHAT_COMPLETIONS = "chat_completions"
ANTHROPIC_MESSAGES = "anthropic_messages"
OPENAI_RESPONSES = "openai_responses"


def informative_display_name(label: object, model: Optional[str]) -> str:
    """Return a stripped label, or '' if missing or equal to the bare wire id."""
    if not isinstance(label, str):
        return ""
    text = label.strip()
    if not text:
        return ""
    bare = normalize_model_id(model)
    if bare and text.lower() == bare.lower():
        return ""
    return text


def needs_catalog_overlay(metadata: Optional[dict], model: Optional[str]) -> bool:
    """True when native metadata has no informative name (absent or id-echo)."""
    if not isinstance(metadata, dict):
        return True
    return not (
        informative_display_name(metadata.get("name"), model)
        or informative_display_name(metadata.get("display_name"), model)
    )


def merge_catalog_overlay(
    native: Optional[dict],
    overlay: Optional[dict],
    model: Optional[str],
) -> dict[str, Any]:
    """Merge overlay onto native metadata. Informative native names win."""
    merged = dict(native or {})
    if not overlay:
        return merged
    for key, value in overlay.items():
        if key in ("name", "display_name"):
            if not informative_display_name(merged.get(key), model):
                label = informative_display_name(value, model)
                if label:
                    merged[key] = label
            continue
        merged.setdefault(key, value)
    return merged


def display_name_for(
    model: Optional[str],
    metadata: Optional[dict] = None,
    *,
    extra_labels: Optional[dict[str, str]] = None,
) -> str:
    """Friendly label for Settings/picker; never replaces the wire id.

    A native ``name`` that merely echoes the bare id (case-insensitive) is
    uninformative — verified/shared labels and extra_labels win instead.
    """
    bare = normalize_model_id(model)
    if isinstance(metadata, dict):
        for key in ("name", "display_name"):
            label = informative_display_name(metadata.get(key), model)
            if label:
                return label
    labels = extra_labels or {}
    return labels.get(bare) or SHARED_DISPLAY_NAMES.get(bare, bare)


def overlay_metadata(
    provider_name: str,
    model: Optional[str],
    *,
    allow_network: bool = False,
) -> dict[str, Any]:
    """Best-effort models.dev overlay. Never raises; empty on any miss."""
    try:
        from .models_dev import lookup_model

        return lookup_model(
            provider_name,
            normalize_model_id(model),
            allow_network=allow_network,
        ) or {}
    except Exception:
        return {}


def normalize_model_id(model: Optional[str]) -> str:
    """Bare OpenCode model id: strip a provider or vendor prefix, keep dots."""
    text = str(model or "").strip()
    if not text:
        return ""
    return text.rsplit("/", 1)[-1].strip()


def driver_base_url(base_url: Optional[str], *, default: str) -> str:
    """The ``/v1`` root Marionette drivers expect for an official OpenCode host.

    All three drivers append only the endpoint segment, so they share one root.
    Anthropic-routed configs can persist a ``/v1``-stripped URL; re-append it
    on opencode.ai so a later chat/responses model does not POST to the
    marketing site. Custom relays on other hosts are left as given.
    """
    url = str(base_url or default).strip().rstrip("/")
    if not url or url.endswith("/v1"):
        return url or default
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
    except ValueError:
        host = ""
    if host == "opencode.ai" or host.endswith(".opencode.ai"):
        return url + "/v1"
    return url


def build_opencode_driver(
    *,
    spec: str,
    model: str,
    api_mode: str,
    api_key_env: str,
    max_tokens: int,
    base_url: str,
    temperature: float = 0.0,
    extra_body: Optional[dict] = None,
):
    """Driver for one OpenCode model after the profile has chosen *api_mode*."""
    bare = normalize_model_id(model)
    if api_mode == ANTHROPIC_MESSAGES:
        from pmharness.drivers.anthropic import AnthropicDriver

        return AnthropicDriver(
            name=spec, model=bare, base_url=base_url,
            api_key_env=api_key_env, max_tokens=max_tokens,
        )
    if api_mode == OPENAI_RESPONSES:
        from pmharness.drivers.codex_responses import CodexResponsesDriver

        return CodexResponsesDriver(
            name=spec, model=bare, base_url=base_url,
            api_key_env=api_key_env, max_tokens=max_tokens,
            chatgpt_backend=False,
        )
    from pmharness.drivers.openai_compat import OpenAICompatDriver

    return OpenAICompatDriver(
        name=spec, model=bare, base_url=base_url,
        api_key_env=api_key_env, max_tokens=max_tokens,
        temperature=temperature,
        extra_body=extra_body or None,
        extra_headers={"User-Agent": USER_AGENT},
    )
