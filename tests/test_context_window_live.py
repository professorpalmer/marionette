"""Tests for context_window live-OpenRouter resolution. Hermetic: the live
source is disabled (PMHARNESS_OR_LIVE_WINDOWS=0) or monkeypatched, so CI never
touches the network. Behavior contracts, not frozen values.
"""
import os
import pytest


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    # Default: live source off -> resolution falls back to catalog then floor.
    monkeypatch.setenv("PMHARNESS_OR_LIVE_WINDOWS", "0")
    # Reset the in-process memo between tests.
    import pmharness.registry as reg
    monkeypatch.setattr(reg, "_CW_MEM", None, raising=False)


def test_catalog_value_used_when_live_off():
    import pmharness.registry as reg
    # gemini-3.5-flash has a catalog window; with live off it must come through.
    w = reg.context_window("gemini-3.5-flash")
    assert w >= 200000  # catalog says 1M; never below the floor


def test_unknown_model_floor():
    import pmharness.registry as reg
    assert reg.context_window("totally-unknown-xyz") == 200000


def test_explicit_default_override():
    import pmharness.registry as reg
    assert reg.context_window("totally-unknown-xyz", default=500000) == 500000


def test_live_map_wins_when_present(monkeypatch):
    import pmharness.registry as reg
    monkeypatch.setenv("PMHARNESS_OR_LIVE_WINDOWS", "1")
    monkeypatch.setattr(reg, "_CW_MEM", None, raising=False)
    # Inject a fake live map; no network.
    monkeypatch.setattr(reg, "_live_windows", lambda: {"z-ai/glm-5.2": 1048576})
    # catalog says glm-5.2 = 200K, live says 1M -> live wins
    assert reg.context_window("glm-5.2") == 1048576


def test_glm_5_3_live_map_wins_over_static(monkeypatch):
    import pmharness.registry as reg
    monkeypatch.setenv("PMHARNESS_OR_LIVE_WINDOWS", "1")
    monkeypatch.setattr(reg, "_CW_MEM", None, raising=False)
    monkeypatch.setattr(reg, "_live_windows", lambda: {"z-ai/glm-5.3": 1048576})
    assert reg.context_window("glm-5.3") == 1048576
    assert reg.context_window("zai:glm-5.3") == 1048576


def test_native_name_fuzzy_matches_openrouter_twin(monkeypatch):
    import pmharness.registry as reg
    monkeypatch.setenv("PMHARNESS_OR_LIVE_WINDOWS", "1")
    monkeypatch.setattr(reg, "_CW_MEM", None, raising=False)
    monkeypatch.setattr(reg, "_live_windows",
                        lambda: {"anthropic/claude-opus-4.8": 1000000,
                                 "anthropic/claude-opus-4.8-fast": 1000000})
    # native 'claude-opus-4-8' should fuzzy-match the base (shortest) twin
    assert reg.context_window("claude-opus-4-8") == 1000000


def test_provider_model_spec_resolves(monkeypatch):
    import pmharness.registry as reg
    monkeypatch.setenv("PMHARNESS_OR_LIVE_WINDOWS", "1")
    monkeypatch.setattr(reg, "_CW_MEM", None, raising=False)
    monkeypatch.setattr(reg, "_live_windows", lambda: {"z-ai/glm-5.2": 1048576})
    assert reg.context_window("openrouter:z-ai/glm-5.2") == 1048576


def test_live_fetch_failure_degrades_gracefully(monkeypatch):
    import pmharness.registry as reg
    monkeypatch.setenv("PMHARNESS_OR_LIVE_WINDOWS", "1")
    monkeypatch.setattr(reg, "_CW_MEM", None, raising=False)
    # live map empty (as if fetch failed) -> catalog/floor path
    monkeypatch.setattr(reg, "_live_windows", lambda: {})
    assert reg.context_window("totally-unknown-xyz") == 200000


def test_apply_context_window_fails_closed_for_unknown_openrouter_slug(monkeypatch):
    import pmharness.registry as reg
    monkeypatch.setattr(reg, "_live_windows", lambda: {})
    slug = "deepseek/deepseek-v4-unknown"
    with pytest.raises(ValueError, match="metadata unavailable"):
        reg.apply_context_window(slug)
    with pytest.raises(ValueError, match="metadata unavailable"):
        reg.apply_context_window(f"openrouter:{slug}")


def test_unknown_explicit_openrouter_slug_is_unavailable(monkeypatch):
    import pmharness.registry as reg
    monkeypatch.setattr(reg, "_live_windows", lambda: {})
    assert reg.context_window("openrouter:deepseek/deepseek-v4-unknown") == 0
    assert reg.context_window("deepseek/deepseek-v4-unknown") == 0
    assert reg.metadata_source("deepseek/deepseek-v4-unknown") == "unknown"


def test_native_slash_id_is_not_explicit_openrouter(monkeypatch):
    import pmharness.registry as reg
    monkeypatch.setattr(reg, "_live_windows", lambda: {})
    assert reg._explicit_openrouter_slug("native/qwen3-coder-30b") == ""
    # Ordinary floor / static path — not the OR zero-window fail-closed path.
    assert reg.context_window("native/totally-unknown-xyz") == 200000
    assert reg.metadata_source("native/totally-unknown-xyz") == "unknown"


def test_id_only_openrouter_metadata_does_not_use_static_128k(monkeypatch):
    """deepseek/deepseek-v4-flash-0731 id-only cache must not inherit deepseekv4=128K."""
    import pmharness.registry as reg

    slug = "deepseek/deepseek-v4-flash-0731"
    monkeypatch.setattr(reg, "_live_windows", lambda: {})
    monkeypatch.setattr(reg, "_PRICE_MEM", {})
    monkeypatch.setattr(
        reg,
        "_cached_openrouter_metadata",
        lambda name: {"id": slug, "source": "stale-live"},
    )
    monkeypatch.setattr(reg, "_try_refresh_openrouter_rich_metadata", lambda name: None)

    assert reg._static_window(slug) == 128000  # family table still exists…
    assert reg.context_window(slug) == 0       # …but explicit OR must not use it
    assert reg.metadata_source(slug) == "unknown"
    with pytest.raises(ValueError, match="metadata unavailable"):
        reg.apply_context_window(slug)


def test_id_only_openrouter_metadata_refresh_unlocks_rich_window(monkeypatch):
    import pmharness.registry as reg

    slug = "deepseek/deepseek-v4-flash-0731"
    monkeypatch.setattr(reg, "_live_windows", lambda: {})
    monkeypatch.setattr(reg, "_PRICE_MEM", {})
    state = {"rich": False}

    def _meta(name):
        if state["rich"]:
            return {"id": slug, "source": "live", "context_length": 1_000_000}
        return {"id": slug, "source": "stale-live"}

    def _refresh(name):
        state["rich"] = True
        return _meta(name)

    monkeypatch.setattr(reg, "_cached_openrouter_metadata", _meta)
    monkeypatch.setattr(reg, "_try_refresh_openrouter_rich_metadata", _refresh)
    assert reg.apply_context_window(f"openrouter:{slug}") == 1_000_000


def test_never_raises_on_garbage(monkeypatch):
    import pmharness.registry as reg
    assert reg.context_window("") == 200000
    assert reg.context_window(None) == 200000  # type: ignore
