from __future__ import annotations

"""Tests for the auto-registry feature.

Hermetic tests (no network, monkeypatch key presence and discovery) that verify:
- With only gemini+anthropic keys present, registry contains only those providers
- A disconnected provider is dropped on resync
- Pre-existing non-agentic entries are preserved
"""

import json
import os
import tempfile
from unittest.mock import patch


def test_sync_with_gemini_and_anthropic_only(monkeypatch, tmp_path):
    """With only gemini+anthropic keys, registry should contain only those providers.

    HARNESS_LIVE_PRICES=0 so this hermetic test pins static template prices
    (live overlay is covered separately).
    """
    models_path = tmp_path / "models.json"
    monkeypatch.setenv("PUPPETMASTER_MODELS_PATH", str(models_path))
    monkeypatch.setenv("HARNESS_LIVE_PRICES", "0")
    
    # Mock provider keys: only gemini and anthropic
    def mock_get_provider_key(provider):
        if provider.name in ("gemini", "anthropic"):
            return "fake-key-" + provider.name
        return None
    
    # Mock disconnected set: empty
    def mock_get_disconnected():
        return set()
    
    # Mock model discovery: return empty to force fallback to curated
    def mock_fetch_models(provider, key, force=False):
        return []
    
    with patch("harness.registry_wizard.get_provider_key", mock_get_provider_key), \
         patch("harness.keys.get_disconnected", mock_get_disconnected), \
         patch("harness.model_fetch.fetch_models", mock_fetch_models):
        
        from harness.auto_registry import sync_agentic_registry
        result = sync_agentic_registry()
        
        assert result["synced"] is True
        assert set(result["providers"]) == {"gemini", "anthropic"}
        assert result["models_count"] > 0
        
        # Read the written models.json
        assert models_path.exists()
        with open(models_path) as f:
            data = json.load(f)
        
        models = data.get("models", [])
        assert len(models) > 0
        
        # All models should be agentic adapter
        for model in models:
            assert model["adapter"] == "agentic"
        
        # Check providers present
        providers_in_models = set()
        for model in models:
            provider = model.get("payload_defaults", {}).get("provider")
            if provider:
                providers_in_models.add(provider)
        
        assert providers_in_models == {"gemini", "anthropic"}
        
        # No openai-api models should be present
        for model in models:
            provider = model.get("payload_defaults", {}).get("provider")
            assert provider != "openai-api"


def test_disconnected_provider_is_dropped(monkeypatch, tmp_path):
    """A disconnected provider should not appear in the registry even if it has a key."""
    models_path = tmp_path / "models.json"
    monkeypatch.setenv("PUPPETMASTER_MODELS_PATH", str(models_path))
    monkeypatch.setenv("HARNESS_LIVE_PRICES", "0")
    
    # Mock provider keys: gemini, anthropic, and openai all have keys
    def mock_get_provider_key(provider):
        if provider.name in ("gemini", "anthropic", "openai"):
            return "fake-key-" + provider.name
        return None
    
    # Mock disconnected set: openai is disconnected
    def mock_get_disconnected():
        return {"openai"}
    
    # Mock model discovery: return empty to force fallback to curated
    def mock_fetch_models(provider, key, force=False):
        return []
    
    with patch("harness.registry_wizard.get_provider_key", mock_get_provider_key), \
         patch("harness.keys.get_disconnected", mock_get_disconnected), \
         patch("harness.model_fetch.fetch_models", mock_fetch_models):
        
        from harness.auto_registry import sync_agentic_registry
        result = sync_agentic_registry()
        
        assert result["synced"] is True
        # Should only have gemini and anthropic, NOT openai-api
        assert set(result["providers"]) == {"gemini", "anthropic"}
        
        # Read the written models.json
        with open(models_path) as f:
            data = json.load(f)
        
        models = data.get("models", [])
        
        # Verify no openai-api models
        for model in models:
            provider = model.get("payload_defaults", {}).get("provider")
            assert provider != "openai-api"


def test_sync_with_opencode_go_only(monkeypatch, tmp_path):
    """Go-only auth must seed agentic worker rows (gpt-5.6-luna, …) from curated."""
    models_path = tmp_path / "models.json"
    monkeypatch.setenv("PUPPETMASTER_MODELS_PATH", str(models_path))
    monkeypatch.setenv("HARNESS_LIVE_PRICES", "0")

    def mock_get_provider_key(provider):
        if provider.name == "opencode-go":
            return "fake-key-opencode-go"
        return None

    with patch("harness.registry_wizard.get_provider_key", mock_get_provider_key), \
         patch("harness.keys.get_disconnected", lambda: set()), \
         patch("harness.model_fetch.fetch_models", lambda *_a, **_k: []), \
         patch("harness.auto_registry._enabled_picker_models", lambda *_a, **_k: []):
        from harness.auto_registry import sync_agentic_registry

        result = sync_agentic_registry()

    assert result["synced"] is True
    assert result["providers"] == ["opencode-go"]
    assert result["models_count"] > 0
    with open(models_path, encoding="utf-8") as f:
        data = json.load(f)
    ids = {m["id"] for m in data["models"] if m.get("adapter") == "agentic"}
    assert "agentic/gpt-5.6-luna" in ids
    assert "agentic/deepseek-v4-flash" in ids
    for model in data["models"]:
        if model.get("adapter") == "agentic":
            assert model.get("payload_defaults", {}).get("provider") == "opencode-go"


def test_sync_with_openai_codex_only(monkeypatch, tmp_path):
    """Codex-only OAuth must seed plan-billed agentic worker rows for swarms."""
    models_path = tmp_path / "models.json"
    monkeypatch.setenv("PUPPETMASTER_MODELS_PATH", str(models_path))
    monkeypatch.setenv("HARNESS_LIVE_PRICES", "0")

    def mock_get_provider_key(provider):
        if provider.name == "openai-codex":
            return "fake-codex-oauth-token"
        return None

    with patch("harness.registry_wizard.get_provider_key", mock_get_provider_key), \
         patch("harness.keys.get_disconnected", lambda: set()), \
         patch("harness.model_fetch.fetch_models", lambda *_a, **_k: []), \
         patch("harness.auto_registry._enabled_picker_models", lambda *_a, **_k: []):
        from harness.auto_registry import sync_agentic_registry

        result = sync_agentic_registry()

    assert result["synced"] is True
    assert result["providers"] == ["openai-codex"]
    assert result["models_count"] > 0
    with open(models_path, encoding="utf-8") as f:
        data = json.load(f)
    agentic = [m for m in data["models"] if m.get("adapter") == "agentic"]
    ids = {m["id"] for m in agentic}
    assert "agentic/openai-codex/gpt-5.6-luna" in ids
    assert "agentic/openai-codex/gpt-5.6-sol" in ids
    for model in agentic:
        defaults = model.get("payload_defaults") or {}
        assert defaults.get("provider") == "openai-codex"
        assert model.get("billing") == "plan"
        assert defaults.get("model") or model.get("adapter_model_name")
        # Wire id stays bare (Responses API); registry id is namespaced.
        assert "/" not in str(model.get("adapter_model_name") or "")


def test_sync_zai_coding_plan_is_plan_billed(monkeypatch, tmp_path):
    """Direct Z.AI Coding Plan rows are subscription-billed, not cash API."""
    models_path = tmp_path / "models.json"
    monkeypatch.setenv("PUPPETMASTER_MODELS_PATH", str(models_path))
    monkeypatch.setenv("HARNESS_LIVE_PRICES", "0")
    monkeypatch.delenv("ZAI_BASE_URL", raising=False)
    monkeypatch.delenv("GLM_BASE_URL", raising=False)

    def mock_get_provider_key(provider):
        if provider.name == "zai":
            return "fake-key-zai"
        return None

    with patch("harness.registry_wizard.get_provider_key", mock_get_provider_key), \
         patch("harness.keys.get_disconnected", lambda: set()), \
         patch("harness.model_fetch.fetch_models", lambda *_a, **_k: []), \
         patch("harness.auto_registry._enabled_picker_models", lambda *_a, **_k: []):
        from harness.auto_registry import sync_agentic_registry

        result = sync_agentic_registry()

    assert result["synced"] is True
    assert result["providers"] == ["zai"]
    with open(models_path, encoding="utf-8") as f:
        data = json.load(f)
    agentic = [m for m in data["models"] if m.get("adapter") == "agentic"]
    assert agentic
    for model in agentic:
        assert (model.get("payload_defaults") or {}).get("provider") == "zai"
        assert model.get("billing") == "plan"
    ids = {m["id"] for m in agentic}
    assert "agentic/glm-5.2" in ids
    assert "agentic/glm-5.3" in ids


def test_zai_discovery_backfills_glm_53_when_listing_lags(monkeypatch, tmp_path):
    models_path = tmp_path / "models.json"
    monkeypatch.setenv("PUPPETMASTER_MODELS_PATH", str(models_path))
    monkeypatch.setenv("HARNESS_LIVE_PRICES", "0")

    live = [
        "glm-5.2", "glm-4.7-flash", "glm-4.5", "glm-4.5-air",
        "glm-4.6", "glm-4.7", "glm-5.1",
    ]

    def mock_get_provider_key(provider):
        return "fake-zai" if provider.name == "zai" else None

    with patch("harness.registry_wizard.get_provider_key", mock_get_provider_key), \
         patch("harness.keys.get_disconnected", lambda: set()), \
         patch("harness.model_fetch.fetch_models", lambda *_a, **_k: list(live)), \
         patch("harness.auto_registry._enabled_picker_models", lambda *_a, **_k: []):
        from harness.auto_registry import sync_agentic_registry
        sync_agentic_registry()

    ids = {m["id"] for m in json.loads(models_path.read_text())["models"]}
    assert "agentic/glm-5.2" in ids
    assert "agentic/glm-5.3" in ids


def test_sync_with_nous_minimax_nvidia(monkeypatch, tmp_path):
    """HTTP pilots that were previously catalog-orphans must seed agentic rows."""
    models_path = tmp_path / "models.json"
    monkeypatch.setenv("PUPPETMASTER_MODELS_PATH", str(models_path))
    monkeypatch.setenv("HARNESS_LIVE_PRICES", "0")

    def mock_get_provider_key(provider):
        if provider.name in ("nous", "minimax", "nvidia"):
            return "fake-key-" + provider.name
        return None

    with patch("harness.registry_wizard.get_provider_key", mock_get_provider_key), \
         patch("harness.keys.get_disconnected", lambda: set()), \
         patch("harness.model_fetch.fetch_models", lambda *_a, **_k: []), \
         patch("harness.auto_registry._enabled_picker_models", lambda *_a, **_k: []):
        from harness.auto_registry import sync_agentic_registry

        result = sync_agentic_registry()

    assert result["synced"] is True
    assert set(result["providers"]) == {"nous", "minimax", "nvidia"}
    with open(models_path, encoding="utf-8") as f:
        data = json.load(f)
    agentic = [m for m in data["models"] if m.get("adapter") == "agentic"]
    providers = {
        (m.get("payload_defaults") or {}).get("provider") for m in agentic
    }
    assert providers == {"nous", "minimax", "nvidia"}
    ids = {m["id"] for m in agentic}
    assert any("Hermes" in i or "hermes" in i.lower() for i in ids)
    assert any("MiniMax" in i or "minimax" in i.lower() for i in ids)


def test_preserves_non_agentic_entries(monkeypatch, tmp_path):
    """Pre-existing non-agentic entries should be preserved during sync."""
    models_path = tmp_path / "models.json"
    monkeypatch.setenv("PUPPETMASTER_MODELS_PATH", str(models_path))
    monkeypatch.setenv("HARNESS_LIVE_PRICES", "0")
    
    # Create existing models.json with mixed agentic and non-agentic entries
    existing_data = {
        "models": [
            {
                "id": "cursor/composer-2-5",
                "adapter": "cursor",
                "adapter_model_name": "composer-2.5",
                "capability_score": 55,
                "tags": ["cursor", "cheap"]
            },
            {
                "id": "claude-code/haiku-4-5",
                "adapter": "claude-code",
                "adapter_model_name": "claude-haiku-4-5",
                "capability_score": 55,
                "tags": ["claude-code"]
            },
            {
                "id": "agentic/old-model",
                "adapter": "agentic",
                "adapter_model_name": "old-model",
                "capability_score": 50,
                "payload_defaults": {"provider": "old-provider"}
            }
        ]
    }
    
    with open(models_path, 'w') as f:
        json.dump(existing_data, f)
    
    # Mock provider keys: only gemini
    def mock_get_provider_key(provider):
        if provider.name == "gemini":
            return "fake-key-gemini"
        return None
    
    # Mock disconnected set: empty
    def mock_get_disconnected():
        return set()
    
    # Mock model discovery: return empty to force fallback to curated
    def mock_fetch_models(provider, key, force=False):
        return []
    
    with patch("harness.registry_wizard.get_provider_key", mock_get_provider_key), \
         patch("harness.keys.get_disconnected", mock_get_disconnected), \
         patch("harness.model_fetch.fetch_models", mock_fetch_models):
        
        from harness.auto_registry import sync_agentic_registry
        result = sync_agentic_registry()
        
        assert result["synced"] is True
        
        # Read the written models.json
        with open(models_path) as f:
            data = json.load(f)
        
        models = data.get("models", [])
        
        # Should have the cursor and claude-code entries preserved
        cursor_models = [m for m in models if m.get("adapter") == "cursor"]
        assert len(cursor_models) == 1
        assert cursor_models[0]["id"] == "cursor/composer-2-5"
        
        claude_models = [m for m in models if m.get("adapter") == "claude-code"]
        assert len(claude_models) == 1
        assert claude_models[0]["id"] == "claude-code/haiku-4-5"
        
        # Should have new agentic entries for gemini
        agentic_models = [m for m in models if m.get("adapter") == "agentic"]
        assert len(agentic_models) > 0
        
        # Old agentic entry should be replaced
        old_model_ids = [m["id"] for m in agentic_models]
        assert "agentic/old-model" not in old_model_ids
        
        # Should have gemini models
        gemini_models = [
            m for m in agentic_models 
            if m.get("payload_defaults", {}).get("provider") == "gemini"
        ]
        assert len(gemini_models) > 0


def test_idempotent_sync(monkeypatch, tmp_path):
    """Running sync multiple times should be idempotent."""
    models_path = tmp_path / "models.json"
    monkeypatch.setenv("PUPPETMASTER_MODELS_PATH", str(models_path))
    monkeypatch.setenv("HARNESS_LIVE_PRICES", "0")
    
    # Mock provider keys: only anthropic
    def mock_get_provider_key(provider):
        if provider.name == "anthropic":
            return "fake-key-anthropic"
        return None
    
    # Mock disconnected set: empty
    def mock_get_disconnected():
        return set()
    
    # Mock model discovery: return empty to force fallback to curated
    def mock_fetch_models(provider, key, force=False):
        return []
    
    with patch("harness.registry_wizard.get_provider_key", mock_get_provider_key), \
         patch("harness.keys.get_disconnected", mock_get_disconnected), \
         patch("harness.model_fetch.fetch_models", mock_fetch_models):
        
        from harness.auto_registry import sync_agentic_registry
        
        # First sync
        result1 = sync_agentic_registry()
        assert result1["synced"] is True
        
        with open(models_path) as f:
            data1 = json.load(f)
        
        # Second sync
        result2 = sync_agentic_registry()
        assert result2["synced"] is True
        
        with open(models_path) as f:
            data2 = json.load(f)
        
        # Results should be the same
        assert data1 == data2


def test_no_keys_no_agentic_entries(monkeypatch, tmp_path):
    """With no provider keys, no agentic entries should be created."""
    models_path = tmp_path / "models.json"
    monkeypatch.setenv("PUPPETMASTER_MODELS_PATH", str(models_path))
    monkeypatch.setenv("HARNESS_LIVE_PRICES", "0")
    
    # Create existing models.json with a non-agentic entry
    existing_data = {
        "models": [
            {
                "id": "cursor/composer-2-5",
                "adapter": "cursor",
                "adapter_model_name": "composer-2.5",
                "capability_score": 55,
            }
        ]
    }
    
    with open(models_path, 'w') as f:
        json.dump(existing_data, f)
    
    # Mock provider keys: none
    def mock_get_provider_key(provider):
        return None
    
    # Mock disconnected set: empty
    def mock_get_disconnected():
        return set()
    
    with patch("harness.registry_wizard.get_provider_key", mock_get_provider_key), \
         patch("harness.keys.get_disconnected", mock_get_disconnected):
        
        from harness.auto_registry import sync_agentic_registry
        result = sync_agentic_registry()
        
        assert result["synced"] is True
        assert result["providers"] == []
        assert result["models_count"] == 0
        
        # Read the written models.json
        with open(models_path) as f:
            data = json.load(f)
        
        models = data.get("models", [])
        
        # Should still have the cursor entry
        assert len(models) == 1
        assert models[0]["id"] == "cursor/composer-2-5"


def test_sync_safe_never_raises(monkeypatch, tmp_path):
    """sync_agentic_registry_safe should never raise, even on errors."""
    models_path = tmp_path / "models.json"
    monkeypatch.setenv("PUPPETMASTER_MODELS_PATH", str(models_path))
    
    # Make models_path a directory instead of a file to force an error
    models_path.mkdir(parents=True, exist_ok=True)
    
    # This should not raise
    from harness.auto_registry import sync_agentic_registry_safe
    sync_agentic_registry_safe()  # Should complete without raising


def test_import_module():
    """Verify the module can be imported."""
    import harness.auto_registry
    assert hasattr(harness.auto_registry, 'sync_agentic_registry')
    assert hasattr(harness.auto_registry, 'sync_agentic_registry_safe')


def test_build_agentic_spec_uses_live_prices(monkeypatch):
    """When pmharness.registry.price returns usable rates, overlay them."""
    monkeypatch.delenv("HARNESS_LIVE_PRICES", raising=False)
    from harness.auto_registry import _AGENTIC_TEMPLATES, _build_agentic_spec

    live_in, live_out = 9.9, 19.9

    def fake_price(name):
        return (live_in, live_out)

    with patch("pmharness.registry.price", fake_price):
        spec = _build_agentic_spec("anthropic", "claude-sonnet-4-5", "balanced", "claude-sonnet-4-5")

    assert spec["input_per_mtok_usd"] == live_in
    assert spec["output_per_mtok_usd"] == live_out
    # Static capability/context preserved; tools/agentic tags are always stamped
    # so Puppetmaster tool-loop roles can select these models.
    template = _AGENTIC_TEMPLATES["anthropic"]["balanced"]
    assert spec["capability_score"] == template[0]
    assert spec["context_window"] == template[3]
    for required in ("tools", "agentic"):
        assert required in spec["tags"]
    for tag in template[4]:
        assert tag in spec["tags"]


def test_build_agentic_spec_keeps_static_on_price_miss(monkeypatch):
    """(None, None) or raised price() keeps static template numbers."""
    monkeypatch.delenv("HARNESS_LIVE_PRICES", raising=False)
    from harness.auto_registry import _AGENTIC_TEMPLATES, _build_agentic_spec

    template = _AGENTIC_TEMPLATES["anthropic"]["balanced"]

    with patch("pmharness.registry.price", lambda name: (None, None)):
        spec = _build_agentic_spec("anthropic", "claude-sonnet-4-5", "balanced", "claude-sonnet-4-5")
    assert spec["input_per_mtok_usd"] == template[1]
    assert spec["output_per_mtok_usd"] == template[2]

    def boom(name):
        raise RuntimeError("no network")

    with patch("pmharness.registry.price", boom):
        spec2 = _build_agentic_spec("anthropic", "claude-sonnet-4-5", "balanced", "claude-sonnet-4-5")
    assert spec2["input_per_mtok_usd"] == template[1]
    assert spec2["output_per_mtok_usd"] == template[2]


def test_build_agentic_spec_live_prices_kill_switch(monkeypatch):
    """HARNESS_LIVE_PRICES=0 skips the overlay entirely."""
    monkeypatch.setenv("HARNESS_LIVE_PRICES", "0")
    from harness.auto_registry import _AGENTIC_TEMPLATES, _build_agentic_spec

    template = _AGENTIC_TEMPLATES["anthropic"]["balanced"]
    called = []

    def fake_price(name):
        called.append(name)
        return (9.9, 19.9)

    with patch("pmharness.registry.price", fake_price):
        spec = _build_agentic_spec("anthropic", "claude-sonnet-4-5", "balanced", "claude-sonnet-4-5")

    assert called == []
    assert spec["input_per_mtok_usd"] == template[1]
    assert spec["output_per_mtok_usd"] == template[2]


def test_placeholder_bedrock_key_excluded_from_sync(monkeypatch, tmp_path):
    """A doctor/placeholder bedrock token must not enter live_providers."""
    models_path = tmp_path / "models.json"
    monkeypatch.setenv("PUPPETMASTER_MODELS_PATH", str(models_path))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("HARNESS_LIVE_PRICES", "0")
    for ev in (
        "AWS_BEARER_TOKEN_BEDROCK", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
    ):
        monkeypatch.delenv(ev, raising=False)

    from harness.keys import set_bedrock_credentials, set_api_key
    from harness.auto_registry import sync_agentic_registry

    set_api_key("anthropic", "sk-ant-live-fakekey1234")
    set_bedrock_credentials({
        "AWS_BEARER_TOKEN_BEDROCK": "doctor-bearer-token-1",
    })

    def mock_fetch_models(provider, key, force=False):
        return []

    with patch("harness.model_fetch.fetch_models", mock_fetch_models):
        result = sync_agentic_registry()

    assert result["synced"] is True
    assert "bedrock" not in result["providers"]
    assert "anthropic" in result["providers"]

    with open(models_path, encoding="utf-8") as f:
        data = json.load(f)
    for model in data.get("models", []):
        provider = model.get("payload_defaults", {}).get("provider")
        assert provider != "bedrock"


def test_disconnected_bedrock_excluded_from_sync(monkeypatch, tmp_path):
    """Disconnected bedrock stays out of sync even with a live-looking keyfile."""
    models_path = tmp_path / "models.json"
    monkeypatch.setenv("PUPPETMASTER_MODELS_PATH", str(models_path))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("HARNESS_LIVE_PRICES", "0")
    for ev in (
        "AWS_BEARER_TOKEN_BEDROCK", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
    ):
        monkeypatch.delenv(ev, raising=False)

    from harness.keys import set_bedrock_credentials, set_provider_enabled
    from harness.auto_registry import sync_agentic_registry

    set_bedrock_credentials({
        "AWS_BEARER_TOKEN_BEDROCK": "live-bedrock-bearer-sync1",
    })
    set_provider_enabled("bedrock", False)

    def mock_fetch_models(provider, key, force=False):
        return []

    with patch("harness.model_fetch.fetch_models", mock_fetch_models):
        result = sync_agentic_registry()

    assert result["synced"] is True
    assert "bedrock" not in result["providers"]


def test_seed_catalog_filters_marionette_unconfigured(monkeypatch, tmp_path):
    """Seed path must not add bedrock when Marionette considers it unconfigured."""
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("PUPPETMASTER_MODELS_PATH", str(tmp_path / "models.json"))
    for ev in (
        "AWS_BEARER_TOKEN_BEDROCK", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
    ):
        monkeypatch.delenv(ev, raising=False)

    from harness.keys import set_bedrock_credentials, mark_disconnected
    from harness.server import _marionette_allowed_agentic_providers

    # Puppetmaster sniffs bedrock (e.g. ~/.aws); Marionette has only a placeholder.
    set_bedrock_credentials({
        "AWS_BEARER_TOKEN_BEDROCK": "doctor-bearer-token-1",
    })
    allowed = _marionette_allowed_agentic_providers({"bedrock", "anthropic"})
    assert "bedrock" not in allowed

    # Real key but explicitly disconnected — still excluded.
    set_bedrock_credentials({
        "AWS_BEARER_TOKEN_BEDROCK": "live-bedrock-bearer-seed1",
    })
    mark_disconnected("bedrock")
    from harness.keys import scrub_provider_env
    scrub_provider_env("bedrock")
    allowed2 = _marionette_allowed_agentic_providers({"bedrock", "anthropic"})
    assert "bedrock" not in allowed2


def test_sync_includes_codex_oauth_but_skips_cursor_cli(monkeypatch, tmp_path):
    """Codex OAuth is a first-class agentic worker; Cursor CLI stays wave-2.

    OpenRouter + Codex both stamp agentic rows; cursor-cli still does not.
    """
    models_path = tmp_path / "models.json"
    monkeypatch.setenv("PUPPETMASTER_MODELS_PATH", str(models_path))
    monkeypatch.setenv("HARNESS_LIVE_PRICES", "0")

    def mock_get_provider_key(provider):
        if provider.name in ("openrouter", "openai-codex", "cursor-cli"):
            return f"fake-key-{provider.name}"
        return None

    def mock_fetch_models(provider, key, force=False):
        return []

    with patch("harness.registry_wizard.get_provider_key", mock_get_provider_key), \
         patch("harness.keys.get_disconnected", lambda: set()), \
         patch("harness.model_fetch.fetch_models", mock_fetch_models), \
         patch("harness.auto_registry._enabled_picker_models", lambda *_a, **_k: []):
        from harness.auto_registry import sync_agentic_registry
        result = sync_agentic_registry()

    assert result["synced"] is True
    assert "openrouter" in result["providers"]
    assert "openai-codex" in result["providers"]
    assert "cursor-cli" not in result["providers"]

    data = json.loads(models_path.read_text())
    providers = {
        (model.get("payload_defaults") or {}).get("provider")
        for model in data.get("models", [])
        if model.get("adapter") == "agentic"
    }
    assert "openai-codex" in providers
    assert "cursor-cli" not in providers
    for model in data.get("models", []):
        assert model.get("adapter") == "agentic"
        assert "tools" in (model.get("tags") or [])
        assert "agentic" in (model.get("tags") or [])


def test_reconcile_restores_shared_non_agentic(monkeypatch, tmp_path):
    """Agentic-only marionette registry regains plan peers from shared PM."""
    home = tmp_path / "home"
    pm = home / ".puppetmaster"
    mh = home / ".pmharness"
    pm.mkdir(parents=True)
    mh.mkdir(parents=True)
    # Cover Unix ($HOME) and Windows (USERPROFILE) Path.home() roots.
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    shared = pm / "models.json"
    dest = mh / "marionette-models.json"
    shared.write_text(json.dumps({
        "models": [
            {"id": "cursor/composer-2-5", "adapter": "cursor", "capability_score": 55},
            {"id": "agentic/stale", "adapter": "agentic", "capability_score": 1},
        ]
    }))
    dest.write_text(json.dumps({
        "models": [
            {
                "id": "agentic/z-ai/glm-5.2",
                "adapter": "agentic",
                "capability_score": 86,
                "payload_defaults": {"provider": "openrouter"},
                "tags": ["tools", "agentic"],
            }
        ]
    }))
    monkeypatch.setenv("PUPPETMASTER_MODELS_PATH", str(dest))

    from harness.marionette_registry import reconcile_shared_models
    report = reconcile_shared_models()
    assert report.get("merged") == 1
    data = json.loads(dest.read_text())
    adapters = {m["adapter"] for m in data["models"]}
    assert adapters == {"cursor", "agentic"}
    assert any(m["id"] == "agentic/z-ai/glm-5.2" for m in data["models"])
    assert not any(m["id"] == "agentic/stale" for m in data["models"])


def test_kimi_k3_static_economics_match_marketplace():
    from harness.auto_registry import _KNOWN_MODEL_SPECS

    score, pin, pout, ctx, tags = _KNOWN_MODEL_SPECS["moonshotai/kimi-k3"]
    assert score == 98
    assert pin == 3.0
    assert pout == 15.0
    assert ctx == 1_000_000
    assert "frontier" in tags


def test_openrouter_picker_does_not_union_curated_ladder(monkeypatch, tmp_path):
    models_path = tmp_path / "models.json"
    monkeypatch.setenv("PUPPETMASTER_MODELS_PATH", str(models_path))
    monkeypatch.setenv("HARNESS_LIVE_PRICES", "0")

    def mock_get_provider_key(provider):
        return "fake-openrouter" if provider.name == "openrouter" else None

    def mock_enabled(_name):
        return ["anthropic/claude-opus-4.8"] if _name == "openrouter" else []

    def mock_fetch_models(provider, key, force=False):
        return []

    with patch("harness.registry_wizard.get_provider_key", mock_get_provider_key), \
         patch("harness.keys.get_disconnected", lambda: set()), \
         patch("harness.model_fetch.fetch_models", mock_fetch_models), \
         patch("harness.auto_registry._enabled_picker_models", mock_enabled):
        from harness.auto_registry import sync_agentic_registry
        result = sync_agentic_registry()

    assert result["synced"] is True
    data = json.loads(models_path.read_text())
    ids = {m["id"] for m in data["models"]}
    assert ids == {"agentic/anthropic/claude-opus-4.8"}
    opus = next(m for m in data["models"] if m["id"] == "agentic/anthropic/claude-opus-4.8")
    assert opus["payload_defaults"]["provider"] == "openrouter"
    assert "tools" in opus["tags"]


def test_openrouter_discovery_intersects_curated_with_live(monkeypatch, tmp_path):
    models_path = tmp_path / "models.json"
    monkeypatch.setenv("PUPPETMASTER_MODELS_PATH", str(models_path))
    monkeypatch.setenv("HARNESS_LIVE_PRICES", "0")

    def mock_get_provider_key(provider):
        return "fake-openrouter" if provider.name == "openrouter" else None

    def mock_fetch_models(provider, key, force=False):
        return ["anthropic/claude-opus-4.8", "z-ai/glm-5.2"]

    with patch("harness.registry_wizard.get_provider_key", mock_get_provider_key), \
         patch("harness.keys.get_disconnected", lambda: set()), \
         patch("harness.model_fetch.fetch_models", mock_fetch_models), \
         patch("harness.auto_registry._enabled_picker_models", lambda _name: []):
        from harness.auto_registry import sync_agentic_registry
        result = sync_agentic_registry()

    data = json.loads(models_path.read_text())
    ids = {m["id"] for m in data["models"]}
    assert ids == {"agentic/z-ai/glm-5.2"}
    assert "agentic/moonshotai/kimi-k3" not in ids
    assert "agentic/deepseek/deepseek-v4-flash" not in ids
    assert "agentic/z-ai/glm-5.3" not in ids


def test_openrouter_discovery_promotes_newer_family_version(monkeypatch, tmp_path):
    """Live glm-5.3 joins Autopilot as a sibling of curated glm-5.2; MIMO does not."""
    models_path = tmp_path / "models.json"
    monkeypatch.setenv("PUPPETMASTER_MODELS_PATH", str(models_path))
    monkeypatch.setenv("HARNESS_LIVE_PRICES", "0")

    live = [
        "z-ai/glm-5.2",
        "z-ai/glm-5.3",
        "xiaomi/mimo-v2-flash",
        "anthropic/claude-opus-4.8",
    ]

    def mock_get_provider_key(provider):
        return "fake-openrouter" if provider.name == "openrouter" else None

    with patch("harness.registry_wizard.get_provider_key", mock_get_provider_key), \
         patch("harness.keys.get_disconnected", lambda: set()), \
         patch("harness.model_fetch.fetch_models", lambda *_a, **_k: list(live)), \
         patch("harness.auto_registry._enabled_picker_models", lambda _name: []):
        from harness.auto_registry import sync_agentic_registry
        sync_agentic_registry()

    ids = {m["id"] for m in json.loads(models_path.read_text())["models"]}
    assert "agentic/z-ai/glm-5.2" in ids
    assert "agentic/z-ai/glm-5.3" in ids
    assert not any("mimo" in mid for mid in ids)


def test_openrouter_live_extras_do_not_inject_mimo(monkeypatch, tmp_path):
    models_path = tmp_path / "models.json"
    monkeypatch.setenv("PUPPETMASTER_MODELS_PATH", str(models_path))
    monkeypatch.setenv("HARNESS_LIVE_PRICES", "0")

    live = [
        "xiaomi/mimo-v2-flash",
        "xiaomi/mimo-v2-pro",
        "minimax/minimax-m3",
        "moonshotai/kimi-k3",
        "deepseek/deepseek-v4-pro",
    ]

    def mock_get_provider_key(provider):
        return "fake-openrouter" if provider.name == "openrouter" else None

    with patch("harness.registry_wizard.get_provider_key", mock_get_provider_key), \
         patch("harness.keys.get_disconnected", lambda: set()), \
         patch("harness.model_fetch.fetch_models", lambda *_a, **_k: list(live)), \
         patch(
             "harness.auto_registry._enabled_picker_models",
             lambda name: ["moonshotai/kimi-k3"] if name == "openrouter" else [],
         ):
        from harness.auto_registry import sync_agentic_registry
        sync_agentic_registry()

    ids = {m["id"] for m in json.loads(models_path.read_text())["models"]}
    assert ids == {"agentic/moonshotai/kimi-k3"}
    assert not any("mimo" in mid for mid in ids)
    assert "agentic/minimax/minimax-m3" not in ids


def test_openrouter_uncurated_live_does_not_dump_mimo(monkeypatch, tmp_path):
    models_path = tmp_path / "models.json"
    monkeypatch.setenv("PUPPETMASTER_MODELS_PATH", str(models_path))
    monkeypatch.setenv("HARNESS_LIVE_PRICES", "0")

    live = [
        "xiaomi/mimo-v2-flash",
        "other/filler-1",
        "moonshotai/kimi-k3",
        "deepseek/deepseek-v4-pro",
    ]

    def mock_get_provider_key(provider):
        return "fake-openrouter" if provider.name == "openrouter" else None

    with patch("harness.registry_wizard.get_provider_key", mock_get_provider_key), \
         patch("harness.keys.get_disconnected", lambda: set()), \
         patch("harness.model_fetch.fetch_models", lambda *_a, **_k: list(live)), \
         patch("harness.auto_registry._enabled_picker_models", lambda _name: []):
        from harness.auto_registry import sync_agentic_registry
        sync_agentic_registry()

    ids = {m["id"] for m in json.loads(models_path.read_text())["models"]}
    assert "agentic/moonshotai/kimi-k3" in ids
    assert "agentic/deepseek/deepseek-v4-pro" in ids
    assert not any("mimo" in mid for mid in ids)
    assert "agentic/other/filler-1" not in ids


def test_opencode_go_live_drops_mimo_when_not_served(monkeypatch, tmp_path):
    models_path = tmp_path / "models.json"
    monkeypatch.setenv("PUPPETMASTER_MODELS_PATH", str(models_path))
    monkeypatch.setenv("HARNESS_LIVE_PRICES", "0")

    def mock_get_provider_key(provider):
        return "fake-go" if provider.name == "opencode-go" else None

    live = ["gpt-5.6-luna", "deepseek-v4-flash", "kimi-k3"]

    with patch("harness.registry_wizard.get_provider_key", mock_get_provider_key), \
         patch("harness.keys.get_disconnected", lambda: set()), \
         patch("harness.model_fetch.fetch_models", lambda *_a, **_k: list(live)), \
         patch("harness.auto_registry._enabled_picker_models", lambda _name: []):
        from harness.auto_registry import sync_agentic_registry
        sync_agentic_registry()

    ids = {m["id"] for m in json.loads(models_path.read_text())["models"]}
    assert "agentic/gpt-5.6-luna" in ids
    assert "agentic/deepseek-v4-flash" in ids
    assert not any("mimo" in mid for mid in ids)


def test_sync_with_no_keys_writes_no_agentic_models(monkeypatch, tmp_path):
    models_path = tmp_path / "models.json"
    models_path.write_text(
        json.dumps({"models": [{"id": "cursor/composer-2-5", "adapter": "cursor"}]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("PUPPETMASTER_MODELS_PATH", str(models_path))
    monkeypatch.setenv("HARNESS_LIVE_PRICES", "0")

    with patch("harness.registry_wizard.get_provider_key", lambda _p: None), \
         patch("harness.keys.get_disconnected", lambda: set()):
        from harness.auto_registry import sync_agentic_registry
        result = sync_agentic_registry()

    assert result["synced"] is True
    assert result["models_count"] == 0
    data = json.loads(models_path.read_text())
    assert data["models"] == [{"id": "cursor/composer-2-5", "adapter": "cursor"}]


def test_newest_dated_snapshot_prefers_0813():
    from harness.auto_registry import _newest_dated_snapshot

    live = [
        "anthropic/claude-opus-4.8",
        *[f"filler/{i}" for i in range(20)],
        "deepseek/deepseek-v4-pro",
        "deepseek/deepseek-v4-pro-0805",
        "deepseek/deepseek-v4-pro-0813",
        "deepseek/deepseek-v4-pro-free",
    ]
    assert _newest_dated_snapshot(
        "deepseek/deepseek-v4-pro", live,
    ) == "deepseek/deepseek-v4-pro-0813"
    assert _newest_dated_snapshot("moonshotai/kimi-k3", live) == "moonshotai/kimi-k3"


def test_openrouter_discovery_promotes_buried_dated_snapshot(monkeypatch, tmp_path):
    """Dated DeepSeek 0813 must win even when it is not in the first 6 live ids."""
    models_path = tmp_path / "models.json"
    monkeypatch.setenv("PUPPETMASTER_MODELS_PATH", str(models_path))
    monkeypatch.setenv("HARNESS_LIVE_PRICES", "0")

    live = (
        [f"other/filler-{i}" for i in range(12)]
        + [
            "deepseek/deepseek-v4-pro",
            "deepseek/deepseek-v4-pro-0813",
            "moonshotai/kimi-k3",
        ]
    )

    def mock_get_provider_key(provider):
        return "fake-openrouter" if provider.name == "openrouter" else None

    def mock_fetch_models(provider, key, force=False):
        return list(live)

    with patch("harness.registry_wizard.get_provider_key", mock_get_provider_key), \
         patch("harness.keys.get_disconnected", lambda: set()), \
         patch("harness.model_fetch.fetch_models", mock_fetch_models), \
         patch("harness.auto_registry._enabled_picker_models", lambda _name: []):
        from harness.auto_registry import sync_agentic_registry
        result = sync_agentic_registry()

    assert result["synced"] is True
    data = json.loads(models_path.read_text())
    by_id = {m["id"]: m for m in data["models"]}
    pro = by_id["agentic/deepseek/deepseek-v4-pro"]
    assert pro["adapter_model_name"] == "deepseek/deepseek-v4-pro-0813"
    assert "agentic/deepseek/deepseek-v4-pro-0813" not in by_id
    kimi = by_id["agentic/moonshotai/kimi-k3"]
    assert kimi["adapter_model_name"] == "moonshotai/kimi-k3"


def test_openrouter_picker_promotes_dated_snapshot(monkeypatch, tmp_path):
    models_path = tmp_path / "models.json"
    monkeypatch.setenv("PUPPETMASTER_MODELS_PATH", str(models_path))
    monkeypatch.setenv("HARNESS_LIVE_PRICES", "0")

    def mock_get_provider_key(provider):
        return "fake-openrouter" if provider.name == "openrouter" else None

    def mock_fetch_models(provider, key, force=False):
        return ["deepseek/deepseek-v4-pro-0813", "anthropic/claude-opus-4.8"]

    with patch("harness.registry_wizard.get_provider_key", mock_get_provider_key), \
         patch("harness.keys.get_disconnected", lambda: set()), \
         patch("harness.model_fetch.fetch_models", mock_fetch_models), \
         patch(
             "harness.auto_registry._enabled_picker_models",
             lambda name: (
                 ["deepseek/deepseek-v4-pro", "anthropic/claude-opus-4.8"]
                 if name == "openrouter" else []
             ),
         ):
        from harness.auto_registry import sync_agentic_registry
        sync_agentic_registry()

    data = json.loads(models_path.read_text())
    by_id = {m["id"]: m for m in data["models"]}
    pro = by_id["agentic/deepseek/deepseek-v4-pro"]
    assert pro["adapter_model_name"] == "deepseek/deepseek-v4-pro-0813"
    assert "agentic/moonshotai/kimi-k3" not in by_id


def test_sync_force_is_passed_to_fetch_models(monkeypatch, tmp_path):
    models_path = tmp_path / "models.json"
    monkeypatch.setenv("PUPPETMASTER_MODELS_PATH", str(models_path))
    monkeypatch.setenv("HARNESS_LIVE_PRICES", "0")
    seen = {}

    def mock_get_provider_key(provider):
        return "fake-openrouter" if provider.name == "openrouter" else None

    def mock_fetch_models(provider, key, force=False):
        seen["force"] = force
        return []

    with patch("harness.registry_wizard.get_provider_key", mock_get_provider_key), \
         patch("harness.keys.get_disconnected", lambda: set()), \
         patch("harness.model_fetch.fetch_models", mock_fetch_models), \
         patch("harness.auto_registry._enabled_picker_models", lambda _name: []):
        from harness.auto_registry import sync_agentic_registry
        sync_agentic_registry(force=True)

    assert seen.get("force") is True


def test_registry_auto_refresh_kill_switch(monkeypatch):
    import harness.auto_registry as ar

    monkeypatch.setenv("HARNESS_REGISTRY_AUTO_REFRESH", "0")
    monkeypatch.setattr(ar, "_refresh_thread", None)
    assert ar.start_registry_auto_refresh() is False


def test_in_live_catalog_accepts_dated_siblings():
    from harness.auto_registry import _in_live_catalog

    live = ["deepseek/deepseek-v4-pro-0813", "moonshotai/kimi-k3"]
    assert _in_live_catalog(
        "deepseek/deepseek-v4-pro", "deepseek/deepseek-v4-pro", live,
    )
    assert _in_live_catalog("moonshotai/kimi-k3", "moonshotai/kimi-k3", live)
    assert not _in_live_catalog("xiaomi/mimo-v2-flash", "xiaomi/mimo-v2-flash", live)


def test_known_spec_follows_dated_family():
    from harness.auto_registry import _known_spec_for, _KNOWN_MODEL_SPECS

    rolling = _KNOWN_MODEL_SPECS["deepseek/deepseek-v4-pro"]
    assert _known_spec_for(
        "deepseek/deepseek-v4-pro-0813", "deepseek/deepseek-v4-pro",
    ) == rolling


def test_keyed_openrouter_not_starved_by_other_provider_toggles(monkeypatch, tmp_path):
    """Stale anthropic Models toggles must not empty a keyed OpenRouter catalog."""
    models_path = tmp_path / "models.json"
    monkeypatch.setenv("PUPPETMASTER_MODELS_PATH", str(models_path))
    monkeypatch.setenv("HARNESS_LIVE_PRICES", "0")

    live = ["moonshotai/kimi-k3", "z-ai/glm-5.2", "xiaomi/mimo-v2-flash"]

    def mock_get_provider_key(provider):
        return "fake-openrouter" if provider.name == "openrouter" else None

    def mock_enabled(name):
        if name == "anthropic":
            return ["claude-opus-4-8"]
        return []

    with patch("harness.registry_wizard.get_provider_key", mock_get_provider_key), \
         patch("harness.keys.get_disconnected", lambda: set()), \
         patch("harness.model_fetch.fetch_models", lambda *_a, **_k: list(live)), \
         patch("harness.auto_registry._enabled_picker_models", mock_enabled):
        from harness.auto_registry import sync_agentic_registry
        result = sync_agentic_registry()

    assert result["synced"] is True
    ids = {m["id"] for m in json.loads(models_path.read_text())["models"]}
    assert "agentic/moonshotai/kimi-k3" in ids
    assert "agentic/z-ai/glm-5.2" in ids
    assert not any("mimo" in mid for mid in ids)
    assert not any("claude" in mid for mid in ids)
