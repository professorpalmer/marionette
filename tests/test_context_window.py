"""Tests for per-model context window resolution. History: every model was once
throttled to a flat 96K; then we resolved from the catalog; now context_window()
resolves from the live OpenRouter /models map first (cached), then the catalog,
then a 200K floor. These tests pin live OFF so they assert the catalog+floor
contract deterministically (the live path is covered in test_context_window_live).
"""
import pytest

from pmharness.registry import context_window


@pytest.fixture(autouse=True)
def _live_off(monkeypatch):
    # Disable the live OpenRouter source so resolution is deterministic against
    # the catalog + floor (no network in CI).
    monkeypatch.setenv("PMHARNESS_OR_LIVE_WINDOWS", "0")
    import pmharness.registry as reg
    monkeypatch.setattr(reg, "_CW_MEM", None, raising=False)


def test_known_models_use_catalog_window():
    # With live off, catalog values come through. Spot-check a few that have one.
    assert context_window("claude-frontier") == 200000
    assert context_window("gemini-3.5-flash") == 1000000


def test_unknown_model_falls_back_to_floor():
    # The floor was raised 96K -> 200K so unknown/new models are not starved.
    assert context_window("not-a-real-model") == 200000
    assert context_window("not-a-real-model", default=120000) == 120000


def test_config_resolves_window_from_driver(monkeypatch):
    monkeypatch.delenv("HARNESS_MAX_CONTEXT_TOKENS", raising=False)
    monkeypatch.setenv("HARNESS_DRIVER", "claude-frontier")
    monkeypatch.setenv("HARNESS_CONFIG", "/nonexistent/harness.json")
    from harness.config import HarnessConfig
    cfg = HarnessConfig.from_env()
    assert cfg.max_context_tokens == 200000


def test_config_env_override_wins(monkeypatch):
    # A deliberate cap must NEVER be silently widened by live or catalog values.
    monkeypatch.setenv("HARNESS_MAX_CONTEXT_TOKENS", "40000")
    monkeypatch.setenv("HARNESS_DRIVER", "gemini-3.5-flash")  # would be 1M
    monkeypatch.setenv("HARNESS_CONFIG", "/nonexistent/harness.json")
    from harness.config import HarnessConfig
    cfg = HarnessConfig.from_env()
    assert cfg.max_context_tokens == 40000


def test_config_unknown_driver_uses_floor(monkeypatch):
    monkeypatch.delenv("HARNESS_MAX_CONTEXT_TOKENS", raising=False)
    monkeypatch.setenv("HARNESS_DRIVER", "totally-made-up")
    monkeypatch.setenv("HARNESS_CONFIG", "/nonexistent/harness.json")
    from harness.config import HarnessConfig
    cfg = HarnessConfig.from_env()
    assert cfg.max_context_tokens == 200000
