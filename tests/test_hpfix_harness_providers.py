from __future__ import annotations

import pytest

from harness import providers


@pytest.fixture
def openrouter_only(monkeypatch):
    for p in providers.PROVIDERS:
        for ev in p.env_vars:
            monkeypatch.delenv(ev, raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")


def test_bare_slug_with_colon_keeps_full_model(openrouter_only):
    d = providers.build_pilot("deepseek/deepseek-r1:free")
    assert d.model == "deepseek/deepseek-r1:free"


def test_other_free_slug_keeps_full_model(openrouter_only):
    d = providers.build_pilot("meta-llama/llama-3.3-70b-instruct:free")
    assert d.model == "meta-llama/llama-3.3-70b-instruct:free"


def test_known_provider_prefix_still_splits(openrouter_only):
    d = providers.build_pilot("openrouter:deepseek/deepseek-r1")
    assert d.model == "deepseek/deepseek-r1"


def test_known_provider_prefix_with_free_suffix(openrouter_only):
    d = providers.build_pilot("openrouter:deepseek/deepseek-r1:free")
    assert d.model == "deepseek/deepseek-r1:free"
