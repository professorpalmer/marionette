"""Pilot arg coercion + dispatch exception action_result coverage.

Locks two coupled fixes:
1) Dispatch paths that emit action_start then raise must yield an
   action_result carrying the REAL exception (never leave the opaque
   turn-end "missing action_result" settle as the only signal).
2) Common model malformations of goals/goal/adapter/mode are coerced
   before validation so the first tool call succeeds.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from harness.conversation import ConvEvent
from harness.pilot import (
    PilotError,
    PilotAction,
    _coerce_actions,
    _tool_name_to_action,
    build_tools_schema,
    from_wire,
)
from harness.send_loop_actions import execute_turn_actions
from harness.send_loop_dispatch import dispatch_parallel_action


# ---------------------------------------------------------------------------
# FIX 1: exception after action_start -> real action_result
# ---------------------------------------------------------------------------


def test_execute_turn_actions_settles_dispatch_exception_with_real_error():
    """A dispatch generator that raises after action_start must not leak."""
    act = PilotAction(kind="run_swarm", goal="audit peel", roles=["explore"])
    turn = SimpleNamespace(actions=[act])

    def boom(*_a, **_k):
        yield ConvEvent("action_start", {
            "id": "a1", "kind": "run_swarm", "goal": "audit peel",
        })
        raise RuntimeError("simulated dispatch boom")
        # make this a generator even if the raise is moved
        yield  # pragma: no cover

    session = SimpleNamespace(
        _cancel=SimpleNamespace(is_set=lambda: False),
        _steer_pending=False,
        _history=[],
        _turn_guard_state=None,
        _pending_advisor_warnings=[],
        config=SimpleNamespace(repo="/repo", swarm_adapter="agentic", no_delegation=False),
        _check_and_inject_steer=lambda: iter(()),
        _sanitize_tool_pairs=MagicMock(),
        _append_action_result=MagicMock(),
        _turn_economy=SimpleNamespace(enforce_tool_batch=lambda msgs: None),
        pilot=None,
    )

    import harness.send_loop_actions as actions_mod

    original = actions_mod.dispatch_swarm_action
    actions_mod.dispatch_swarm_action = boom
    try:
        counters = {"action_seq": 0, "swarms": 0, "demo_swarms": 0}
        gen = execute_turn_actions(
            session,
            turn=turn,
            user_message="audit",
            is_native=True,
            plan=False,
            counters=counters,
            step=0,
            turn_findings=[],
        )
        events = []
        try:
            while True:
                events.append(next(gen))
        except StopIteration as stop:
            disposition = stop.value
    finally:
        actions_mod.dispatch_swarm_action = original

    results = [e for e in events if e.kind == "action_result"]
    assert results, "expected an action_result after dispatch exception"
    assert results[-1].data.get("id") == "a1"
    assert "simulated dispatch boom" in (results[-1].data.get("error") or "")
    assert "missing action_result" not in (results[-1].data.get("error") or "")
    session._append_action_result.assert_called()
    assert disposition[0] is None


def test_dispatch_parallel_agentic_exception_after_start_yields_real_error(tmp_path, monkeypatch):
    """Agentic run_parallel path: raise after action_start -> real error result."""
    act = PilotAction(
        kind="run_parallel",
        goals=["Goal A", "Goal B"],
        adapter="",
        mode="implement",
    )
    session = SimpleNamespace(
        config=SimpleNamespace(repo=str(tmp_path), driver="stub"),
        _append_action_result=MagicMock(),
        _validate_target_repo=MagicMock(return_value=(str(tmp_path), None)),
        _resolve_requested_implement_adapter=MagicMock(return_value=("", "")),
        _external_adapter_available=MagicMock(return_value=False),
        _claim_objective=MagicMock(side_effect=RuntimeError("claim exploded")),
        _answer_remaining_tool_calls=MagicMock(return_value=iter(())),
        _session_job_ids=[],
    )

    import harness.send_loop_dispatch as dispatch

    monkeypatch.setattr(dispatch, "_puppetmaster_available", lambda: False)
    monkeypatch.setattr(
        "harness.edit_engines.select_edit_engine",
        lambda *_a, **_k: "agentic",
    )
    monkeypatch.setattr(
        "harness.implement_guards.check_implement_workspace",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "harness.implement_guards.check_oversized_single_file_rewrite",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "harness.repo_resolve.resolve_effective_repo",
        lambda p: p,
    )

    events = list(
        dispatch_parallel_action(
            session,
            act,
            "par1",
            True,
            turn_actions=[act],
            action_idx=0,
            action_seq=1,
            step=0,
            swarms=0,
        )
    )
    kinds = [e.kind for e in events]
    assert "action_start" in kinds
    results = [e for e in events if e.kind == "action_result"]
    assert results
    assert results[-1].data.get("id") == "par1"
    assert "claim exploded" in (results[-1].data.get("error") or "")
    assert "missing action_result" not in (results[-1].data.get("error") or "")
    session._append_action_result.assert_called()


# ---------------------------------------------------------------------------
# FIX 2: tolerant goals / goal / adapter / mode coercion
# ---------------------------------------------------------------------------


def test_coerce_goals_as_plain_string():
    act = from_wire("run_parallel", {"goals": "Fix the flaky login test"})
    assert act.goals == ["Fix the flaky login test"]


def test_coerce_goals_as_json_encoded_array_string():
    act = from_wire(
        "run_parallel",
        {"goals": '["Add unit tests for auth.py", "Document the API routes"]'},
    )
    assert act.goals == [
        "Add unit tests for auth.py",
        "Document the API routes",
    ]


def test_coerce_goal_singular_to_goals_for_run_parallel():
    act = from_wire("run_parallel", {"goal": "Ship the release notes"})
    assert act.goals == ["Ship the release notes"]


def test_coerce_goals_array_to_goal_for_run_implement():
    act = from_wire(
        "run_implement",
        {"goals": ["Patch the retry loop", "also ignored second"]},
    )
    assert act.goal == "Patch the retry loop"
    assert act.goals == []


def test_coerce_goals_array_to_goal_for_run_swarm():
    act = _tool_name_to_action(
        "run_swarm",
        {"goals": ["Map the auth flow"]},
        tool_call_id="tc_swarm",
    )
    assert act.goal == "Map the auth flow"


def test_coerce_drops_whitespace_only_goals_and_normalizes_case():
    act = from_wire(
        "run_parallel",
        {
            "goals": ["Keep this", "  ", "", "Also keep"],
            "adapter": "Agentic",
            "mode": "Analysis",
        },
    )
    assert act.goals == ["Keep this", "Also keep"]
    assert act.adapter == "agentic"
    assert act.mode == "analysis"


def test_empty_goals_still_errors_with_existing_message():
    with pytest.raises(PilotError) as ei:
        from_wire("run_parallel", {"goals": []})
    assert "requires a list of 'goals'" in str(ei.value)

    with pytest.raises(PilotError) as ei2:
        _coerce_actions([{"kind": "run_parallel", "goals": ["  ", ""]}])
    assert "requires a list of 'goals'" in str(ei2.value)

    with pytest.raises(PilotError) as ei3:
        from_wire("run_parallel", {})
    assert "requires a list of 'goals'" in str(ei3.value)


def test_run_parallel_schema_states_goals_array_contract():
    schema = build_tools_schema(no_delegation=False)
    parallel = next(
        t for t in schema
        if (t.get("function") or {}).get("name") == "run_parallel"
    )
    fn = parallel["function"]
    desc = fn["description"]
    goals_prop = fn["parameters"]["properties"]["goals"]
    assert "JSON array" in desc
    assert "2-8" in desc
    assert "example" in desc.lower() or "[" in desc
    assert "2-8" in goals_prop["description"]
    assert goals_prop.get("minItems") == 2
    assert goals_prop.get("maxItems") == 8

    swarm = next(
        t for t in schema
        if (t.get("function") or {}).get("name") == "run_swarm"
    )
    assert "run_parallel" in swarm["function"]["description"]
