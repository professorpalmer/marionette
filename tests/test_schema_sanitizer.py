"""Unit tests for pmharness.schema_sanitizer (pure, hermetic)."""

import pytest

from pmharness.schema_sanitizer import sanitize_tool_arguments


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param({"path": "src/main.py"}, {"path": "src/main.py"}, id="clean_dict_passthrough"),
        pytest.param('{"path": "a.py", "line": 3}', {"path": "a.py", "line": 3}, id="clean_json_string"),
        pytest.param("```json\n{\"path\": \"a.py\"}\n```", {"path": "a.py"}, id="fenced_json"),
        pytest.param("```\n{\"x\": 1}\n```", {"x": 1}, id="fenced_no_language_tag"),
        pytest.param('{"path": "a.py", "line": 3,}', {"path": "a.py", "line": 3}, id="trailing_comma"),
        pytest.param('{"items": [1, 2, 3,],}', {"items": [1, 2, 3]}, id="trailing_comma_in_array"),
        pytest.param("{'path': 'a.py', 'ok': true}", {"path": "a.py", "ok": True}, id="single_quote"),
        pytest.param('{path: "a.py", line: 3}', {"path": "a.py", "line": 3}, id="unquoted_key"),
        pytest.param(
            {"force": "true", "dry": "False"}, {"force": True, "dry": False}, id="stringified_bool",
        ),
        pytest.param(
            {"n": "42", "ratio": "3.14", "x": "null"},
            {"n": 42, "ratio": 3.14, "x": None},
            id="stringified_number_and_null",
        ),
    ],
)
def test_sanitize_repairs(raw, expected):
    result, reason = sanitize_tool_arguments(raw)
    assert reason == ""
    assert result == expected


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("this is not json at all <<<>>>", id="hopeless_garbage"),
        pytest.param("   ", id="empty_input"),
        pytest.param("[1, 2, 3]", id="non_object_json"),
    ],
)
def test_sanitize_refuses(raw):
    result, reason = sanitize_tool_arguments(raw)
    assert result == {}
    assert reason != ""
