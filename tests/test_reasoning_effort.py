"""Reasoning effort normalization and Codex API mapping."""

from __future__ import annotations

import pytest

from harness.reasoning_effort import (
    DEFAULT_CODEX_REASONING_EFFORT,
    REASONING_EFFORT_LEVELS,
    codex_api_effort,
    current_reasoning_effort,
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
