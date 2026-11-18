"""Regression tests for infer_roles signal matching."""
from __future__ import annotations

from pmharness.intent import infer_roles

_FULL = [
    "explore",
    "conflict-auditor",
    "test-coverage-reviewer",
    "pipeline-mapper",
    "decision-explainer",
]


def test_audit_word_inside_another_word_does_not_fan_out():
    assert infer_roles("What does the previewer do?") == ["explore"]


def test_audit_word_at_word_start_still_fans_out():
    assert infer_roles("Review the previewer") == _FULL
    assert infer_roles("Do an evaluation of the cache") == _FULL
    assert infer_roles("look for bugs in the parser") == _FULL


def test_hyphenated_flow_goal_adds_pipeline():
    assert infer_roles("Show the data-flow of the request") == ["explore", "pipeline-mapper"]
    assert infer_roles("Show the control-flow of the parser") == ["explore", "pipeline-mapper"]


def test_spaced_flow_goal_keeps_pipeline():
    assert infer_roles("Show the data flow of the request") == ["explore", "pipeline-mapper"]
