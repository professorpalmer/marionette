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
    # Static capability/context/tags preserved from template.
    template = _AGENTIC_TEMPLATES["anthropic"]["balanced"]
    assert spec["capability_score"] == template[0]
    assert spec["context_window"] == template[3]
    assert spec["tags"] == list(template[4])


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
