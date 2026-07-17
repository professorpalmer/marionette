"""Tests for driver re-resolution: the app must never default to a driver whose
provider is unavailable (e.g. saved driver qwen3-coder-30b routing through a
disconnected OpenRouter).

These test the _resolve_available_driver / _driver_provider_available helpers
directly against a saved _cfg, without reloading harness.server (which would
mutate shared module globals and leak into other tests)."""
import os
import json
import tempfile
import pytest

import harness.server as srv
from harness.config import HarnessConfig


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Snapshot + restore harness.server's module-level _cfg around each test so
    driver mutations never leak into other tests."""
    monkeypatch.setenv("HARNESS_STATE_DIR", tempfile.mkdtemp())
    # Ambient Cursor CLI login on a developer machine must not steal fallback.
    monkeypatch.delenv("CURSOR_CLI_LOGIN", raising=False)
    monkeypatch.setattr(
        "harness.cursor_cli_auth.is_authenticated",
        lambda: False,
    )
    saved = srv._cfg
    yield
    srv._cfg = saved


def _install_cfg(monkeypatch, enabled, driver, disconnect=None):
    state = os.environ["HARNESS_STATE_DIR"]
    json.dump({"enabled": enabled}, open(os.path.join(state, "models.json"), "w"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-...rted")
    # Pin the curated set so ambient ~/.pmharness model visibility cannot
    # insert cursor-cli (or other) specs ahead of the fixtures under test.
    monkeypatch.setattr(
        "harness.model_visibility.get_enabled",
        lambda: list(enabled),
    )
    monkeypatch.setattr(
        "harness.model_visibility.enabled_pilots",
        lambda: list(enabled),
    )
    import importlib
    from harness import keys as K
    importlib.reload(K)
    if disconnect:
        for d in disconnect:
            K.mark_disconnected(d)
    srv._cfg = HarnessConfig(driver=driver, reach="openrouter", state_dir=state)


def test_bare_driver_resolves_when_reach_disconnected(monkeypatch):
    _install_cfg(monkeypatch,
                 enabled=["anthropic:claude-opus-4-8", "anthropic:claude-sonnet-4-5"],
                 driver="qwen3-coder-30b",
                 disconnect=["openrouter"])
    srv._resolve_available_driver()
    assert srv._cfg.driver != "qwen3-coder-30b"
    assert srv._driver_provider_available(srv._cfg.driver)
    assert srv._cfg.driver.startswith("anthropic:")


def test_available_driver_is_left_alone(monkeypatch):
    _install_cfg(monkeypatch,
                 enabled=["anthropic:claude-opus-4-8"],
                 driver="anthropic:claude-opus-4-8",
                 disconnect=["openrouter"])
    srv._resolve_available_driver()
    assert srv._cfg.driver == "anthropic:claude-opus-4-8"


def test_provider_spec_driver_resolves_when_disconnected(monkeypatch):
    _install_cfg(monkeypatch,
                 enabled=["anthropic:claude-opus-4-8", "openrouter:openai/gpt-5.5"],
                 driver="openrouter:openai/gpt-5.5",
                 disconnect=["openrouter"])
    srv._resolve_available_driver()
    assert srv._driver_provider_available(srv._cfg.driver)
    assert srv._cfg.driver.startswith("anthropic:")


def test_curated_enabled_drops_compiled_in_default(monkeypatch):
    """User enables only a non-default OpenRouter model — active driver must
    leave qwen3-coder-30b and land on the first enabled spec."""
    _install_cfg(
        monkeypatch,
        enabled=["openrouter:deepseek/deepseek-v4-pro"],
        driver="qwen3-coder-30b",
    )
    monkeypatch.setattr(
        "harness.model_visibility.get_enabled",
        lambda: ["openrouter:deepseek/deepseek-v4-pro"],
    )
    monkeypatch.setattr(
        "harness.model_visibility.enabled_pilots",
        lambda: ["openrouter:deepseek/deepseek-v4-pro"],
    )
    srv._resolve_available_driver()
    assert srv._cfg.driver == "openrouter:deepseek/deepseek-v4-pro"


def test_available_pilots_does_not_inject_stale_default(monkeypatch):
    """Picker list must not force-prepend a driver the user never enabled."""
    _install_cfg(
        monkeypatch,
        enabled=["openrouter:deepseek/deepseek-v4-pro"],
        driver="qwen3-coder-30b",
    )
    state = os.environ["HARNESS_STATE_DIR"]
    monkeypatch.setattr(
        "harness.model_visibility._store_path",
        lambda: os.path.join(state, "models.json"),
    )
    monkeypatch.setattr(
        "harness.model_visibility.enabled_pilots",
        lambda: ["openrouter:deepseek/deepseek-v4-pro"],
    )
    monkeypatch.setattr(
        "harness.model_visibility.get_enabled",
        lambda: ["openrouter:deepseek/deepseek-v4-pro"],
    )
    pilots = srv._available_pilots()
    assert "qwen3-coder-30b" not in pilots
    assert pilots[0] == "openrouter:deepseek/deepseek-v4-pro"
