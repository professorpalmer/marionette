"""Reasoning effort normalization and Codex API mapping."""

from __future__ import annotations

import pytest

from harness.reasoning_effort import (
    DEFAULT_CODEX_REASONING_EFFORT,
    DEFAULT_SWARM_REASONING_EFFORT,
    REASONING_EFFORT_LEVELS,
    SWARM_REASONING_EFFORT_ENV,
    anthropic_thinking_budget,
    apply_anthropic_thinking,
    codex_api_effort,
    current_reasoning_effort,
    current_swarm_reasoning_effort,
    is_reasoning_mandatory_error,
    model_supports_anthropic_thinking,
    normalize_reasoning_effort,
    reasoning_effort_label,
)


@pytest.mark.parametrize("raw,expected", [
    ("low", "low"),
    ("Medium", "medium"),
    ("EXTRA HIGH", "xhigh"),
    ("extra_high", "xhigh"),
    ("ultra", "xhigh"),
    ("ULtra", "xhigh"),
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


def test_current_swarm_reasoning_effort_defaults_medium(monkeypatch):
    monkeypatch.delenv(SWARM_REASONING_EFFORT_ENV, raising=False)
    assert current_swarm_reasoning_effort() == DEFAULT_SWARM_REASONING_EFFORT
    assert DEFAULT_SWARM_REASONING_EFFORT == "medium"
    monkeypatch.setenv(SWARM_REASONING_EFFORT_ENV, "low")
    assert current_swarm_reasoning_effort() == "low"
    monkeypatch.setenv(SWARM_REASONING_EFFORT_ENV, "xhigh")
    assert current_swarm_reasoning_effort() == "xhigh"


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


@pytest.mark.parametrize("text", [
    "Reasoning is mandatory for this model",
    "HTTP 400: reasoning cannot be disabled",
    "thinking is required when tools are present",
    "REASONING IS MANDATORY",
])
def test_is_reasoning_mandatory_error_true(text):
    assert is_reasoning_mandatory_error(text) is True


@pytest.mark.parametrize("text", [
    None,
    "",
    "   ",
    "cannot be disabled",
    "streaming cannot be disabled",
    "HTTP 400: invalid_request — unknown parameter foo",
    "context length exceeded",
    "rate limit",
])
def test_is_reasoning_mandatory_error_false(text):
    assert is_reasoning_mandatory_error(text) is False
