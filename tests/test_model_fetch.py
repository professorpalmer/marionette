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


def test_chat_model_filter_drops_non_chat():
    """Live model fetch must drop image/video/audio/embedding/etc models -- a
    pilot must be a text chat model. This kept veo/imagen/lyria/tts/embedding
    entries out of the picker."""
    from harness.model_fetch import _is_chat_model
    # Chat models -> kept
    for m in ["gpt-5.5", "gpt-5.4", "claude-opus-4-8", "gemini-3-pro-preview",
              "deepseek-chat", "glm-5.2"]:
        assert _is_chat_model(m), f"{m} should be a chat model"
    # Non-chat -> dropped
    for m in ["veo-3.0-generate-001", "imagen-4.0-generate-001", "lyria-3-pro-preview",
              "gemini-embedding-2", "tts-1-hd", "whisper-1", "dall-e-3",
              "gpt-realtime-2", "text-embedding-3-large", "aqa",
              "gemini-2.5-pro-preview-tts", "nano-banana-pro-preview"]:
        assert not _is_chat_model(m), f"{m} should NOT be a chat model"
