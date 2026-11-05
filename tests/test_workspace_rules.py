import os
import pytest
from harness.config import HarnessConfig
from harness.conversation import ConversationalSession, load_workspace_rules

def test_workspace_rules_loads_correctly(tmp_path):
    # Setup a temp repo with AGENTS.md containing "no emojis"
    repo_dir = tmp_path / "my_repo"
    repo_dir.mkdir()
    
    agents_file = repo_dir / "AGENTS.md"
    agents_file.write_text("no emojis please")
    
    claude_file = repo_dir / "CLAUDE.md"
    claude_file.write_text("claude rules")
    
    # Let's test the load_workspace_rules function directly first
    rules = load_workspace_rules(str(repo_dir))
    assert "# Workspace rules (from AGENTS.md)" in rules
    assert "no emojis please" in rules
    assert "# Workspace rules (from CLAUDE.md)" in rules
    assert "claude rules" in rules

    # Test missing files are skipped, e.g. .cursorrules or .github/copilot-instructions.md are not in block
    assert ".cursorrules" not in rules

    # Build a config and session
    config = HarnessConfig()
    config.repo = str(repo_dir)
    session = ConversationalSession(config)
    
    system_prompt = session._history[0]["content"]
    assert "# Workspace rules (from AGENTS.md)" in system_prompt
    assert "no emojis please" in system_prompt


def test_workspace_rules_caps_size(tmp_path):
    repo_dir = tmp_path / "my_repo_cap"
    repo_dir.mkdir()
    
    # 9KB file (above 8KB limit)
    large_agents = "x" * 9000
    (repo_dir / "AGENTS.md").write_text(large_agents)
    
    # Another 9KB file
    large_claude = "y" * 9000
    (repo_dir / "CLAUDE.md").write_text(large_claude)
    
    rules = load_workspace_rules(str(repo_dir))
    # AGENTS.md should be capped to 8KB (8192 bytes)
    assert "# Workspace rules (from AGENTS.md)" in rules
    # Total size read should be capped at 16KB (16384 bytes)
    # The content of AGENTS.md (8192 bytes) plus CLAUDE.md (8192 bytes) is 16384 bytes
    assert len(rules) >= 16384
    # Let's verify AGENTS.md block length is around 8192 chars
    agents_block_start = rules.find("# Workspace rules (from AGENTS.md)\n") + len("# Workspace rules (from AGENTS.md)\n")
    claude_block_start = rules.find("# Workspace rules (from CLAUDE.md)\n")
    agents_content = rules[agents_block_start:claude_block_start].strip()
    rule_text, marker = agents_content.split("\n[cut here:", 1)
    assert rule_text == "x" * 8192
    assert marker.startswith(" AGENTS.md has more text.")


def test_workspace_rules_no_repo_no_crash():
    # unset repo -> no crash, no block
    rules = load_workspace_rules(None)
    assert rules == ""
    
    config = HarnessConfig()
    config.repo = None
    session = ConversationalSession(config)
    system_prompt = session._history[0]["content"]
    assert "Workspace rules" not in system_prompt
