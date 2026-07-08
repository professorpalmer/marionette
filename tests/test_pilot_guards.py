"""Tests for harness/pilot_guards.py — loop breaker, swarm gate, delegate gate, budget."""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from harness.pilot_guards import (
    BROAD_SWARM_ROLES,
    DELEGATE_THRESHOLD,
    IterationBudget,
    LOOP_REPEAT_CAP,
    SWARM_GATE_READ_ALLOWANCE,
    TurnGuardState,
    check_cli_redirect,
    check_delegate_gate,
    check_iteration_budget,
    check_loop_guard,
    check_pilot_guards,
    check_swarm_gate,
    cli_redirect_enabled,
    delegate_gate_enabled,
    guards_active,
    is_broad_intent_user_message,
    is_exploration_command,
    is_native_exploration,
    is_puppetmaster_cli_command,
    is_swarm_gate_blocked_exploration,
    iteration_budget_enabled,
    loop_guard_enabled,
    new_turn_guard_state,
    normalize_action_args,
    puppetmaster_cli_native_mapping,
    record_action_execution,
    swarm_gate_enabled,
    turn_tool_budget_cap,
)


@dataclass
class _Act:
    kind: str = ""
    path: str = ""
    command: str = ""
    query: str = ""
    goal: str = ""
    goals: list = field(default_factory=list)
    roles: list = field(default_factory=list)
    arguments: dict = field(default_factory=dict)
    start_line: int | None = None
    limit: int | None = None


@pytest.mark.parametrize(
    "message",
    [
        "Give me an audit of this directory",
        "Please review the codebase for security issues",
        "Look through the repo and find problems",
        "Find all places we handle auth",
        "Map the pipeline architecture",
        "What could break if we ship this?",
        "Do a sweep of error handling",
        "Draft a refactor plan for the harness",
        "Improve quality across the project",
    ],
)
def test_broad_intent_classification_positives(message):
    assert is_broad_intent_user_message(message) is True


@pytest.mark.parametrize(
    "message",
    [
        "hi",
        "thanks!",
        "Where is PilotAction defined?",
        "What calls normalize_action_args?",
        "How does check_loop_guard work?",
        "Show me the function parse_turn_budget",
        "Find the class TurnGuardState",
    ],
)
def test_broad_intent_classification_negatives(message):
    assert is_broad_intent_user_message(message) is False


def test_swarm_gate_disabled_by_env(monkeypatch):
    monkeypatch.delenv("HARNESS_SWARM_GATE", raising=False)
    assert swarm_gate_enabled() is True
    monkeypatch.setenv("HARNESS_SWARM_GATE", "0")
    assert swarm_gate_enabled() is False


def test_iteration_budget_cap_from_env(monkeypatch):
    monkeypatch.setenv("HARNESS_PILOT_TOOL_BUDGET", "10")
    assert turn_tool_budget_cap() == 10
    monkeypatch.delenv("HARNESS_PILOT_TOOL_BUDGET", raising=False)
    monkeypatch.setenv("HARNESS_TURN_BUDGET", "15")
    assert turn_tool_budget_cap() == 15
    monkeypatch.setenv("HARNESS_PILOT_TOOL_BUDGET", "0")
    assert turn_tool_budget_cap() == 0
    assert iteration_budget_enabled() is False


def test_swarm_gate_suppresses_list_dir_before_dispatch():
    state = new_turn_guard_state("Give me an audit of this directory")
    act = _Act(kind="list_dir", path=".")
    verdict = check_swarm_gate(state, "list_dir", act)
    assert verdict.suppress is True
    assert verdict.reason == "swarm_gate"
    assert "run_swarm" in verdict.message
    for role in BROAD_SWARM_ROLES:
        assert role in verdict.message


def test_swarm_gate_allows_two_reads_then_blocks():
    state = new_turn_guard_state("Review the platform for regressions")
    for i in range(SWARM_GATE_READ_ALLOWANCE):
        act = _Act(kind="read_file", path=f"a{i}.py")
        assert check_swarm_gate(state, "read_file", act).suppress is False
        record_action_execution(state, "read_file", act)

    blocked = _Act(kind="read_file", path="extra.py")
    verdict = check_swarm_gate(state, "read_file", blocked)
    assert verdict.suppress is True
    assert verdict.reason == "swarm_gate"


def test_swarm_gate_unlocks_after_swarm_dispatch():
    state = new_turn_guard_state("Audit the harness directory")
    record_action_execution(state, "run_swarm", _Act(kind="run_swarm", goal="map harness"))
    assert state.swarm_dispatched is True

    verdict = check_swarm_gate(state, "list_dir", _Act(kind="list_dir", path="."))
    assert verdict.suppress is False


def test_swarm_gate_off_allows_exploration(monkeypatch):
    monkeypatch.setenv("HARNESS_SWARM_GATE", "0")
    state = new_turn_guard_state("Give me an audit of this directory")
    verdict = check_swarm_gate(state, "list_dir", _Act(kind="list_dir", path="."))
    assert verdict.suppress is False


def test_swarm_gate_not_active_for_narrow_message():
    state = new_turn_guard_state("Where is TurnGuardState defined?")
    assert state.broad_intent is False
    verdict = check_swarm_gate(state, "list_dir", _Act(kind="list_dir", path="."))
    assert verdict.suppress is False


def test_iteration_budget_blocks_after_cap():
    budget = IterationBudget(cap=3)
    state = TurnGuardState(iteration_budget=budget)
    for _ in range(3):
        assert check_iteration_budget(state, "read_file", _Act()).suppress is False
        record_action_execution(state, "read_file", _Act(kind="read_file", path="x.py"))

    verdict = check_iteration_budget(state, "run_swarm", _Act(kind="run_swarm", goal="go"))
    assert verdict.suppress is True
    assert verdict.reason == "budget"
    assert "budget exhausted" in verdict.message


def test_iteration_budget_consume_refund():
    budget = IterationBudget(cap=2)
    assert budget.consume() is True
    assert budget.used == 1
    assert budget.remaining == 1
    budget.refund()
    assert budget.used == 0
    assert budget.consume() is True
    assert budget.consume() is True
    assert budget.consume() is False


def test_per_turn_reset_includes_broad_intent_and_budget():
    turn1 = new_turn_guard_state("Audit the repo")
    record_action_execution(turn1, "list_dir", _Act(kind="list_dir", path="."))
    assert turn1.broad_intent is True

    turn2 = new_turn_guard_state("Where is foo defined?")
    assert turn2.broad_intent is False
    assert turn2.iteration_budget is not None
    assert turn2.iteration_budget.used == 0


def test_check_pilot_guards_swarm_gate_before_delegate():
    state = new_turn_guard_state("Give me an audit of this directory")
    verdict = check_pilot_guards(state, "list_dir", _Act(kind="list_dir", path="."))
    assert verdict.suppress is True
    assert verdict.reason == "swarm_gate"


def test_loop_guard_disabled_by_env(monkeypatch):
    monkeypatch.delenv("HARNESS_LOOP_GUARD", raising=False)
    assert loop_guard_enabled() is True
    monkeypatch.setenv("HARNESS_LOOP_GUARD", "0")
    assert loop_guard_enabled() is False


def test_delegate_gate_disabled_by_env(monkeypatch):
    monkeypatch.delenv("HARNESS_DELEGATE_GATE", raising=False)
    assert delegate_gate_enabled() is True
    monkeypatch.setenv("HARNESS_DELEGATE_GATE", "0")
    assert delegate_gate_enabled() is False


def test_guards_active_reflects_either_switch(monkeypatch):
    monkeypatch.setenv("HARNESS_LOOP_GUARD", "1")
    monkeypatch.setenv("HARNESS_DELEGATE_GATE", "1")
    monkeypatch.setenv("HARNESS_SWARM_GATE", "1")
    monkeypatch.setenv("HARNESS_PILOT_TOOL_BUDGET", "25")
    monkeypatch.setenv("HARNESS_CLI_REDIRECT", "1")
    assert guards_active() is True
    monkeypatch.setenv("HARNESS_LOOP_GUARD", "0")
    monkeypatch.setenv("HARNESS_DELEGATE_GATE", "0")
    monkeypatch.setenv("HARNESS_SWARM_GATE", "0")
    monkeypatch.setenv("HARNESS_PILOT_TOOL_BUDGET", "0")
    monkeypatch.setenv("HARNESS_CLI_REDIRECT", "0")
    assert guards_active() is False


def test_normalize_near_identical_paths():
    a = _Act(kind="read_file", path="src/Foo.py")
    b = _Act(kind="read_file", path="src\\foo.py")
    assert normalize_action_args("read_file", a) == normalize_action_args("read_file", b)


def test_loop_suppresses_identical_repeat():
    """Without a cached successful result, an identical repeat still hard-suppresses.

    Intentional: loop-guard replay only fires when a prior SUCCESSFUL result was
    recorded for the same (kind, args); otherwise the old SUPPRESSED path remains.
    """
    state = new_turn_guard_state()
    act = _Act(kind="read_file", path="main.py")

    assert check_loop_guard(state, "read_file", act).suppress is False
    record_action_execution(state, "read_file", act)

    verdict = check_loop_guard(state, "read_file", act)
    assert verdict.suppress is True
    assert verdict.reason == "loop"
    assert "SUPPRESSED" in verdict.message
    assert "run_swarm" in verdict.message


def test_loop_replays_identical_successful_call():
    """Second identical successful call returns cached content, not SUPPRESSED."""
    from harness.pilot_guards import record_successful_result

    state = new_turn_guard_state()
    act = _Act(kind="read_file", path="main.py")
    record_action_execution(state, "read_file", act)
    record_successful_result(state, "read_file", act, "(read_file main.py returned)\nhello")

    verdict = check_loop_guard(state, "read_file", act)
    assert verdict.suppress is True
    assert verdict.replay is True
    assert verdict.reason == "loop_replay"
    assert "[cached repeat of identical call]" in verdict.message
    assert "hello" in verdict.message
    assert "SUPPRESSED" not in verdict.message


def test_loop_hard_suppresses_after_repeat_cap():
    """The (LOOP_REPEAT_CAP + 1)th identical call hard-suppresses after replays."""
    from harness.pilot_guards import record_successful_result

    state = new_turn_guard_state()
    act = _Act(kind="read_file", path="main.py")
    # Original + (CAP - 1) replays already recorded => prior == CAP
    for _ in range(LOOP_REPEAT_CAP):
        record_action_execution(state, "read_file", act)
        record_successful_result(state, "read_file", act, "cached body")

    verdict = check_loop_guard(state, "read_file", act)
    assert verdict.suppress is True
    assert verdict.replay is False
    assert verdict.reason == "loop"
    assert "SUPPRESSED" in verdict.message


def test_loop_suppresses_near_identical_repeat():
    state = new_turn_guard_state()
    first = _Act(kind="read_file", path="pkg/mod.py", start_line=10, limit=20)
    near = _Act(kind="read_file", path="pkg\\mod.py", start_line=10, limit=20)

    record_action_execution(state, "read_file", first)
    verdict = check_loop_guard(state, "read_file", near)
    assert verdict.suppress is True


def test_loop_guard_off_allows_repeats(monkeypatch):
    monkeypatch.setenv("HARNESS_LOOP_GUARD", "0")
    state = new_turn_guard_state()
    act = _Act(kind="read_file", path="x.py")
    record_action_execution(state, "read_file", act)
    assert check_loop_guard(state, "read_file", act).suppress is False


def test_loop_repeat_cap_constant_documented():
    assert LOOP_REPEAT_CAP >= 1


def test_delegate_gate_trips_after_threshold():
    state = new_turn_guard_state()
    for i in range(DELEGATE_THRESHOLD):
        act = _Act(kind="read_file", path=f"f{i}.py")
        assert check_delegate_gate(state, "read_file", act).suppress is False
        record_action_execution(state, "read_file", act)

    blocked = _Act(kind="search_files", query="pattern")
    verdict = check_delegate_gate(state, "search_files", blocked)
    assert verdict.suppress is True
    assert verdict.reason == "delegate"
    assert "search_codegraph" in verdict.message
    assert "run_swarm" in verdict.message


def test_delegate_gate_counts_exploration_run_command():
    state = new_turn_guard_state()
    assert is_exploration_command("rg foo bar")
    assert is_exploration_command("find . -name '*.py'")
    assert not is_exploration_command("pytest -q")

    for _ in range(DELEGATE_THRESHOLD):
        act = _Act(kind="run_command", command="rg needle haystack")
        assert check_delegate_gate(state, "run_command", act).suppress is False
        record_action_execution(state, "run_command", act)

    verdict = check_delegate_gate(state, "run_command", _Act(kind="run_command", command="grep x"))
    assert verdict.suppress is True


def test_exempt_tools_never_suppressed_by_delegate_gate():
    state = new_turn_guard_state()
    for i in range(DELEGATE_THRESHOLD + 2):
        record_action_execution(state, "read_file", _Act(kind="read_file", path=f"a{i}.py"))

    for kind, act in [
        ("search_codegraph", _Act(kind="search_codegraph", query="PilotAction")),
        ("query_wiki", _Act(kind="query_wiki", arguments={"question": "auth flow?"})),
        ("run_swarm", _Act(kind="run_swarm", goal="map auth")),
        ("run_implement", _Act(kind="run_implement", goal="fix bug")),
        ("run_parallel", _Act(kind="run_parallel", goals=["a", "b"])),
        ("route_task", _Act(kind="route_task", arguments={"instruction": "plan refactor"})),
    ]:
        assert check_delegate_gate(state, kind, act).suppress is False


def test_delegation_seen_disables_delegate_gate_for_exploration():
    state = new_turn_guard_state()
    for i in range(DELEGATE_THRESHOLD):
        record_action_execution(state, "read_file", _Act(kind="read_file", path=f"z{i}.py"))

    record_action_execution(state, "search_codegraph", _Act(kind="search_codegraph", query="foo"))
    assert state.delegation_seen is True

    verdict = check_delegate_gate(state, "read_file", _Act(kind="read_file", path="more.py"))
    assert verdict.suppress is False


def test_delegate_gate_off_allows_exploration_spree(monkeypatch):
    monkeypatch.setenv("HARNESS_DELEGATE_GATE", "0")
    state = new_turn_guard_state()
    for i in range(DELEGATE_THRESHOLD + 3):
        act = _Act(kind="read_file", path=f"n{i}.py")
        record_action_execution(state, "read_file", act)
        assert check_delegate_gate(state, "read_file", act).suppress is False


def test_per_turn_reset():
    turn1 = new_turn_guard_state()
    act = _Act(kind="read_file", path="same.py")
    record_action_execution(turn1, "read_file", act)
    assert check_loop_guard(turn1, "read_file", act).suppress is True

    turn2 = new_turn_guard_state()
    assert check_loop_guard(turn2, "read_file", act).suppress is False


def test_check_pilot_guards_loop_before_delegate():
    state = new_turn_guard_state()
    act = _Act(kind="read_file", path="dup.py")
    record_action_execution(state, "read_file", act)
    verdict = check_pilot_guards(state, "read_file", act)
    assert verdict.suppress is True
    assert verdict.reason == "loop"


def test_is_native_exploration_classification():
    assert is_native_exploration("read_file", _Act())
    assert is_native_exploration("list_dir", _Act())
    assert is_native_exploration("search_files", _Act())
    assert is_native_exploration("run_command", _Act(command="rg foo"))
    assert not is_native_exploration("run_command", _Act(command="npm test"))
    assert not is_native_exploration("write_file", _Act())


def test_session_suppresses_duplicate_read(monkeypatch, tmp_path):
    """End-to-end: duplicate read_file in one turn replays the cached result.

    Intentional behavior change: identical successful calls now return a cached
    replay (token bleed fix) instead of a SUPPRESSED error. Hard-suppress still
    fires after LOOP_REPEAT_CAP identical executions in the same turn.
    """
    import json
    import os
    import tempfile

    from harness.config import HarnessConfig
    from harness.conversation import ConversationalSession

    monkeypatch.setenv("HARNESS_LOOP_GUARD", "1")
    monkeypatch.setenv("HARNESS_DELEGATE_GATE", "0")
    repo = os.path.realpath(tmp_path)
    target = os.path.join(repo, "dup.txt")
    with open(target, "w", encoding="utf-8") as f:
        f.write("hello")

    cfg = HarnessConfig(repo=repo, swarm_adapter="demo", state_dir=tempfile.mkdtemp())
    session = ConversationalSession(cfg)

    class DuplicatePilot:
        def complete(self, prompt, system=None):
            from pmharness.drivers.openai_compat import DriverResponse
            return DriverResponse(
                text=json.dumps({
                    "say": "reading twice",
                    "actions": [
                        {"kind": "read_file", "path": "dup.txt"},
                        {"kind": "read_file", "path": "dup.txt"},
                    ],
                }),
                tokens_out=10,
                latency_ms=1.0,
            )

    session.pilot = DuplicatePilot()
    events = list(session.send("go"))
    # Second identical read should be a cached replay, not a SUPPRESSED error.
    errors = [e.data.get("error", "") for e in events if e.kind == "action_result"]
    assert not any("SUPPRESSED" in (r or "") for r in errors)
    history_text = " ".join(
        m.get("content", "") for m in session._history if isinstance(m.get("content"), str)
    )
    assert "[cached repeat of identical call]" in history_text


@pytest.mark.parametrize(
    "command",
    [
        "python -m puppetmaster swarm --goal map auth",
        "puppetmaster cursor --implement",
        "puppetmaster.exe status",
        "python -m puppetmaster route --instruction plan refactor",
    ],
)
def test_puppetmaster_cli_detection_positives(command):
    assert is_puppetmaster_cli_command(command) is True


@pytest.mark.parametrize(
    "command",
    [
        "python -m pytest -q",
        "npm run build",
        "echo hello",
        "git status",
    ],
)
def test_puppetmaster_cli_detection_negatives(command):
    assert is_puppetmaster_cli_command(command) is False


@pytest.mark.parametrize(
    "command,expected_kind,expected_fragment",
    [
        (
            "python -m puppetmaster swarm --goal map auth",
            "run_swarm",
            'goal="...", roles=["explore","pipeline-mapper"]',
        ),
        (
            "puppetmaster cursor --implement",
            "run_implement",
            'goal="..."',
        ),
        (
            "puppetmaster.exe status",
            "action_result",
            "action_result/swarm_result",
        ),
        (
            "python -m puppetmaster route --instruction plan",
            "route_task",
            'instruction="..."',
        ),
    ],
)
def test_cli_redirect_mapping(command, expected_kind, expected_fragment):
    native_kind, example = puppetmaster_cli_native_mapping(command)
    assert native_kind == expected_kind
    verdict = check_cli_redirect(new_turn_guard_state(), "run_command", _Act(command=command))
    assert verdict.suppress is True
    assert verdict.reason == "cli_redirect"
    assert expected_kind in verdict.message or expected_fragment in verdict.message
    assert expected_fragment in verdict.message or expected_fragment in example


def test_cli_redirect_status_names_in_history_records():
    """Status/artifacts CLI redirect must name action_result/swarm_result in history."""
    verdict = check_cli_redirect(
        new_turn_guard_state(),
        "run_command",
        _Act(command="puppetmaster status"),
    )
    assert verdict.suppress is True
    assert "action_result" in verdict.message
    assert "swarm_result" in verdict.message
    assert "search_state" in verdict.message
    assert "ALREADY" in verdict.message or "already" in verdict.message.lower()


def test_cli_redirect_kill_switch(monkeypatch):
    monkeypatch.setenv("HARNESS_CLI_REDIRECT", "0")
    assert cli_redirect_enabled() is False
    act = _Act(kind="run_command", command="python -m puppetmaster swarm")
    verdict = check_cli_redirect(new_turn_guard_state(), "run_command", act)
    assert verdict.suppress is False


def test_cli_redirect_before_swarm_gate_on_broad_turn():
    state = new_turn_guard_state("Give me an audit of this directory")
    act = _Act(kind="run_command", command="python -m puppetmaster swarm --goal map")
    verdict = check_pilot_guards(state, "run_command", act)
    assert verdict.suppress is True
    assert verdict.reason == "cli_redirect"


@pytest.mark.parametrize(
    "command",
    [
        "echo hello",
        "ls",
        "ls -1",
        "dir",
    ],
)
def test_echo_and_dir_probes_count_as_exploration(command):
    assert is_exploration_command(command) is True


@pytest.mark.parametrize(
    "command",
    [
        "echo hello",
        "ls -1",
        "dir",
    ],
)
def test_swarm_gate_blocks_echo_and_dir_probes_on_broad_turn(command):
    state = new_turn_guard_state("Give me an audit of this directory")
    act = _Act(kind="run_command", command=command)
    assert is_swarm_gate_blocked_exploration(state, "run_command", act) is True
    verdict = check_swarm_gate(state, "run_command", act)
    assert verdict.suppress is True
    assert verdict.reason == "swarm_gate"


def test_echo_not_blocked_on_narrow_turn():
    state = new_turn_guard_state("Where is TurnGuardState defined?")
    act = _Act(kind="run_command", command="echo hello")
    assert is_swarm_gate_blocked_exploration(state, "run_command", act) is False
    verdict = check_swarm_gate(state, "run_command", act)
    assert verdict.suppress is False
