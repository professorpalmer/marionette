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


def test_fetch_failure_records_reason_instead_of_swallowing(monkeypatch):
    # A failing fetch must still return [] (graceful fallback) BUT surface WHY,
    # so an empty picker can explain bad-key vs network vs schema change.
    p = prov.get_provider("anthropic")

    def _boom(url, headers):
        raise RuntimeError("simulated 401 unauthorized")

    monkeypatch.setattr(mf, "_get", _boom)
    assert mf._fetch_provider_models(p, "bad-key") == []
    reason = mf.last_fetch_error("anthropic")
    assert reason is not None and "simulated 401" in reason


def test_fetch_success_clears_prior_error(monkeypatch):
    monkeypatch.setattr(mf, "_get", lambda url, headers: {"data": [{"id": "claude-opus-4-8"}]})
    out = mf._fetch_provider_models(prov.get_provider("anthropic"), "good-key")
    assert out == ["claude-opus-4-8"]
    assert mf.last_fetch_error("anthropic") is None


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


def test_cursor_cli_provider_models_prefers_live_order(monkeypatch):
    p = prov.get_provider("cursor-cli")
    monkeypatch.setattr(p.__class__, "key", lambda self: "1")
    monkeypatch.setattr(
        mf, "fetch_models",
        lambda provider, key, **kw: ["composer-2.5", "cursor-grok-4.5-high", "gpt-5.5-high"],
    )
    merged = mv.provider_models(p)
    assert merged[:3] == ["composer-2.5", "cursor-grok-4.5-high", "gpt-5.5-high"]


def test_codex_oauth_models_parse(monkeypatch):
    monkeypatch.setattr(
        mf, "_get",
        lambda url, headers: {
            "models": [
                {"slug": "gpt-5.5", "priority": 1},
                {"slug": "gpt-5.3-codex", "priority": 2},
                {"slug": "hidden-x", "visibility": "hidden", "priority": 0},
            ]
        },
    )
    out = mf._fetch_codex_oauth_models("tok")
    assert out[0] == "gpt-5.5"
    assert "gpt-5.3-codex" in out
    assert "hidden-x" not in out
    assert "gpt-5.6-sol" in out  # forward-compat


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
