from __future__ import annotations

"""Full-stack vs pilot-only capability map for Settings honesty.

Product invariant: an auth offered in Settings should power pilot + workers
unless explicitly marked pilot-only. This module is the single source for that
label so API, Settings UI, and banners stay aligned.
"""

from typing import Any, Optional

# Harness provider names that sync into the agentic worker catalog and have a
# matching Puppetmaster PROVIDER_REGISTRY slug (or platform worker path).
FULL_STACK_AGENTIC = frozenset({
    "anthropic",
    "openai",
    "gemini",
    "openrouter",
    "deepseek",
    "zai",
    "xai",
    "bedrock",
    "opencode-go",
    "opencode-zen",
    "openai-codex",
    "nous",
    "minimax",
    "nvidia",
})

# Plan Sign-in rows that are pilot-only until a dedicated worker wire lands.
# Cursor CLI (agent login) is distinct from CURSOR_API_KEY platform workers.
PILOT_ONLY = frozenset({
    "cursor-cli",
})

# Credential-pool / platform worker auth that is not a harness pilot Provider
# but can drive Puppetmaster workers when present.
PLATFORM_WORKER_AUTH = frozenset({
    "cursor",  # CURSOR_API_KEY → platform cursor adapter
})


def worker_capability(provider_name: str) -> str:
    """Return ``full_stack``, ``pilot_only``, or ``platform_worker``."""
    name = (provider_name or "").strip().lower()
    if name in PILOT_ONLY:
        return "pilot_only"
    if name in PLATFORM_WORKER_AUTH:
        return "platform_worker"
    if name in FULL_STACK_AGENTIC:
        return "full_stack"
    return "pilot_only"


def capability_label(capability: str) -> str:
    if capability == "full_stack":
        return "Full stack"
    if capability == "platform_worker":
        return "Workers (platform)"
    return "Pilot only"


def capability_hint(capability: str) -> str:
    if capability == "full_stack":
        return "Powers chat pilot and agentic swarm/implement workers."
    if capability == "platform_worker":
        return (
            "Powers Puppetmaster cursor workers when CURSOR_API_KEY is set. "
            "Distinct from Cursor CLI agent-login pilot."
        )
    return (
        "Powers the chat pilot only. Swarm/implement workers need a Full stack "
        "API key or OAuth (OpenRouter, Codex, OpenCode Go, …). Cursor CLI is "
        "optional and not required. A Cursor API key is a separate platform-worker upgrade."
    )


def annotate_provider_row(row: dict) -> dict:
    """Stamp capability fields onto an API provider dict (best-effort)."""
    out = dict(row) if isinstance(row, dict) else {}
    name = str(out.get("name") or "").strip()
    cap = worker_capability(name)
    out["worker_capability"] = cap
    out["worker_capability_label"] = capability_label(cap)
    out["worker_capability_hint"] = capability_hint(cap)
    return out


def cursor_platform_workers_ready(env: Optional[Any] = None) -> bool:
    """True when platform cursor workers can run (CURSOR_API_KEY present)."""
    import os

    mapping = env if env is not None else os.environ
    try:
        return bool(str(mapping.get("CURSOR_API_KEY") or "").strip())
    except Exception:
        return False


def model_supports_reasoning_effort(provider_name: str, model_id: str) -> bool:
    """Return True when the given *provider_name*:*model_id* pair supports a
    reasoning-effort knob over the wire.

    Uses the existing per-adapter logic so no new model-family knowledge is
    duplicated:

    * ``openai-codex`` / ``codex-plan`` / ``chatgpt-codex`` — always True
      (Codex Responses ``reasoning_effort``).
    * ``anthropic`` / ``bedrock`` — delegates to
      :func:`harness.reasoning_effort.model_supports_anthropic_thinking`.
    * ``opencode-go`` — delegates to
      :func:`harness.opencode_go.reasoning_body_extras`; True when the
      function returns a non-empty dict (meaning it has a dialect for this
      model).
    * ``opencode-zen`` — always True (relay passes ``reasoning_effort``
      through; backend degrades gracefully on unknown models).
    * ``openrouter`` — always True (same relay-semantics reasoning).
    * Everything else — defaults to True so the user-visible knob never
      disappears (``None`` or unrecognized providers are treated
      permissively).
    """
    from . import providers as prov
    from .reasoning_effort import model_supports_anthropic_thinking

    name = (provider_name or "").strip().lower()
    mid = (model_id or "").strip().lower()

    # Resolve aliases to canonical provider name.
    p = prov.get_provider(name)
    if p is not None:
        name = p.name

    # -- Codex family always supports reasoning_effort --
    if name in ("openai-codex",):
        return True

    # -- Anthropic / Bedrock Claude (opus / sonnet, not haiku) --
    if name in ("anthropic", "bedrock"):
        return model_supports_anthropic_thinking(mid)

    # -- OpenCode Go: ask the per-family adapter --
    if name == "opencode-go":
        try:
            from .opencode_go import reasoning_body_extras
            extras = reasoning_body_extras(mid, effort="high")
            return bool(extras)
        except Exception:
            return True  # permissive fallback

    # -- OpenCode Zen / OpenRouter: relay passes reasoning_effort through --
    if name in ("opencode-zen", "openrouter"):
        return True

    # -- Default: show the knob --
    return True
