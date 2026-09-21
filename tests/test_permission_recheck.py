from __future__ import annotations

from harness.permission_recheck import recheck_rewritten_input


def test_unchanged_input_is_allowed():
    result = recheck_rewritten_input("echo hi", "echo hi")
    assert result.allowed is True
    assert result.rewritten is False
    assert result.reason == "unchanged"


def test_safe_rewrite_is_allowed():
    result = recheck_rewritten_input("python -m pytest", "/venv/bin/python -m pytest")
    assert result.allowed is True
    assert result.rewritten is True
    assert result.reason == "rewritten_safe"


def test_danger_rewrite_needs_approval():
    result = recheck_rewritten_input("echo hi", "rm -rf /")
    assert result.allowed is False
    assert result.rewritten is True
    assert result.reason == "rewritten_needs_approval"


def test_empty_rewrite_is_refused():
    result = recheck_rewritten_input("echo hi", "   ")
    assert result.allowed is False
    assert result.reason == "rewritten_empty"
