from __future__ import annotations

"""Settings + platform worker allowlist for product swarms."""

import pytest

_KIMI = "openrouter:moonshotai/kimi-k3"
_GROK = "cursor-cli:cursor-grok-4.5-high"


@pytest.mark.parametrize(
    ("specs", "locked"),
    [
        pytest.param([_KIMI, _GROK], {"agentic", "cursor"}, id="grok_enabled_cursor_unlocked"),
        pytest.param([_KIMI], {"agentic", "cursor"}, id="only_agentic_enabled"),
        pytest.param([_KIMI, _GROK], {"agentic"}, id="platform_lock_excludes_cursor"),
    ],
)
def test_allowlist_is_agentic_only_when_agentic_is_eligible(monkeypatch, specs, locked):
    monkeypatch.setattr(
        "harness.swarm_worker_allowlist._enabled_or_visible_specs",
        lambda: list(specs),
    )
    monkeypatch.setattr(
        "harness.swarm_worker_allowlist._agentic_eligible",
        lambda: True,
    )
    monkeypatch.setattr(
        "harness.swarm_worker_allowlist._platform_locked_adapters",
        lambda: frozenset(locked),
    )
    from harness.swarm_worker_allowlist import resolve_swarm_worker_allowlist

    out = resolve_swarm_worker_allowlist()
    assert out["allowed_adapters"] == ["agentic"]
    assert "cursor" not in out["allowed_adapters"]
    assert out["prefer_plan_billed"] is False
    assert out["primary_adapter"] == "agentic"


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
    monkeypatch.setattr(
        swa,
        "_platform_locked_adapters",
        lambda: frozenset({"agentic", "cursor", "openai", "codex"}),
    )

    specs = swa._enabled_or_visible_specs()
    assert "cursor-cli:cursor-grok-4.5-high-fast" in specs
    out = swa.resolve_swarm_worker_allowlist()
    assert out["allowed_adapters"] == ["agentic"]
    assert out["prefer_plan_billed"] is False


def test_enabled_specs_do_not_union_full_keyed_catalog_when_curated(monkeypatch):
    """Models toggles are the only discretionary allowlist.

    enabled_pilots() falls back to every keyed vendor model when a curated
    spec misses avail_set. Unioning that list is how toggled-off glm-5.2
    won implement auto-route.
    """
    import harness.model_visibility as mv
    import harness.swarm_worker_allowlist as swa

    monkeypatch.setattr(
        mv,
        "get_enabled",
        lambda: [
            "openai-codex:gpt-5.6-luna",
            "openrouter:google/gemini-3.8-flash",
            "opencode-go:deepseek-v4-flash",
        ],
    )
    monkeypatch.setattr(
        mv,
        "enabled_pilots",
        lambda: [
            "openai-codex:gpt-5.6-luna",
            "openrouter:google/gemini-3.8-flash",
            "opencode-go:deepseek-v4-flash",
            "zai:glm-5.2",
            "openrouter:z-ai/glm-5.2",
        ],
    )
    specs = swa._enabled_or_visible_specs()
    blob = " ".join(specs).lower()
    assert "glm-5.2" not in blob
    assert "glm-5-2" not in blob
    assert "openai-codex:gpt-5.6-luna" in specs
    ids = swa.allowed_model_ids_from_specs(specs)
    id_blob = " ".join(ids).lower()
    assert "glm-5.2" not in id_blob
    assert "glm-5-2" not in id_blob


def test_singleton_enabled_spec_maps_luna_not_gpt53(monkeypatch):
    monkeypatch.setattr(
        "harness.swarm_model_pin._registry_rows",
        lambda **_: [{
            "id": "agentic/openai-codex/gpt-5.6-luna",
            "adapter": "agentic",
            "adapter_model_name": "gpt-5.6-luna",
            "payload_defaults": {"provider": "openai-codex"},
        }],
    )
    monkeypatch.setattr(
        "harness.auto_registry.keyed_agentic_providers",
        lambda: {"openai-codex"},
    )
    from harness.swarm_worker_allowlist import allowed_model_ids_from_specs

    ids = allowed_model_ids_from_specs(["openai-codex:gpt-5.6-luna"])
    blob = " ".join(ids).lower()
    assert "gpt-5.6-luna" in blob
    assert "gpt-5-3" not in blob
    assert "gpt-5.3" not in blob


def test_cary_enabled_subset_never_aliases_mimo(monkeypatch):
    """Live Go/OR catalogs list MIMO; Settings toggles must not expand to it."""
    rows = []
    for provider, model in (
        ("opencode-go", "deepseek-v4-flash"),
        ("openai-codex", "gpt-6-astra"),
        ("openai-codex", "gpt-5.6-sol"),
        ("openai-codex", "gpt-5.6-luna"),
        ("openrouter", "google/gemini-3.8-flash"),
        ("openrouter", "google/gemini-3.7-flash"),
    ):
        rows.append({
            "id": f"agentic/{provider}/{model}",
            "adapter": "agentic",
            "adapter_model_name": model,
            "payload_defaults": {"provider": provider},
        })
    monkeypatch.setattr(
        "harness.swarm_model_pin._registry_rows", lambda **_: rows,
    )
    monkeypatch.setattr(
        "harness.auto_registry.keyed_agentic_providers",
        lambda: {"opencode-go", "openai-codex", "openrouter"},
    )
    from harness.swarm_worker_allowlist import allowed_model_ids_from_specs

    ids = allowed_model_ids_from_specs([
        "opencode-go:deepseek-v4-flash",
        "openai-codex:gpt-6-astra",
        "openai-codex:gpt-5.6-sol",
        "openai-codex:gpt-5.6-luna",
        "openrouter:google/gemini-3.8-flash",
        "openrouter:google/gemini-3.7-flash",
        "local:managed/qwen-test",
        "opencode-go:deepseek-flash",
    ])
    blob = " ".join(ids).lower()
    assert "deepseek-v4-flash" in blob
    assert "gemini-3.8-flash" in blob
    assert "mimo" not in blob
    assert "xiaomi" not in blob


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
        "harness.swarm_worker_allowlist._platform_locked_adapters",
        lambda: frozenset({"cursor"}),
    )
    from harness.swarm_worker_allowlist import resolve_swarm_worker_allowlist

    out = resolve_swarm_worker_allowlist()
    assert out["allowed_adapters"] == []
    assert out["prefer_plan_billed"] is False
    assert out["primary_adapter"] == "agentic"
