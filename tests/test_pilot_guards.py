"""Tests for harness/pilot_guards.py — loop breaker, swarm gate, delegate gate, budget."""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from harness.pilot_guards import (
    BROAD_SWARM_ROLES,
    DELEGATE_THRESHOLD,
    EDIT_FIRST_READ_ALLOWANCE_DEFAULT,
    IterationBudget,
    LOOP_REPEAT_CAP,
    POST_IMPLEMENT_TOOL_ALLOWANCE_DEFAULT,
    SWARM_GATE_FULL_REDIRECT_CAP,
    SWARM_GATE_READ_ALLOWANCE,
    TINY_WORKSPACE_TOOL_BUDGET_DEFAULT,
    TURN_TOOL_BUDGET_DEFAULT,
    TurnGuardState,
    check_backend_restart,
    check_chrome_file_smoke,
    check_cli_redirect,
    check_delegate_gate,
    check_edit_first,
    check_implement_exhausted,
    check_iteration_budget,
    check_loop_guard,
    check_pilot_guards,
    check_swarm_gate,
    clamp_post_implement_iteration_budget,
    cli_redirect_enabled,
    delegate_gate_enabled,
    guards_active,
    is_backend_restart_command,
    is_broad_intent_user_message,
    is_exploration_command,
    is_headless_chrome_file_smoke_command,
    is_local_handoff_command,
    is_native_exploration,
    is_puppetmaster_cli_command,
    is_swarm_gate_blocked_exploration,
    is_tiny_workspace,
    iteration_budget_enabled,
    job_result_shows_implement_success,
    loop_guard_enabled,
    mid_turn_restart_blocked,
    new_turn_guard_state,
    normalize_action_args,
    note_implement_exhausted_from_provenance,
    note_implement_success_from_job_result,
    note_kernel_recovery,
    note_kernel_recovery_from_result,
    result_shows_kernel_failure,
    post_implement_tool_allowance,
    puppetmaster_cli_native_mapping,
    record_action_execution,
    swarm_gate_enabled,
    tiny_workspace_tool_budget,
    turn_tool_budget_cap,
    user_requests_browser_qa,
    workspace_source_stats,
)


@dataclass
class _Act:
    kind: str = ""
    path: str = ""
    command: str = ""
    query: str = ""
    goal: str = ""
    model: str = ""
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
        "Find out why the browser fails on Windows",
        "Figure out the auth timeout",
        "Dig into the session lifecycle",
        "Trace the request path through the harness",
        "Investigate the flaky browser integration",
        "How does inherit=True affect subprocess behaviour?",
        "Processes are impacting each other via subprocess inherit",
        "Compare Windows vs Mac browser launch",
        (
            "marionette still having the issue with the browser not working on "
            "windows. doesn't have this issue on Mac... Find out for me"
        ),
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


def test_investigate_shaped_turn_swarm_gate_suppresses_exploration():
    """Cross-platform investigate prompt must trip broad intent + swarm gate."""
    prompt = (
        "marionette still having the issue with the browser not working on "
        "windows. doesn't have this issue on Mac... Find out for me"
    )
    assert is_broad_intent_user_message(prompt) is True
    state = new_turn_guard_state(prompt)
    assert state.broad_intent is True

    first = check_swarm_gate(state, "list_dir", _Act(kind="list_dir", path="."))
    assert first.suppress is True
    assert first.reason == "swarm_gate"
    assert first.replay is False

    second = check_swarm_gate(
        state, "search_files", _Act(kind="search_files", query="browser")
    )
    assert second.suppress is True
    assert second.reason == "swarm_gate_replay"
    assert second.replay is True


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


def test_turn_tool_budget_default_hermetic_after_import_time_zero(monkeypatch):
    """Ambient HARNESS_PILOT_TOOL_BUDGET=0 must not bake into TURN_TOOL_BUDGET_DEFAULT.

    Reload with env=0 (simulating Settings at import), then delenv — default 25
    and an enabled iteration budget must return, matching conftest isolation.
    """
    import importlib

    import harness.pilot_guards as pilot_guards

    monkeypatch.setenv("HARNESS_PILOT_TOOL_BUDGET", "0")
    importlib.reload(pilot_guards)
    try:
        assert pilot_guards.TURN_TOOL_BUDGET_DEFAULT == 25
        monkeypatch.delenv("HARNESS_PILOT_TOOL_BUDGET", raising=False)
        monkeypatch.delenv("HARNESS_TURN_BUDGET", raising=False)
        assert pilot_guards.turn_tool_budget_cap() == 25
        assert pilot_guards.iteration_budget_enabled() is True
        state = pilot_guards.new_turn_guard_state("do a thing")
        assert state.iteration_budget is not None
        assert state.iteration_budget.cap == 25
    finally:
        monkeypatch.delenv("HARNESS_PILOT_TOOL_BUDGET", raising=False)
        monkeypatch.delenv("HARNESS_TURN_BUDGET", raising=False)
        importlib.reload(pilot_guards)


def test_swarm_gate_suppresses_list_dir_before_dispatch():
    state = new_turn_guard_state("Give me an audit of this directory")
    act = _Act(kind="list_dir", path=".")
    verdict = check_swarm_gate(state, "list_dir", act)
    assert verdict.suppress is True
    assert verdict.reason == "swarm_gate"
    assert verdict.replay is False
    assert "run_swarm" in verdict.message
    assert "STOP exploring" in verdict.message
    assert state.swarm_gate_suppress_count == 1
    for role in BROAD_SWARM_ROLES:
        assert role in verdict.message


def test_swarm_gate_subsequent_suppressions_use_short_replay():
    """After the first full redirect, further exploration is a cheap cached replay."""
    state = new_turn_guard_state("Give me an audit of this directory")
    first = check_swarm_gate(state, "list_dir", _Act(kind="list_dir", path="."))
    assert first.suppress is True
    assert first.reason == "swarm_gate"
    assert first.replay is False
    assert first.message.startswith("(SUPPRESSED")
    assert state.swarm_gate_suppress_count == SWARM_GATE_FULL_REDIRECT_CAP

    second = check_swarm_gate(state, "search_files", _Act(kind="search_files", query="foo"))
    assert second.suppress is True
    assert second.reason == "swarm_gate_replay"
    assert second.replay is True
    assert second.message.startswith("[swarm_gate redirect already issued")
    assert "run_swarm" in second.message
    assert len(second.message) < len(first.message)
    assert state.swarm_gate_suppress_count == SWARM_GATE_FULL_REDIRECT_CAP + 1

    third = check_swarm_gate(
        state, "run_command", _Act(kind="run_command", command="rg TODO")
    )
    assert third.suppress is True
    assert third.reason == "swarm_gate_replay"
    assert third.replay is True
    assert len(third.message) < len(first.message)


def test_swarm_gate_replay_does_not_apply_when_narrow():
    state = new_turn_guard_state("Where is TurnGuardState defined?")
    assert state.broad_intent is False
    first = check_swarm_gate(state, "list_dir", _Act(kind="list_dir", path="."))
    assert first.suppress is False
    assert state.swarm_gate_suppress_count == 0
    second = check_swarm_gate(state, "search_files", _Act(kind="search_files", query="x"))
    assert second.suppress is False
    assert state.swarm_gate_suppress_count == 0


def test_swarm_gate_allows_search_codegraph_on_broad_turn():
    """search_codegraph stays open even after native exploration is redirected."""
    state = new_turn_guard_state("Give me an audit of this directory")
    assert state.broad_intent is True
    blocked = check_swarm_gate(state, "list_dir", _Act(kind="list_dir", path="."))
    assert blocked.suppress is True
    assert state.swarm_gate_suppress_count == 1

    act = _Act(kind="search_codegraph", query="TurnGuardState")
    assert check_swarm_gate(state, "search_codegraph", act).suppress is False
    assert check_pilot_guards(state, "search_codegraph", act).suppress is False
    # Suppress count must not advance for allowed tools.
    assert state.swarm_gate_suppress_count == 1


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
    assert verdict.replay is False

    # Further over-allowance reads reuse the short cached redirect.
    again = check_swarm_gate(state, "read_file", _Act(kind="read_file", path="extra2.py"))
    assert again.suppress is True
    assert again.reason == "swarm_gate_replay"
    assert again.replay is True


def test_swarm_gate_keeps_blocking_sweeps_after_dispatch():
    """After dispatch, list_dir/search_files/grep stay blocked; read_file unlocks."""
    state = new_turn_guard_state("Audit the harness directory")
    record_action_execution(state, "run_swarm", _Act(kind="run_swarm", goal="map harness"))
    assert state.swarm_dispatched is True

    list_verdict = check_swarm_gate(state, "list_dir", _Act(kind="list_dir", path="."))
    assert list_verdict.suppress is True
    assert list_verdict.reason == "swarm_gate"
    assert "re-dispatch" in list_verdict.message.lower()
    assert "inline exploration" in list_verdict.message.lower()

    search_verdict = check_swarm_gate(
        state, "search_files", _Act(kind="search_files", query="TODO")
    )
    assert search_verdict.suppress is True
    assert search_verdict.reason == "swarm_gate_replay"

    grep_verdict = check_swarm_gate(
        state, "run_command", _Act(kind="run_command", command="rg TODO")
    )
    assert grep_verdict.suppress is True

    read_verdict = check_swarm_gate(
        state, "read_file", _Act(kind="read_file", path="harness/pilot.py")
    )
    assert read_verdict.suppress is False

    cg_verdict = check_swarm_gate(
        state, "search_codegraph", _Act(kind="search_codegraph", query="TurnGuardState")
    )
    assert cg_verdict.suppress is False


def test_swarm_gate_pre_dispatch_message_forbids_inline_substitute():
    state = new_turn_guard_state("Give me an audit of this directory")
    verdict = check_swarm_gate(state, "list_dir", _Act(kind="list_dir", path="."))
    assert verdict.suppress is True
    assert "re-dispatch a narrowed swarm" in verdict.message
    assert "native exploration unlocks" not in verdict.message
    assert "list_dir/search_files/grep sweeps stay blocked" in verdict.message


def test_pilot_system_requires_redispatch_on_shallow_swarm():
    from harness.pilot import PILOT_SYSTEM

    assert "re-dispatch a narrowed run_swarm" in PILOT_SYSTEM
    assert "NEVER open a broad inline exploration campaign" in PILOT_SYSTEM
    assert "thin results mean sharpen and re-dispatch" in PILOT_SYSTEM
    assert "do NOT \"validate with native tools\"" in PILOT_SYSTEM
    # Old abuse-prone phrasing must not remain as the sole post-swarm guidance.
    assert "use native exploration only to validate specific findings." not in PILOT_SYSTEM


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
        def __init__(self):
            self.n = 0

        def complete(self, prompt, system=None):
            from pmharness.drivers.openai_compat import DriverResponse
            self.n += 1
            # One step with twin reads, then stop — TurnGuardState now persists
            # across model steps, so re-emitting the same twins would eventually
            # hard-suppress after LOOP_REPEAT_CAP (that is intentional).
            if self.n == 1:
                text = json.dumps({
                    "say": "reading twice",
                    "actions": [
                        {"kind": "read_file", "path": "dup.txt"},
                        {"kind": "read_file", "path": "dup.txt"},
                    ],
                })
            else:
                text = json.dumps({"say": "done", "actions": []})
            return DriverResponse(text=text, tokens_out=10, latency_ms=1.0)

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


@pytest.mark.parametrize(
    "command",
    [
        "echo 'handoff summary' | pbcopy",
        "printf '%s' \"$note\" | pbcopy",
        "pbpaste",
        "echo hi | clip",
        "Set-Clipboard -Value 'handoff'",
        "xclip -selection clipboard",
        "wl-copy",
    ],
)
def test_clipboard_handoff_is_not_exploration(command):
    assert is_local_handoff_command(command) is True
    assert is_exploration_command(command) is False
    assert is_native_exploration("run_command", _Act(command=command)) is False


def test_delegate_gate_allows_clipboard_after_threshold():
    state = new_turn_guard_state()
    for i in range(DELEGATE_THRESHOLD):
        record_action_execution(state, "read_file", _Act(kind="read_file", path=f"f{i}.py"))
    act = _Act(kind="run_command", command="echo 'handoff' | pbcopy")
    assert check_delegate_gate(state, "run_command", act).suppress is False
    assert check_pilot_guards(state, "run_command", act).suppress is False


def test_result_shows_kernel_failure_markers():
    luna_400 = (
        "HTTP 400: Function tools with reasoning_effort are not supported "
        "for gpt-5.6-luna in /v1/chat/completions. Use /v1/responses or "
        "set reasoning_effort to 'none'."
    )
    assert result_shows_kernel_failure(luna_400) is True
    assert result_shows_kernel_failure("preflight_blocked: puppetmaster unavailable") is True
    assert result_shows_kernel_failure("swarm produced no FINDING/RISK/DECISION artifacts") is False
    assert result_shows_kernel_failure("") is False


def test_kernel_recovery_lifts_swarm_and_delegate_gates():
    state = new_turn_guard_state("Give me an audit of this directory")
    for i in range(DELEGATE_THRESHOLD):
        record_action_execution(state, "read_file", _Act(kind="read_file", path=f"z{i}.py"))
    record_action_execution(state, "run_swarm", _Act(kind="run_swarm", goal="map auth"))
    assert state.swarm_dispatched is True
    assert state.delegation_seen is True

    list_act = _Act(kind="list_dir", path=".")
    rg_act = _Act(kind="run_command", command="rg reasoning_effort")
    assert is_swarm_gate_blocked_exploration(state, "list_dir", list_act) is True
    assert check_swarm_gate(state, "list_dir", list_act).suppress is True
    assert check_swarm_gate(state, "run_command", rg_act).suppress is True

    note_kernel_recovery_from_result(
        state,
        "run_swarm",
        "HTTP 400: Function tools with reasoning_effort are not supported",
    )
    assert state.kernel_recovery is True
    assert is_swarm_gate_blocked_exploration(state, "list_dir", list_act) is False
    assert check_swarm_gate(state, "list_dir", list_act).suppress is False
    assert check_swarm_gate(state, "run_command", rg_act).suppress is False
    assert check_delegate_gate(state, "search_files", _Act(query="adapter")).suppress is False


def test_kernel_recovery_still_loop_blocks_identical_swarm():
    state = new_turn_guard_state("Give me an audit of this directory")
    act = _Act(kind="run_swarm", goal="map auth")
    record_action_execution(state, "run_swarm", act)
    note_kernel_recovery(state)
    verdict = check_pilot_guards(state, "run_swarm", act)
    assert verdict.suppress is True
    assert verdict.reason == "loop"


@pytest.mark.parametrize(
    "command",
    [
        'curl -X POST http://127.0.0.1:50076/api/restart -H "X-Harness-Token: abc"',
        "Invoke-WebRequest -Uri http://localhost:8799/api/restart -Method POST",
        "python -c \"...\" # then POST /api/restart",
        "harness:restart",
        "restart-backend",
    ],
)
def test_mid_turn_backend_restart_suppressed(command, monkeypatch):
    monkeypatch.delenv("HARNESS_ALLOW_MID_TURN_RESTART", raising=False)
    assert mid_turn_restart_blocked() is True
    assert is_backend_restart_command(command) is True
    state = new_turn_guard_state("wire discord mcp")
    act = _Act(kind="run_command", command=command)
    verdict = check_backend_restart(state, "run_command", act)
    assert verdict.suppress is True
    assert verdict.reason == "mid_turn_restart"
    assert "manage_mcp" in verdict.message


def test_mid_turn_restart_kill_switch(monkeypatch):
    monkeypatch.setenv("HARNESS_ALLOW_MID_TURN_RESTART", "1")
    assert mid_turn_restart_blocked() is False
    state = new_turn_guard_state("wire discord mcp")
    act = _Act(kind="run_command", command="curl -X POST http://127.0.0.1/api/restart")
    assert check_backend_restart(state, "run_command", act).suppress is False


def test_ordinary_commands_not_restart(monkeypatch):
    monkeypatch.delenv("HARNESS_ALLOW_MID_TURN_RESTART", raising=False)
    assert is_backend_restart_command("docker restart discord-mcp") is False
    assert is_backend_restart_command("curl http://127.0.0.1:8085/mcp") is False


def test_implement_exhausted_soft_refuses_run_implement():
    state = new_turn_guard_state("fix the mockup styles")
    act = _Act(kind="run_implement", goal="polish styles.css")
    assert check_implement_exhausted(state, "run_implement", act).suppress is False

    state.last_implement_exhausted = True
    verdict = check_implement_exhausted(state, "run_implement", act)
    assert verdict.suppress is True
    assert verdict.reason == "implement_exhausted"
    assert "do not re-dispatch run_implement" in verdict.message
    assert "hash_edit/edit_file" in verdict.message


def test_plumbing_swarm_thrash_soft_refuses_identical_redispatch():
    from harness.pilot_guards import (
        check_plumbing_swarm_thrash,
        record_plumbing_degraded_swarm,
    )

    state = new_turn_guard_state("audit auth middleware")
    act = _Act(kind="run_swarm", goal="Audit auth middleware", model="")
    assert check_plumbing_swarm_thrash(state, "run_swarm", act).suppress is False

    record_plumbing_degraded_swarm(state, act.goal, act.model)
    verdict = check_plumbing_swarm_thrash(state, "run_swarm", act)
    assert verdict.suppress is True
    assert verdict.reason == "plumbing_swarm_thrash"
    assert "model pin" in verdict.message.lower()

    # Changing model pin is allowed.
    pinned = _Act(kind="run_swarm", goal=act.goal, model="cursor/grok-4-5")
    assert check_plumbing_swarm_thrash(state, "run_swarm", pinned).suppress is False

    # Changing goal is allowed.
    different = _Act(kind="run_swarm", goal="Audit billing paths only", model="")
    assert check_plumbing_swarm_thrash(state, "run_swarm", different).suppress is False

    # Wired into check_pilot_guards.
    assert check_pilot_guards(state, "run_swarm", act).reason == "plumbing_swarm_thrash"


def test_implement_exhausted_soft_refuses_swarm_dispatch_kinds():
    """After empty_managed_implement_exhausted, fan-out kinds must soft-refuse too."""
    state = new_turn_guard_state("fix the mockup styles")
    state.last_implement_exhausted = True
    for kind in ("run_implement", "run_parallel", "run_swarm"):
        act = _Act(kind=kind, goal="retry polish via fan-out")
        verdict = check_implement_exhausted(state, kind, act)
        assert verdict.suppress is True, kind
        assert verdict.reason == "implement_exhausted", kind
        assert "run_parallel" in verdict.message
        assert "run_swarm" in verdict.message
    # Non-dispatch kinds stay open so the pilot can inspect/edit live files.
    explore = check_implement_exhausted(
        state, "read_file", _Act(kind="read_file", path="styles.css"),
    )
    assert explore.suppress is False


def test_check_pilot_guards_blocks_exhausted_run_parallel():
    state = TurnGuardState(last_implement_exhausted=True)
    act = _Act(kind="run_parallel", goals=["polish styles.css", "touch app.js"])
    verdict = check_pilot_guards(state, "run_parallel", act)
    assert verdict.suppress is True
    assert verdict.reason == "implement_exhausted"


def test_note_implement_exhausted_from_provenance_sets_flag():
    state = new_turn_guard_state("fix styles")
    note_implement_exhausted_from_provenance(state, {})
    assert state.last_implement_exhausted is False
    note_implement_exhausted_from_provenance(
        state, {"empty_managed_implement_exhausted": True},
    )
    assert state.last_implement_exhausted is True


def test_check_pilot_guards_blocks_exhausted_run_implement():
    state = TurnGuardState(last_implement_exhausted=True)
    act = _Act(kind="run_implement", goal="retry the same polish")
    verdict = check_pilot_guards(state, "run_implement", act)
    assert verdict.suppress is True
    assert verdict.reason == "implement_exhausted"


def test_tiny_workspace_classifier_counts_source_ignores_vendor(tmp_path):
    (tmp_path / "app.js").write_text("console.log(1);\n", encoding="utf-8")
    (tmp_path / "index.html").write_text("<html></html>\n", encoding="utf-8")
    (tmp_path / "styles.css").write_text("body{}\n", encoding="utf-8")
    (tmp_path / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00")
    vendor = tmp_path / "node_modules" / "pkg"
    vendor.mkdir(parents=True)
    (vendor / "index.js").write_text("module.exports=1;\n" * 200, encoding="utf-8")
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("x\n" * 50, encoding="utf-8")
    build = tmp_path / "build"
    build.mkdir()
    (build / "out.js").write_text("x\n" * 100, encoding="utf-8")

    files, loc = workspace_source_stats(str(tmp_path))
    assert files == 3
    assert loc == 3
    assert is_tiny_workspace(str(tmp_path)) is True


def test_tiny_workspace_budget_tightens_default(tmp_path, monkeypatch):
    monkeypatch.delenv("HARNESS_PILOT_TOOL_BUDGET", raising=False)
    monkeypatch.delenv("HARNESS_TURN_BUDGET", raising=False)
    monkeypatch.delenv("HARNESS_TINY_WORKSPACE_TOOL_BUDGET", raising=False)
    (tmp_path / "a.js").write_text("a\n", encoding="utf-8")
    (tmp_path / "b.js").write_text("b\n", encoding="utf-8")

    assert turn_tool_budget_cap() == TURN_TOOL_BUDGET_DEFAULT
    assert turn_tool_budget_cap(repo_path=str(tmp_path)) == TINY_WORKSPACE_TOOL_BUDGET_DEFAULT

    state = new_turn_guard_state("polish the demo", repo_path=str(tmp_path))
    assert state.tiny_workspace is True
    assert state.iteration_budget is not None
    assert state.iteration_budget.cap == TINY_WORKSPACE_TOOL_BUDGET_DEFAULT


def test_tiny_workspace_budget_only_tightens_explicit_ceiling(tmp_path, monkeypatch):
    (tmp_path / "a.js").write_text("a\n", encoding="utf-8")
    monkeypatch.setenv("HARNESS_PILOT_TOOL_BUDGET", "8")
    monkeypatch.delenv("HARNESS_TINY_WORKSPACE_TOOL_BUDGET", raising=False)
    assert turn_tool_budget_cap(repo_path=str(tmp_path)) == 8
    assert turn_tool_budget_cap(repo_path=str(tmp_path / "missing")) == 8

    monkeypatch.setenv("HARNESS_PILOT_TOOL_BUDGET", "20")
    assert turn_tool_budget_cap(repo_path=str(tmp_path)) == TINY_WORKSPACE_TOOL_BUDGET_DEFAULT
    assert turn_tool_budget_cap(repo_path=str(tmp_path / "missing")) == 20

    monkeypatch.setenv("HARNESS_TINY_WORKSPACE_TOOL_BUDGET", "10")
    assert tiny_workspace_tool_budget() == 10
    assert turn_tool_budget_cap(repo_path=str(tmp_path)) == 10


def test_large_workspace_keeps_default_budget(tmp_path, monkeypatch):
    monkeypatch.delenv("HARNESS_PILOT_TOOL_BUDGET", raising=False)
    monkeypatch.delenv("HARNESS_TURN_BUDGET", raising=False)
    for i in range(20):
        (tmp_path / f"f{i}.py").write_text("x = 1\n" * 300, encoding="utf-8")
    assert is_tiny_workspace(str(tmp_path)) is False
    assert turn_tool_budget_cap(repo_path=str(tmp_path)) == TURN_TOOL_BUDGET_DEFAULT
    state = new_turn_guard_state("audit", repo_path=str(tmp_path))
    assert state.tiny_workspace is False
    assert state.iteration_budget is not None
    assert state.iteration_budget.cap == TURN_TOOL_BUDGET_DEFAULT


def test_implement_success_provenance_clamps_post_implement_allowance(monkeypatch):
    monkeypatch.delenv("HARNESS_POST_IMPLEMENT_TOOL_ALLOWANCE", raising=False)
    budget = IterationBudget(cap=25, used=10)
    state = TurnGuardState(iteration_budget=budget)
    assert job_result_shows_implement_success({
        "applied": True,
        "files": ["app.js"],
        "has_patch_art": True,
        "worker_provenance": {"worktree_diff_empty": False},
    }) is True

    note_implement_success_from_job_result(
        state,
        {
            "applied": True,
            "files": ["app.js"],
            "has_patch_art": True,
            "worker_provenance": {"worktree_diff_empty": False},
        },
        {"role": "implement"},
    )
    assert state.implement_success_seen is True
    assert budget.cap == 10 + POST_IMPLEMENT_TOOL_ALLOWANCE_DEFAULT
    assert budget.remaining == POST_IMPLEMENT_TOOL_ALLOWANCE_DEFAULT

    # Idempotent — second note must not raise or re-widen.
    note_implement_success_from_job_result(
        state,
        {"applied": True, "files": ["app.js"], "has_patch_art": True},
        {"role": "implement"},
    )
    assert budget.cap == 10 + POST_IMPLEMENT_TOOL_ALLOWANCE_DEFAULT


def test_implement_success_never_raises_existing_cap():
    budget = IterationBudget(cap=12, used=10)
    state = TurnGuardState(iteration_budget=budget)
    clamp_post_implement_iteration_budget(state)
    # used(10)+allowance(4)=14, but existing cap 12 wins.
    assert budget.cap == 12


def test_empty_managed_implement_exhaustion_is_not_success():
    state = TurnGuardState(iteration_budget=IterationBudget(cap=25, used=3))
    res = {
        "applied": False,
        "error": "empty managed implement exhausted",
        "has_patch_art": False,
        "worker_provenance": {"empty_managed_implement_exhausted": True},
    }
    assert job_result_shows_implement_success(res, {"role": "implement"}) is False
    note_implement_success_from_job_result(state, res, {"role": "implement"})
    assert state.implement_success_seen is False
    assert state.iteration_budget.cap == 25


def test_analysis_ok_is_not_implement_success():
    res = {
        "applied": False,
        "analysis_ok": True,
        "has_patch_art": False,
        "worker_provenance": {"worktree_diff_empty": True},
    }
    assert job_result_shows_implement_success(res, {"role": "analysis"}) is False


def test_post_implement_budget_exhaustion_uses_calm_message():
    budget = IterationBudget(cap=14, used=14)
    state = TurnGuardState(
        iteration_budget=budget,
        implement_success_seen=True,
    )
    verdict = check_iteration_budget(state, "read_file", _Act(path="app.js"))
    assert verdict.suppress is True
    assert verdict.reason == "budget"
    assert "worker patch already landed" in verdict.message
    assert "Report the outcome" in verdict.message


def test_chrome_file_smoke_detection():
    assert is_headless_chrome_file_smoke_command(
        "chromium --headless --dump-dom file:///tmp/freeze/index.html"
    ) is True
    assert is_headless_chrome_file_smoke_command(
        "google-chrome --headless --dump-dom ./index.html"
    ) is True
    assert is_headless_chrome_file_smoke_command(
        "chromium --headless --dump-dom https://example.com"
    ) is False
    assert is_headless_chrome_file_smoke_command("ls index.html") is False


def test_chrome_file_smoke_suppressed_on_tiny_or_post_implement():
    act = _Act(
        kind="run_command",
        command="chromium --headless --dump-dom file:///tmp/site/index.html",
    )
    tiny = TurnGuardState(tiny_workspace=True, user_message="polish the demo")
    verdict = check_chrome_file_smoke(tiny, "run_command", act)
    assert verdict.suppress is True
    assert verdict.reason == "chrome_file_smoke"
    assert "static" in verdict.message.lower() or "cheap" in verdict.message.lower()

    post = TurnGuardState(
        implement_success_seen=True, user_message="polish the demo",
    )
    assert check_chrome_file_smoke(post, "run_command", act).suppress is True

    large = TurnGuardState(user_message="polish the demo")
    assert check_chrome_file_smoke(large, "run_command", act).suppress is False


def test_chrome_file_smoke_not_suppressed_for_explicit_browser_qa():
    assert user_requests_browser_qa("please do a visual QA in the browser") is True
    state = TurnGuardState(
        tiny_workspace=True,
        implement_success_seen=True,
        user_message="run a browser QA / visual check on the demo",
    )
    act = _Act(
        kind="run_command",
        command="chromium --headless --dump-dom file:///tmp/site/index.html",
    )
    assert check_chrome_file_smoke(state, "run_command", act).suppress is False


def test_browser_star_tools_not_suppressed_by_chrome_smoke_guard():
    state = TurnGuardState(tiny_workspace=True, implement_success_seen=True)
    verdict = check_chrome_file_smoke(
        state, "browser_navigate", _Act(kind="browser_navigate"),
    )
    assert verdict.suppress is False


def test_post_implement_allowance_env_override(monkeypatch):
    monkeypatch.setenv("HARNESS_POST_IMPLEMENT_TOOL_ALLOWANCE", "2")
    assert post_implement_tool_allowance() == 2
    budget = IterationBudget(cap=25, used=5)
    state = TurnGuardState(iteration_budget=budget)
    clamp_post_implement_iteration_budget(state)
    assert budget.cap == 7


def test_tiny_foreground_vs_nested_implement_budget(tmp_path, monkeypatch):
    """Foreground tiny pilots tighten to 12; nested implement workers do not."""
    monkeypatch.delenv("HARNESS_PILOT_TOOL_BUDGET", raising=False)
    monkeypatch.delenv("HARNESS_TURN_BUDGET", raising=False)
    monkeypatch.delenv("HARNESS_TINY_WORKSPACE_TOOL_BUDGET", raising=False)
    (tmp_path / "app.js").write_text("console.log(1);\n", encoding="utf-8")

    foreground = new_turn_guard_state("polish the demo", repo_path=str(tmp_path))
    assert foreground.tiny_workspace is True
    assert foreground.nested_implement is False
    assert foreground.iteration_budget is not None
    assert foreground.iteration_budget.cap == TINY_WORKSPACE_TOOL_BUDGET_DEFAULT

    nested = new_turn_guard_state(
        "IMPLEMENT TASK: polish app.js",
        repo_path=str(tmp_path),
        nested_implement=True,
    )
    assert nested.tiny_workspace is True
    assert nested.nested_implement is True
    assert nested.iteration_budget is not None
    assert nested.iteration_budget.cap == TURN_TOOL_BUDGET_DEFAULT
    assert turn_tool_budget_cap(str(tmp_path), nested_implement=True) == TURN_TOOL_BUDGET_DEFAULT


def test_nested_implement_edit_first_blocks_exploration_before_write():
    state = new_turn_guard_state("IMPLEMENT TASK: fix app.js", nested_implement=True)
    assert state.nested_implement is True

    # Broad exploration is blocked before any edit.
    for kind in ("list_dir", "search_files", "run_ipython", "search_codegraph"):
        verdict = check_edit_first(state, kind, _Act(kind=kind, path="."))
        assert verdict.suppress is True, kind
        assert verdict.reason == "edit_first"

    # Target reads are allowed up to the edit-first allowance.
    for i in range(EDIT_FIRST_READ_ALLOWANCE_DEFAULT):
        act = _Act(kind="read_file", path=f"app{i}.js")
        assert check_edit_first(state, "read_file", act).suppress is False
        record_action_execution(state, "read_file", act)

    blocked = check_edit_first(
        state, "read_file", _Act(kind="read_file", path="extra.js"),
    )
    assert blocked.suppress is True
    assert blocked.reason == "edit_first"

    # A write clears the gate; further reads are allowed by edit-first.
    record_action_execution(
        state, "edit_file", _Act(kind="edit_file", path="app.js"),
    )
    assert state.edit_seen is True
    assert check_edit_first(
        state, "read_file", _Act(kind="read_file", path="app.js"),
    ).suppress is False


def test_nested_implement_edit_first_wired_into_pilot_guards():
    state = TurnGuardState(nested_implement=True)
    verdict = check_pilot_guards(
        state, "list_dir", _Act(kind="list_dir", path="."),
    )
    assert verdict.suppress is True
    assert verdict.reason == "edit_first"


def test_successful_implement_stops_redundant_parent_validation(monkeypatch):
    """Post-implement clamp + chrome smoke stay honest after a real patch."""
    monkeypatch.delenv("HARNESS_POST_IMPLEMENT_TOOL_ALLOWANCE", raising=False)
    budget = IterationBudget(cap=25, used=8)
    state = TurnGuardState(iteration_budget=budget, tiny_workspace=True)
    note_implement_success_from_job_result(
        state,
        {
            "applied": True,
            "files": ["app.js"],
            "has_patch_art": True,
            "worker_provenance": {"worktree_diff_empty": False},
        },
        {"role": "implement"},
    )
    assert state.implement_success_seen is True
    assert budget.remaining == POST_IMPLEMENT_TOOL_ALLOWANCE_DEFAULT
    chrome = check_chrome_file_smoke(
        state,
        "run_command",
        _Act(command="chromium --headless --dump-dom file:///tmp/index.html"),
    )
    assert chrome.suppress is True
    assert chrome.reason == "chrome_file_smoke"
