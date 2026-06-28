"""Tests for live per-provider model discovery + merging into the picker catalog."""
import os
import tempfile

import harness.model_fetch as mf
import harness.providers as prov
from harness import model_visibility as mv


def _fake_provider(name="anthropic", models=("claude-opus-4-8",), key_env="ANTHROPIC_API_KEY"):
    p = prov.get_provider(name)
    return p


def test_fetch_models_disabled_via_env(monkeypatch):
    monkeypatch.setenv("PMHARNESS_LIVE_MODELS", "0")
    p = prov.get_provider("anthropic")
    assert mf.fetch_models(p, "fake-key") == []


def test_provider_models_merges_live_with_curated(monkeypatch):
    # Curated list for anthropic is 3; simulate a live fetch returning more.
    p = prov.get_provider("anthropic")
    monkeypatch.setattr(p.__class__, "key", lambda self: "fake-key")
    monkeypatch.setattr(
        mf, "fetch_models",
        lambda provider, key, **kw: ["claude-opus-4-8", "claude-fable-5", "claude-opus-4-7"],
    )
    merged = mv.provider_models(p)
    # Curated entries come first, then new live ones, de-duplicated.
    assert merged[0] == "claude-opus-4-8"
    assert "claude-fable-5" in merged
    assert "claude-opus-4-7" in merged
    # No duplicate of the curated opus-4-8 even though it is in both.
    assert merged.count("claude-opus-4-8") == 1


def test_provider_models_falls_back_to_curated_on_fetch_failure(monkeypatch):
    p = prov.get_provider("openai")
    monkeypatch.setattr(p.__class__, "key", lambda self: "fake-key")
    monkeypatch.setattr(mf, "fetch_models", lambda provider, key, **kw: [])
    merged = mv.provider_models(p)
    # Falls back to exactly the curated pilot_models.
    assert merged == list(p.pilot_models)


def test_provider_models_no_key_returns_curated(monkeypatch):
    p = prov.get_provider("xai")
    monkeypatch.setattr(p.__class__, "key", lambda self: None)
    merged = mv.provider_models(p)
    assert merged == list(p.pilot_models)
