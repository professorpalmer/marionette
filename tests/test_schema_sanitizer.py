"""Unit tests for pmharness.schema_sanitizer (pure, hermetic)."""

from pmharness.schema_sanitizer import sanitize_tool_arguments


def test_clean_dict_passthrough():
    result, reason = sanitize_tool_arguments({"path": "src/main.py"})
    assert reason == ""
    assert result == {"path": "src/main.py"}


def test_clean_json_string():
    result, reason = sanitize_tool_arguments('{"path": "a.py", "line": 3}')
    assert reason == ""
    assert result == {"path": "a.py", "line": 3}


def test_fenced_json_extraction():
    raw = "```json\n{\"path\": \"a.py\"}\n```"
    result, reason = sanitize_tool_arguments(raw)
    assert reason == ""
    assert result == {"path": "a.py"}


def test_fenced_no_language_tag():
    raw = "```\n{\"x\": 1}\n```"
    result, reason = sanitize_tool_arguments(raw)
    assert reason == ""
    assert result == {"x": 1}


def test_trailing_comma_repair():
    raw = '{"path": "a.py", "line": 3,}'
    result, reason = sanitize_tool_arguments(raw)
    assert reason == ""
    assert result == {"path": "a.py", "line": 3}


def test_trailing_comma_in_array():
    raw = '{"items": [1, 2, 3,],}'
    result, reason = sanitize_tool_arguments(raw)
    assert reason == ""
    assert result == {"items": [1, 2, 3]}


def test_single_quote_repair():
    raw = "{'path': 'a.py', 'ok': true}"
    result, reason = sanitize_tool_arguments(raw)
    assert reason == ""
    assert result == {"path": "a.py", "ok": True}


def test_unquoted_key_repair():
    raw = '{path: "a.py", line: 3}'
    result, reason = sanitize_tool_arguments(raw)
    assert reason == ""
    assert result == {"path": "a.py", "line": 3}


def test_stringified_bool_coercion():
    result, reason = sanitize_tool_arguments({"force": "true", "dry": "False"})
    assert reason == ""
    assert result == {"force": True, "dry": False}


def test_stringified_number_and_null_coercion():
    result, reason = sanitize_tool_arguments(
        {"n": "42", "ratio": "3.14", "x": "null"}
    )
    assert reason == ""
    assert result == {"n": 42, "ratio": 3.14, "x": None}


def test_hopeless_garbage_returns_empty_and_reason():
    result, reason = sanitize_tool_arguments("this is not json at all <<<>>>")
    assert result == {}
    assert reason != ""


def test_empty_input_returns_reason():
    result, reason = sanitize_tool_arguments("   ")
    assert result == {}
    assert reason != ""


def test_non_object_json_returns_reason():
    result, reason = sanitize_tool_arguments("[1, 2, 3]")
    assert result == {}
    assert reason != ""
