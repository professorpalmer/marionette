"""Tests for the AST structural preview (harness/ast_preview.py)."""
from harness.ast_preview import ast_preview_enabled, structural_diff


BASE = """
def alpha(x):
    return x


class Widget:
    def render(self, size):
        return size
"""


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("HARNESS_AST_PREVIEW", raising=False)
    assert ast_preview_enabled() is False


def test_enabled_via_env(monkeypatch):
    monkeypatch.setenv("HARNESS_AST_PREVIEW", "1")
    assert ast_preview_enabled() is True


def test_added_function_detected():
    after = BASE + "\n\ndef beta(y):\n    return y\n"
    diff = structural_diff(BASE, after)
    assert diff["available"] is True
    assert diff["added"] == ["beta"]
    assert diff["removed"] == []
    assert diff["changed"] == []


def test_removed_function_detected():
    after = BASE.replace("def alpha(x):\n    return x\n", "")
    diff = structural_diff(BASE, after)
    assert "alpha" in diff["removed"]
    assert diff["added"] == []


def test_rename_appears_as_remove_plus_add():
    after = BASE.replace("def alpha", "def alpha_renamed")
    diff = structural_diff(BASE, after)
    assert diff["removed"] == ["alpha"]
    assert diff["added"] == ["alpha_renamed"]


def test_signature_change_detected():
    after = BASE.replace("def alpha(x):", "def alpha(x, y=1):")
    diff = structural_diff(BASE, after)
    assert diff["changed"] == ["alpha"]
    assert diff["added"] == [] and diff["removed"] == []


def test_nested_method_uses_dotted_path():
    after = BASE.replace("def render(self, size):", "def render(self, size, dpi):")
    diff = structural_diff(BASE, after)
    assert diff["changed"] == ["Widget.render"]


def test_body_only_change_is_silent():
    after = BASE.replace("return x", "return x + 1")
    diff = structural_diff(BASE, after)
    assert diff == {"available": True, "added": [], "removed": [], "changed": []}


def test_syntax_error_is_safe():
    assert structural_diff("def broken(:", BASE) == {"available": False}
    assert structural_diff(BASE, "def broken(:") == {"available": False}


def test_empty_sources_are_safe():
    diff = structural_diff("", "")
    assert diff["available"] is True
    assert diff["added"] == [] and diff["removed"] == []
