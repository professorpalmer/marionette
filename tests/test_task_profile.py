"""Adaptive task-depth MICRO / STANDARD / DEEP (hermetic)."""
from __future__ import annotations

from types import SimpleNamespace

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.pilot_guards import check_swarm_gate, new_turn_guard_state
from harness.task_profile import (
    DEEP,
    MICRO,
    STANDARD,
    classify_task_profile,
    maybe_escalate,
    micro_visible_tool_names,
    profile_disables_swarm_gate,
)
from harness.tool_discovery import core_visible_names
from harness.wiki import WikiClient


def test_classify_typo_readme_is_micro():
    assert classify_task_profile("typo in README.md") == MICRO


def test_classify_trivial_ping_is_micro():
    assert classify_task_profile("test") == MICRO
    assert classify_task_profile("pong") == MICRO
    assert classify_task_profile("ok") == MICRO
    assert classify_task_profile("add OAuth support") == STANDARD


def test_classify_add_oauth_is_standard():
    assert classify_task_profile("add OAuth support") == STANDARD


def test_classify_audit_architecture_is_deep():
    assert classify_task_profile("audit authentication architecture") == DEEP


def test_classify_override_beats_heuristics():
    assert classify_task_profile("audit authentication architecture", override="micro") == MICRO
    assert classify_task_profile("typo in README.md", override="deep") == DEEP
    assert classify_task_profile("typo in README.md", override="auto") == MICRO
    assert classify_task_profile("add OAuth support", override="STANDARD") == STANDARD


def test_escalate_micro_three_files_to_standard():
    assert maybe_escalate(MICRO, files_touched=3) == STANDARD


def test_escalate_never_demotes():
    assert maybe_escalate(DEEP, files_touched=0) == DEEP
    assert maybe_escalate(STANDARD, files_touched=0) == STANDARD
    assert maybe_escalate(STANDARD, files_touched=2, tests_failed=True) == STANDARD


def test_escalate_standard_to_deep():
    assert maybe_escalate(STANDARD, files_touched=8) == DEEP
    assert maybe_escalate(STANDARD, user_wants_deep=True) == DEEP
    assert maybe_escalate(MICRO, files_touched=8) == DEEP


def test_micro_visible_tool_names_contract():
    names = micro_visible_tool_names()
    assert "search_tools" in names
    assert "search_files" in names
    assert "run_swarm" not in names
    assert "query_wiki" not in names
    assert "search_codegraph" not in names
    assert "run_implement" not in names
    assert "run_parallel" not in names


def test_core_visible_names_micro_hides_orchestration():
    names = core_visible_names(profile=MICRO)
    assert "search_tools" in names
    assert "read_file" in names
    assert "run_swarm" not in names
    assert "query_wiki" not in names
    assert "run_implement" not in names
    standard = core_visible_names(profile=STANDARD)
    assert "run_swarm" in standard
    assert "query_wiki" in standard


def test_swarm_gate_allows_exploration_when_micro(monkeypatch):
    monkeypatch.delenv("HARNESS_SWARM_GATE", raising=False)
    prompt = "audit authentication architecture across the codebase"
    state = new_turn_guard_state(prompt, task_profile=MICRO)
    assert state.broad_intent is True
    assert profile_disables_swarm_gate(state.task_profile) is True
    verdict = check_swarm_gate(
        state, "list_dir", SimpleNamespace(kind="list_dir", path=".")
    )
    assert verdict.suppress is False

    # Same turn without MICRO still suppresses (sanity).
    blocked = new_turn_guard_state(prompt, task_profile=STANDARD)
    assert check_swarm_gate(
        blocked, "list_dir", SimpleNamespace(kind="list_dir", path=".")
    ).suppress is True


def test_wiki_section_empty_when_micro_even_if_configured(tmp_path, monkeypatch):
    cfg = HarnessConfig(
        driver="stub-oracle-v2",
        state_dir=str(tmp_path),
        repo=str(tmp_path / "repo"),
    )
    s = ConversationalSession(cfg)
    s._wiki = WikiClient(base_url="https://wiki.example.com", token="tok")
    s._task_profile = MICRO

    def boom(query, *, limit=5):
        raise AssertionError("wiki search must not run on MICRO")

    monkeypatch.setattr(s._wiki, "search_pages", boom)
    assert s._wiki.configured is True
    assert s._build_turn_wiki_section("what did we decide about auth?") == ""


def test_cli_micro_sets_task_profile(monkeypatch):
    captured = {}

    class FakeSession:
        def __init__(self, cfg):
            captured["cfg"] = cfg

        def preflight(self):
            return None

        def run(self, *a, **k):
            return iter([])

    monkeypatch.setattr("harness.cli.Session", FakeSession)
    from harness.cli import main

    code = main(["--micro", "--driver", "stub-oracle-v2", "typo in README.md"])
    assert code == 2  # no final action from empty run
    assert captured["cfg"].task_profile == "micro"


def test_config_from_env_task_profile(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_CONFIG", str(tmp_path / "missing.json"))
    monkeypatch.delenv("HARNESS_TASK_PROFILE", raising=False)
    assert HarnessConfig.from_env().task_profile == "auto"
    monkeypatch.setenv("HARNESS_TASK_PROFILE", "deep")
    assert HarnessConfig.from_env().task_profile == "deep"
