from __future__ import annotations

"""Settings + platform worker allowlist for product swarms."""


def test_visibility_maps_openrouter_to_agentic():
    from harness.swarm_worker_allowlist import adapters_from_visibility

    adapters = adapters_from_visibility(["openrouter:moonshotai/kimi-k3"])
    assert adapters == {"agentic"}


def test_visibility_maps_cursor_cli_grok_to_cursor():
    from harness.swarm_worker_allowlist import adapters_from_visibility

    adapters = adapters_from_visibility(
        ["cursor-cli:cursor-grok-4.5-high", "openrouter:moonshotai/kimi-k3"]
    )
    assert adapters == {"agentic", "cursor"}


def test_allowlist_includes_cursor_when_settings_enable_grok(monkeypatch):
    monkeypatch.setattr(
        "harness.swarm_worker_allowlist._enabled_or_visible_specs",
        lambda: [
            "openrouter:moonshotai/kimi-k3",
            "cursor-cli:cursor-grok-4.5-high",
        ],
    )
    monkeypatch.setattr(
        "harness.swarm_worker_allowlist._agentic_eligible",
        lambda: True,
    )
    monkeypatch.setattr(
        "harness.swarm_worker_allowlist._cursor_platform_ready",
        lambda: True,
    )
    monkeypatch.setattr(
        "harness.swarm_worker_allowlist._platform_locked_adapters",
        lambda: frozenset({"agentic", "cursor"}),
    )
    from harness.swarm_worker_allowlist import resolve_swarm_worker_allowlist

    out = resolve_swarm_worker_allowlist()
    assert out["allowed_adapters"] == ["agentic", "cursor"]
    assert out["prefer_plan_billed"] is False
    assert out["primary_adapter"] == "agentic"


def test_allowlist_agentic_only_when_only_agentic_enabled(monkeypatch):
    monkeypatch.setattr(
        "harness.swarm_worker_allowlist._enabled_or_visible_specs",
        lambda: ["openrouter:moonshotai/kimi-k3"],
    )
    monkeypatch.setattr(
        "harness.swarm_worker_allowlist._agentic_eligible",
        lambda: True,
    )
    monkeypatch.setattr(
        "harness.swarm_worker_allowlist._cursor_platform_ready",
        lambda: True,
    )
    monkeypatch.setattr(
        "harness.swarm_worker_allowlist._platform_locked_adapters",
        lambda: frozenset({"agentic", "cursor"}),
    )
    from harness.swarm_worker_allowlist import resolve_swarm_worker_allowlist

    out = resolve_swarm_worker_allowlist()
    assert out["allowed_adapters"] == ["agentic"]
    assert out["prefer_plan_billed"] is False


def test_allowlist_intersects_platform_lock(monkeypatch):
    monkeypatch.setattr(
        "harness.swarm_worker_allowlist._enabled_or_visible_specs",
        lambda: [
            "openrouter:moonshotai/kimi-k3",
            "cursor-cli:cursor-grok-4.5-high",
        ],
    )
    monkeypatch.setattr(
        "harness.swarm_worker_allowlist._agentic_eligible",
        lambda: True,
    )
    monkeypatch.setattr(
        "harness.swarm_worker_allowlist._cursor_platform_ready",
        lambda: True,
    )
    # Platform lock disables cursor — Settings intent alone must not unlock it.
    monkeypatch.setattr(
        "harness.swarm_worker_allowlist._platform_locked_adapters",
        lambda: frozenset({"agentic"}),
    )
    from harness.swarm_worker_allowlist import resolve_swarm_worker_allowlist

    out = resolve_swarm_worker_allowlist()
    assert out["allowed_adapters"] == ["agentic"]
    assert "cursor" not in out["allowed_adapters"]


def test_enabled_specs_keep_cursor_cli_intent_when_pilots_drop_it(monkeypatch):
    """Regression: Models enables cursor-cli Grok, but enabled_pilots() drops it
    when Cursor Agent login is unkeyed. Platform CURSOR_API_KEY workers must
    still see cursor in the allowlist so Luna can orchestrate Grok."""
    import harness.model_visibility as mv
    import harness.swarm_worker_allowlist as swa

    monkeypatch.setattr(
        mv,
        "get_enabled",
        lambda: [
            "openrouter:moonshotai/kimi-k3",
            "cursor-cli:cursor-grok-4.5-high-fast",
            "cursor-cli:composer-2.5",
        ],
    )
    monkeypatch.setattr(
        mv,
        "enabled_pilots",
        lambda: ["openrouter:moonshotai/kimi-k3"],
    )
    monkeypatch.setattr(swa, "_agentic_eligible", lambda: True)
    monkeypatch.setattr(swa, "_cursor_platform_ready", lambda: True)
    monkeypatch.setattr(
        swa,
        "_platform_locked_adapters",
        lambda: frozenset({"agentic", "cursor", "openai", "codex"}),
    )

    specs = swa._enabled_or_visible_specs()
    assert "cursor-cli:cursor-grok-4.5-high-fast" in specs
    out = swa.resolve_swarm_worker_allowlist()
    assert out["allowed_adapters"] == ["agentic", "cursor"]
    assert out["prefer_plan_billed"] is False


def test_prefer_plan_billed_true_only_when_cursor_only(monkeypatch):
    monkeypatch.setattr(
        "harness.swarm_worker_allowlist._enabled_or_visible_specs",
        lambda: ["cursor-cli:cursor-grok-4.5-high"],
    )
    monkeypatch.setattr(
        "harness.swarm_worker_allowlist._agentic_eligible",
        lambda: False,
    )
    monkeypatch.setattr(
        "harness.swarm_worker_allowlist._cursor_platform_ready",
        lambda: True,
    )
    monkeypatch.setattr(
        "harness.swarm_worker_allowlist._platform_locked_adapters",
        lambda: frozenset({"cursor"}),
    )
    from harness.swarm_worker_allowlist import resolve_swarm_worker_allowlist

    out = resolve_swarm_worker_allowlist()
    assert out["allowed_adapters"] == ["cursor"]
    assert out["prefer_plan_billed"] is True
    assert out["primary_adapter"] == "cursor"
