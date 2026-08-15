"""Tests for model visibility (enabled-set persistence + catalog + picker feed)."""
import json
import os

import pytest


@pytest.fixture
def mv(tmp_path, monkeypatch):
    # Redirect the store to a temp dir so tests never touch the real one.
    import harness.model_visibility as m
    store = tmp_path / "models.json"
    monkeypatch.setattr(m, "_store_path", lambda: str(store))
    return m


def test_empty_by_default(mv):
    assert mv.get_enabled() == []


def test_set_and_get(mv):
    out = mv.set_enabled(["openrouter:z-ai/glm-5.2", "openai:gpt-5.4"])
    assert out == ["openrouter:z-ai/glm-5.2", "openai:gpt-5.4"]
    assert mv.get_enabled() == ["openrouter:z-ai/glm-5.2", "openai:gpt-5.4"]


def test_set_dedups_and_strips(mv):
    out = mv.set_enabled(["a:b", "a:b", "  ", "c:d "])
    assert out == ["a:b", "c:d"]


def test_toggle_on_off(mv):
    mv.set_enabled(["a:b"])
    assert "x:y" not in mv.get_enabled()
    after_on = mv.toggle("x:y", True)
    assert "x:y" in after_on
    after_off = mv.toggle("x:y", False)
    assert "x:y" not in after_off


def test_toggle_on_is_idempotent(mv):
    mv.toggle("a:b", True)
    mv.toggle("a:b", True)
    assert mv.get_enabled().count("a:b") == 1


def test_persists_across_reload(mv):
    mv.set_enabled(["p:m"])
    # simulate a fresh process: re-read from disk
    assert mv.get_enabled() == ["p:m"]


def test_catalog_marks_enabled(mv, monkeypatch):
    import harness.providers as prov
    # Force a provider to look available regardless of env keys.
    monkeypatch.setattr(prov, "available_providers", lambda: [prov.get_provider("openrouter")])
    monkeypatch.setattr(prov.get_provider("openrouter").__class__, "key", lambda self: None)
    spec = "openrouter:" + prov.get_provider("openrouter").pilot_models[0]
    mv.set_enabled([spec])
    cat = mv.catalog(available_only=True)
    assert any(c["spec"] == spec and c["enabled"] for c in cat)
    # every entry carries the contract fields
    for c in cat:
        assert set(c) >= {"provider", "provider_display", "model", "spec", "available", "enabled"}


def test_enabled_pilots_filters_to_available(mv, monkeypatch):
    import harness.providers as prov
    monkeypatch.setattr(prov, "available_providers", lambda: [prov.get_provider("openrouter")])
    monkeypatch.setattr(prov.get_provider("openrouter").__class__, "key", lambda self: None)
    or_model = prov.get_provider("openrouter").pilot_models[0]
    # Enable one available spec and one for a provider with no key.
    mv.set_enabled([f"openrouter:{or_model}", "anthropic:claude-opus-4-8"])
    pilots = mv.enabled_pilots()
    assert f"openrouter:{or_model}" in pilots
    assert "anthropic:claude-opus-4-8" not in pilots  # no anthropic key -> filtered


def test_promote_newer_family_versions_glm_and_not_mimo(mv):
    promoted = mv.promote_newer_family_versions(
        ["glm-5.2", "z-ai/glm-5.2", "moonshotai/kimi-k3"],
        ["glm-5.3", "z-ai/glm-5.3", "xiaomi/mimo-v2-flash", "glm-4.7"],
    )
    assert "glm-5.3" in promoted
    assert not any("mimo" in item for item in promoted)
    assert "glm-4.7" not in promoted


def test_promote_newer_family_keeps_openrouter_prefix(mv):
    promoted = mv.promote_newer_family_versions(
        ["z-ai/glm-5.2"],
        ["z-ai/glm-5.3"],
    )
    assert promoted == ["z-ai/glm-5.3"]


def test_inherit_family_spec_uses_older_sibling(mv):
    specs = {"z-ai/glm-5.2": (86, 1.0, 3.5, 1000000, ["quality"])}
    assert mv.inherit_family_spec("z-ai/glm-5.3", "z-ai/glm-5.3", specs) == specs["z-ai/glm-5.2"]
    assert mv.inherit_family_spec("xiaomi/mimo-v2-flash", "xiaomi/mimo-v2-flash", specs) is None


def test_enabled_pilots_falls_back_when_empty(mv, monkeypatch):
    import harness.providers as prov
    monkeypatch.setattr(prov, "available_providers", lambda: [prov.get_provider("openrouter")])
    monkeypatch.setattr(
        mv,
        "provider_models",
        lambda provider, **_kwargs: list(provider.pilot_models),
    )
    # nothing curated -> full available set
    pilots = mv.enabled_pilots()
    assert len(pilots) == len(prov.get_provider("openrouter").pilot_models)
