"""Tests for per-model context window resolution. The bug: every model was
throttled to a flat 96K even when its real window is 200K-1M. Fix: resolve from
the catalog's context_window, with an env override that always wins.
"""
import os

import pytest

from pmharness.registry import context_window


def test_known_models_use_real_window():
    # representative spot checks against the catalog values
    assert context_window("qwen3-coder-30b") == 262144
    assert context_window("claude-frontier") == 200000
    assert context_window("gemini-3.5-flash") == 1000000
    assert context_window("deepseek-v4-pro") == 128000


def test_unknown_model_falls_back_to_default():
    assert context_window("not-a-real-model") == 96000
    assert context_window("not-a-real-model", default=120000) == 120000


def test_config_resolves_window_from_driver(monkeypatch):
    monkeypatch.delenv("HARNESS_MAX_CONTEXT_TOKENS", raising=False)
    monkeypatch.setenv("HARNESS_DRIVER", "claude-frontier")
    monkeypatch.setenv("HARNESS_CONFIG", "/nonexistent/harness.json")
    from harness.config import HarnessConfig
    cfg = HarnessConfig.from_env()
    assert cfg.max_context_tokens == 200000


def test_config_env_override_wins(monkeypatch):
    monkeypatch.setenv("HARNESS_MAX_CONTEXT_TOKENS", "40000")
    monkeypatch.setenv("HARNESS_DRIVER", "gemini-3.5-flash")  # would be 1M
    monkeypatch.setenv("HARNESS_CONFIG", "/nonexistent/harness.json")
    from harness.config import HarnessConfig
    cfg = HarnessConfig.from_env()
    assert cfg.max_context_tokens == 40000  # explicit cap respected


def test_config_unknown_driver_safe_default(monkeypatch):
    monkeypatch.delenv("HARNESS_MAX_CONTEXT_TOKENS", raising=False)
    monkeypatch.setenv("HARNESS_DRIVER", "totally-made-up")
    monkeypatch.setenv("HARNESS_CONFIG", "/nonexistent/harness.json")
    from harness.config import HarnessConfig
    cfg = HarnessConfig.from_env()
    assert cfg.max_context_tokens == 96000
