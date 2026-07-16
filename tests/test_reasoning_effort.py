"""Reasoning effort normalization and Codex API mapping."""

from __future__ import annotations

import pytest

from harness.reasoning_effort import (
    DEFAULT_CODEX_REASONING_EFFORT,
    REASONING_EFFORT_LEVELS,
    anthropic_thinking_budget,
    apply_anthropic_thinking,
    codex_api_effort,
    current_reasoning_effort,
    model_supports_anthropic_thinking,
    normalize_reasoning_effort,
    reasoning_effort_label,
)


@pytest.mark.parametrize("raw,expected", [
    ("low", "low"),
    ("Medium", "medium"),
    ("EXTRA HIGH", "xhigh"),
    ("extra_high", "xhigh"),
    ("none", "none"),
    ("off", "none"),
    ("", DEFAULT_CODEX_REASONING_EFFORT),
    (None, DEFAULT_CODEX_REASONING_EFFORT),
    ("not-a-level", DEFAULT_CODEX_REASONING_EFFORT),
])
def test_normalize_reasoning_effort(raw, expected):
    assert normalize_reasoning_effort(raw) == expected


def test_codex_api_effort_none_omits():
    assert codex_api_effort("none") is None


@pytest.mark.parametrize("level", ["low", "medium", "high", "xhigh", "max"])
def test_codex_api_effort_maps_levels(level):
    assert codex_api_effort(level) == level


def test_current_reasoning_effort_reads_env(monkeypatch):
    monkeypatch.delenv("HARNESS_CODEX_REASONING_EFFORT", raising=False)
    assert current_reasoning_effort() == DEFAULT_CODEX_REASONING_EFFORT
    monkeypatch.setenv("HARNESS_CODEX_REASONING_EFFORT", "high")
    assert current_reasoning_effort() == "high"


def test_reasoning_effort_labels_cover_all_levels():
    for level in REASONING_EFFORT_LEVELS:
        assert reasoning_effort_label(level)


@pytest.mark.parametrize("model,ok", [
    ("claude-opus-4-8", True),
    ("claude-sonnet-4-5", True),
    ("us.anthropic.claude-sonnet-4-20250514-v1:0", True),
    ("claude-haiku-4-5", False),
    ("gpt-5.6-luna", False),
])
def test_model_supports_anthropic_thinking(model, ok):
    assert model_supports_anthropic_thinking(model) is ok


def test_anthropic_thinking_budget_none_omits(monkeypatch):
    monkeypatch.setenv("HARNESS_CODEX_REASONING_EFFORT", "none")
    assert anthropic_thinking_budget() is None


def test_apply_anthropic_thinking_injects_and_raises_max_tokens(monkeypatch):
    monkeypatch.setenv("HARNESS_CODEX_REASONING_EFFORT", "low")
    body = {"model": "claude-sonnet-4-5", "max_tokens": 1000}
    apply_anthropic_thinking(body, "claude-sonnet-4-5", max_tokens=1000)
    assert body["thinking"] == {"type": "enabled", "budget_tokens": 4096}
    assert body["max_tokens"] > 4096


def test_apply_anthropic_thinking_skips_haiku(monkeypatch):
    monkeypatch.setenv("HARNESS_CODEX_REASONING_EFFORT", "high")
    body = {"model": "claude-haiku-4-5", "max_tokens": 8000}
    apply_anthropic_thinking(body, "claude-haiku-4-5", max_tokens=8000)
    assert "thinking" not in body
