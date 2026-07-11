"""Dispatch dedupe + objective fingerprint (twin implement / platform lock)."""
from __future__ import annotations

from types import SimpleNamespace

from harness.pilot_guards import (
    TurnGuardState,
    check_loop_guard,
    dedupe_dispatch_actions,
    normalize_objective_key,
    record_action_execution,
)


def test_normalize_objective_collapses_path_spellings():
    a = normalize_objective_key(r"Rewrite backend at C:\Ashita\addons\kotoba")
    b = normalize_objective_key("Rewrite backend at C:/Ashita/addons/kotoba.")
    assert a == b


def test_dedupe_dispatch_actions_keeps_first_implement():
    acts = [
        SimpleNamespace(kind="run_implement", goal="Fix foo in a.py", roles=[], repo=""),
        SimpleNamespace(kind="read_file", goal="", path="a.py"),
        SimpleNamespace(kind="run_implement", goal="fix  FOO in a.py", roles=[], repo=""),
    ]
    out = dedupe_dispatch_actions(acts)
    assert len(out) == 2
    assert out[0].kind == "run_implement"
    assert out[1].kind == "read_file"


def test_loop_guard_blocks_second_identical_implement():
    state = TurnGuardState()
    act = SimpleNamespace(
        kind="run_implement",
        goal="Complete rewrite of translator.py",
        roles=[],
        repo="",
        arguments={},
    )
    record_action_execution(state, "run_implement", act)
    verdict = check_loop_guard(state, "run_implement", act)
    assert verdict.suppress is True
    assert verdict.reason in ("loop", "loop_replay")
