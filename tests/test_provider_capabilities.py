from __future__ import annotations

"""Full-stack vs pilot-only capability map."""


def test_cursor_cli_is_pilot_only():
    from harness.provider_capabilities import worker_capability, capability_hint

    assert worker_capability("cursor-cli") == "pilot_only"
    hint = capability_hint("pilot_only").lower()
    assert "full stack" in hint
    assert "not required" in hint


def test_openrouter_is_full_stack():
    from harness.provider_capabilities import worker_capability

    assert worker_capability("openrouter") == "full_stack"


def test_openai_codex_and_nous_are_full_stack():
    from harness.provider_capabilities import worker_capability

    assert worker_capability("openai-codex") == "full_stack"
    assert worker_capability("opencode-zen") == "full_stack"
    assert worker_capability("nous") == "full_stack"
    assert worker_capability("minimax") == "full_stack"
    assert worker_capability("nvidia") == "full_stack"


def test_cursor_api_key_pool_is_platform_worker():
    from harness.provider_capabilities import worker_capability

    assert worker_capability("cursor") == "platform_worker"


def test_annotate_provider_row_stamps_labels():
    from harness.provider_capabilities import annotate_provider_row

    row = annotate_provider_row({"name": "cursor-cli", "display_name": "Cursor CLI"})
    assert row["worker_capability"] == "pilot_only"
    assert row["worker_capability_label"] == "Pilot only"
    assert "workers" in row["worker_capability_hint"].lower()


def test_product_worker_adapter_falls_back_to_cursor(monkeypatch):
    monkeypatch.setattr(
        "harness.auto_registry.keyed_agentic_providers",
        lambda: set(),
    )
    monkeypatch.setattr(
        "harness.provider_capabilities.cursor_platform_workers_ready",
        lambda env=None: True,
    )
    monkeypatch.setattr(
        "harness.swarm_adapter.resolve_bridge_swarm_adapter",
        lambda configured=None, repo_cwd="": "agentic",
    )
    from harness.swarm_worker_route import resolve_product_worker_adapter

    assert resolve_product_worker_adapter() == "cursor"


def test_product_worker_adapter_prefers_keyed_agentic(monkeypatch):
    monkeypatch.setattr(
        "harness.auto_registry.keyed_agentic_providers",
        lambda: {"openrouter"},
    )
    monkeypatch.setattr(
        "harness.provider_capabilities.cursor_platform_workers_ready",
        lambda env=None: True,
    )
    monkeypatch.setattr(
        "harness.swarm_adapter.resolve_bridge_swarm_adapter",
        lambda configured=None, repo_cwd="": "agentic",
    )
    from harness.swarm_worker_route import resolve_product_worker_adapter

    assert resolve_product_worker_adapter() == "agentic"


# ---------------------------------------------------------------------------
# Reasoning-effort support per model spec
# ---------------------------------------------------------------------------


def test_reasoning_support_codex_always():
    from harness.provider_capabilities import model_supports_reasoning_effort

    # canonical name
    assert model_supports_reasoning_effort("openai-codex", "gpt-5.6-luna") is True
    assert model_supports_reasoning_effort("openai-codex", "gpt-5.6-sol") is True
    # aliases
    assert model_supports_reasoning_effort("codex-plan", "gpt-5.6-luna") is True
    assert model_supports_reasoning_effort("chatgpt-codex", "gpt-5.6-sol") is True


def test_reasoning_support_anthropic_uses_model_check():
    from harness.provider_capabilities import model_supports_reasoning_effort

    # opus → True
    assert model_supports_reasoning_effort("anthropic", "claude-opus-4-8") is True
    # sonnet → True
    assert model_supports_reasoning_effort("anthropic", "claude-sonnet-4-5") is True
    # Bedrock sonnet → True
    assert model_supports_reasoning_effort("bedrock", "us.anthropic.claude-sonnet-4-20250514-v1:0") is True
    # haiku → False
    assert model_supports_reasoning_effort("anthropic", "claude-haiku-4-5") is False
    # non-claude → False
    assert model_supports_reasoning_effort("anthropic", "gpt-5.6-luna") is False


def test_reasoning_support_opencode_go_known_families():
    from harness.provider_capabilities import model_supports_reasoning_effort

    # GLM-5.3 has a dialect
    assert model_supports_reasoning_effort("opencode-go", "glm-5.3") is True
    # GLM-5.2 has a dialect
    assert model_supports_reasoning_effort("opencode-go", "glm-5.2") is True
    # Kimi K2 has a dialect
    assert model_supports_reasoning_effort("opencode-go", "kimi-k2.7-code") is True
    # DeepSeek-thinking has a dialect
    assert model_supports_reasoning_effort("opencode-go", "deepseek-v4-flash") is True
    # Unrecognised → False (reasoning_body_extras returns {})
    assert model_supports_reasoning_effort("opencode-go", "mimo-v2.5") is False
    assert model_supports_reasoning_effort("opencode-go", "grok-4.5") is False

    # alias resolution for "opencode_go"
    assert model_supports_reasoning_effort("opencode_go", "glm-5.3") is True


def test_reasoning_support_opencode_zen_and_openrouter():
    from harness.provider_capabilities import model_supports_reasoning_effort

    # Always True: relay passes reasoning_effort through
    assert model_supports_reasoning_effort("opencode-zen", "deepseek-v4-flash-free") is True
    assert model_supports_reasoning_effort("opencode-zen", "mimo-v2.5-free") is True
    assert model_supports_reasoning_effort("openrouter", "anthropic/claude-sonnet-4") is True
    assert model_supports_reasoning_effort("openrouter", "openai/gpt-5.6-luna") is True

    # alias resolution
    assert model_supports_reasoning_effort("opencode_zen", "deepseek-v4-flash-free") is True


def test_reasoning_support_unknown_provider_default_true():
    """Where genuinely unknowable, the user wants the knob to appear."""
    from harness.provider_capabilities import model_supports_reasoning_effort

    assert model_supports_reasoning_effort("cursor-cli", "gpt-5.6-luna") is True
    assert model_supports_reasoning_effort("nous", "model-x") is True
    assert model_supports_reasoning_effort("", "") is True
    assert model_supports_reasoning_effort("gemini", "gemini-2.5-pro") is True
