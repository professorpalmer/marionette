from __future__ import annotations

"""Workspace rules say what the size budget cut, so the model can read the rest."""

from harness.conversation import load_workspace_rules


def test_small_rule_files_load_whole_with_no_marker(tmp_path):
    (tmp_path / "AGENTS.md").write_text("use tabs\n", encoding="utf-8")
    assert load_workspace_rules(str(tmp_path)) == (
        "\n\n# Workspace rules (from AGENTS.md)\nuse tabs\n"
    )


def test_a_cut_file_names_itself_and_the_rest_is_readable(tmp_path):
    (tmp_path / "AGENTS.md").write_text("a" * 9000 + "LAST RULE", encoding="utf-8")
    text = load_workspace_rules(str(tmp_path))
    assert "LAST RULE" not in text
    assert "[cut here: AGENTS.md has more text. read_file AGENTS.md for the rest.]" in text


def test_files_past_the_total_budget_are_listed(tmp_path):
    (tmp_path / "AGENTS.md").write_text("a" * 8192, encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("b" * 8192, encoding="utf-8")
    rules = tmp_path / ".cursor" / "rules"
    rules.mkdir(parents=True)
    (rules / "style.md").write_text("never use semicolons", encoding="utf-8")
    text = load_workspace_rules(str(tmp_path))
    assert "never use semicolons" not in text
    assert "# Workspace rules not loaded (16 KB budget used)\n- .cursor/rules/style.md" in text
