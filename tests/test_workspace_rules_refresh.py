from __future__ import annotations

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession, load_workspace_rules
from harness.workspace_rules_refresh import (
    is_instruction_path,
    maybe_refresh_workspace_rules,
    reconcile_workspace_rules,
)


def test_instruction_path_matches_loaded_files(tmp_path):
    repo = str(tmp_path)
    assert is_instruction_path(str(tmp_path / "AGENTS.md"), repo)
    assert is_instruction_path("CLAUDE.md", repo)
    assert is_instruction_path(str(tmp_path / ".cursorrules"), repo)
    assert is_instruction_path(str(tmp_path / ".cursor" / "rules" / "foo.md"), repo)
    assert is_instruction_path(str(tmp_path / ".github" / "copilot-instructions.md"), repo)
    assert not is_instruction_path(str(tmp_path / "README.md"), repo)


def test_reconcile_replaces_live_and_frozen_prompt(tmp_path):
    (tmp_path / "AGENTS.md").write_text("rule-one\n", encoding="utf-8")
    cfg = HarnessConfig(repo=str(tmp_path), state_dir=str(tmp_path / "state"))
    session = ConversationalSession(cfg)
    assert "rule-one" in session._history[0]["content"]
    session._frozen_system_prompt = session._history[0]["content"]
    (tmp_path / "AGENTS.md").write_text("rule-two\n", encoding="utf-8")
    assert reconcile_workspace_rules(session) is True
    assert "rule-two" in session._history[0]["content"]
    assert "rule-one" not in session._history[0]["content"]
    assert "rule-two" in session._frozen_system_prompt
    assert session._workspace_rules_block == load_workspace_rules(str(tmp_path))


def test_reconcile_is_noop_when_unchanged(tmp_path):
    (tmp_path / "AGENTS.md").write_text("stable\n", encoding="utf-8")
    cfg = HarnessConfig(repo=str(tmp_path), state_dir=str(tmp_path / "state"))
    session = ConversationalSession(cfg)
    assert reconcile_workspace_rules(session) is False


def test_maybe_refresh_ignores_ordinary_files(tmp_path):
    (tmp_path / "AGENTS.md").write_text("keep\n", encoding="utf-8")
    cfg = HarnessConfig(repo=str(tmp_path), state_dir=str(tmp_path / "state"))
    session = ConversationalSession(cfg)
    (tmp_path / "src.py").write_text("x = 1\n", encoding="utf-8")
    assert maybe_refresh_workspace_rules(session, str(tmp_path / "src.py")) is False
    assert "keep" in session._history[0]["content"]
