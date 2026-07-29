from __future__ import annotations

"""Boot gate: the agentic catalog must match live provider keys.

Hermetic — no network. A fresh/restarted backend keyed only for OpenRouter must
end up with the Marionette OpenRouter ladder and nothing it cannot authenticate,
and a keyless backend must fail closed rather than route into a guaranteed 401.
"""

import json
from unittest.mock import patch


def _write_registry(path, models):
    path.write_text(json.dumps({"version": 1, "models": models}, indent=2), encoding="utf-8")


def _read_models(path):
    return json.loads(path.read_text(encoding="utf-8"))["models"]


def _only(*keyed_provider_names):
    def _get_provider_key(provider):
        if provider.name in keyed_provider_names:
            return "fake-key-" + provider.name
        return None

    return _get_provider_key


def _hermetic(monkeypatch, tmp_path):
    # Deliberately NOT named marionette-models.json: reconcile_shared_models
    # gates on that filename, so this keeps the fixture off the real ~/.puppetmaster.
    models_path = tmp_path / "models.json"
    monkeypatch.setenv("PUPPETMASTER_MODELS_PATH", str(models_path))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HARNESS_LIVE_PRICES", "0")
    return models_path


def test_only_openrouter_keyed_yields_ladder_and_prunes_foreign_rows(monkeypatch, tmp_path):
    models_path = _hermetic(monkeypatch, tmp_path)
    # Stale agentic anthropic row (no key) outranking everything, plus a
    # non-agentic plan peer that must survive.
    _write_registry(models_path, [
        {"id": "cursor/grok-4-5", "adapter": "cursor", "capability_score": 91},
        {
            "id": "agentic/claude-opus-4-8",
            "adapter": "agentic",
            "capability_score": 99,
            "payload_defaults": {"provider": "anthropic"},
        },
    ])

    from harness.auto_registry import ensure_keyed_provider_registry_health

    with patch("harness.registry_wizard.get_provider_key", _only("openrouter")), \
         patch("harness.keys.get_disconnected", lambda: set()), \
         patch("harness.model_fetch.fetch_models", lambda p, k, force=False: []):
        report = ensure_keyed_provider_registry_health()

    assert report["ready"] is True
    assert report["providers"] == ["openrouter"]

    models = _read_models(models_path)
    ids = {m["id"] for m in models}
    # Non-agentic peer preserved.
    assert "cursor/grok-4-5" in ids
    # Foreign (unkeyed) agentic row gone.
    assert "agentic/claude-opus-4-8" not in ids
    # Every remaining agentic row belongs to the keyed provider.
    agentic = [m for m in models if m["adapter"] == "agentic"]
    assert agentic
    assert {m["payload_defaults"]["provider"] for m in agentic} == {"openrouter"}
    # Required OpenRouter ladder rows are present and ladder-scored.
    by_id = {m["id"]: m for m in agentic}
    assert "agentic/moonshotai/kimi-k3" in by_id
    assert "agentic/deepseek/deepseek-v4-pro" in by_id
    assert by_id["agentic/moonshotai/kimi-k3"]["capability_score"] == 98
    assert report["missing_ladder"] == []


def test_no_key_fails_closed_and_prunes_every_agentic_row(monkeypatch, tmp_path):
    models_path = _hermetic(monkeypatch, tmp_path)
    _write_registry(models_path, [
        {"id": "cursor/grok-4-5", "adapter": "cursor", "capability_score": 91},
        {
            "id": "agentic/moonshotai/kimi-k3",
            "adapter": "agentic",
            "capability_score": 98,
            "payload_defaults": {"provider": "openrouter"},
        },
    ])

    from harness.auto_registry import ensure_keyed_provider_registry_health

    with patch("harness.registry_wizard.get_provider_key", lambda p: None), \
         patch("harness.keys.get_disconnected", lambda: set()), \
         patch("harness.model_fetch.fetch_models", lambda p, k, force=False: []):
        report = ensure_keyed_provider_registry_health()

    assert report["ready"] is False
    assert report["providers"] == []
    assert report["reason"] == "no keyed agentic provider"
    assert report["pruned"] == 1

    models = _read_models(models_path)
    assert [m["id"] for m in models] == ["cursor/grok-4-5"]


def test_ladder_is_reseeded_when_every_required_row_is_missing(monkeypatch, tmp_path):
    models_path = _hermetic(monkeypatch, tmp_path)
    _write_registry(models_path, [])

    from harness import auto_registry

    # Simulate a sync that produced only non-ladder OpenRouter rows (picker
    # curation gone stale), so the deterministic ladder is entirely absent.
    def _sync_without_ladder():
        _write_registry(models_path, [{
            "id": "agentic/z-ai/glm-5.2",
            "adapter": "agentic",
            "capability_score": 86,
            "payload_defaults": {"provider": "openrouter"},
        }])

    with patch("harness.registry_wizard.get_provider_key", _only("openrouter")), \
         patch("harness.keys.get_disconnected", lambda: set()), \
         patch("harness.model_fetch.fetch_models", lambda p, k, force=False: []), \
         patch.object(auto_registry, "sync_agentic_registry_safe", _sync_without_ladder):
        report = auto_registry.ensure_keyed_provider_registry_health()

    ids = {m["id"] for m in _read_models(models_path)}
    assert "agentic/moonshotai/kimi-k3" in ids
    assert "agentic/deepseek/deepseek-v4-pro" in ids
    assert report["missing_ladder"] == []
    assert set(report["seeded_ladder"]) == {
        "agentic/moonshotai/kimi-k3", "agentic/deepseek/deepseek-v4-pro",
    }


def test_prune_keeps_non_agentic_peers_untouched(monkeypatch, tmp_path):
    models_path = _hermetic(monkeypatch, tmp_path)
    _write_registry(models_path, [
        {"id": "cursor/composer-2-5", "adapter": "cursor"},
        {"id": "codex/gpt-5", "adapter": "codex"},
        {
            "id": "agentic/gemini-flash-latest",
            "adapter": "agentic",
            "payload_defaults": {"provider": "gemini"},
        },
    ])

    from harness.auto_registry import prune_unavailable_agentic_rows

    report = prune_unavailable_agentic_rows({"openrouter"})
    assert report["pruned"] == 1
    assert [m["id"] for m in _read_models(models_path)] == [
        "cursor/composer-2-5", "codex/gpt-5",
    ]


def test_keyed_agentic_providers_maps_harness_names_to_slugs(monkeypatch, tmp_path):
    _hermetic(monkeypatch, tmp_path)
    from harness.auto_registry import keyed_agentic_providers

    with patch("harness.registry_wizard.get_provider_key", _only("openai", "openrouter")), \
         patch("harness.keys.get_disconnected", lambda: set()):
        assert keyed_agentic_providers() == {"openai-api", "openrouter"}
