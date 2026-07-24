"""Characterization tests for send_loop_dispatch peel from _send_locked_inner.

Locks the extracted swarm / implement / parallel / route_task / memory helper
contracts and asserts those branches no longer live inline in the turn kernel.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from harness.pilot import PilotAction
from harness.send_loop import SendLoopMixin
from harness.send_loop_dispatch import (
    DISPATCH_ACTION_KINDS,
    dispatch_implement_action,
    dispatch_memory_action,
    dispatch_parallel_action,
    dispatch_route_task_action,
    dispatch_swarm_action,
    is_untracked_pm_start_tool,
)

DISPATCH_HELPERS = (
    "dispatch_swarm_action",
    "dispatch_implement_action",
    "dispatch_parallel_action",
    "dispatch_route_task_action",
    "dispatch_memory_action",
)


def _inner_source() -> str:
    src = Path(inspect.getsourcefile(SendLoopMixin)).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "SendLoopMixin":
            for item in node.body:
                if (
                    isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and item.name == "_send_locked_inner"
                ):
                    return ast.get_source_segment(src, item) or ""
    raise AssertionError("_send_locked_inner not found")


def test_dispatch_helpers_are_module_level_callables():
    import harness.send_loop_dispatch as dispatch

    for name in DISPATCH_HELPERS:
        fn = getattr(dispatch, name)
        assert callable(fn)
        assert fn.__module__ == "harness.send_loop_dispatch"


def test_dispatch_action_kinds_covers_delegate_surface():
    assert DISPATCH_ACTION_KINDS == frozenset(
        {"run_swarm", "run_implement", "run_parallel", "route_task", "memory"}
    )
    # Read-only / local stay in send_loop_phases.
    assert "read_file" not in DISPATCH_ACTION_KINDS
    assert "write_file" not in DISPATCH_ACTION_KINDS
    assert "run_command" not in DISPATCH_ACTION_KINDS


def test_mixin_still_owns_public_orchestration_surface():
    for name in ("send", "_send_locked", "_send_locked_inner"):
        attr = getattr(SendLoopMixin, name)
        assert attr.__qualname__ == f"SendLoopMixin.{name}"


def test_mixin_calls_dispatch_helpers():
    # Kernel fans out via execute_turn_actions; dispatch helpers are invoked there.
    src = Path(inspect.getsourcefile(SendLoopMixin)).read_text(encoding="utf-8")
    assert "execute_turn_actions(" in src
    actions_src = Path("harness/send_loop_actions.py").read_text(encoding="utf-8")
    for name in DISPATCH_HELPERS:
        assert f"{name}(" in actions_src, name
    assert "DISPATCH_ACTION_KINDS" in actions_src


def test_send_locked_inner_no_longer_inlines_dispatch_branches():
    segment = _inner_source()
    assert "---- swarm branch" not in segment
    assert "---- run_implement branch" not in segment
    assert "---- run_parallel branch" not in segment
    assert "---- route_task branch" not in segment
    assert "---- memory branch" not in segment
    assert "execute_turn_actions(" in segment
    actions_src = Path("harness/send_loop_actions.py").read_text(encoding="utf-8")
    assert "dispatch_swarm_action(" in actions_src
    assert "dispatch_implement_action(" in actions_src
    assert "dispatch_parallel_action(" in actions_src
    assert "dispatch_route_task_action(" in actions_src
    assert "dispatch_memory_action(" in actions_src
    assert "DISPATCH_ACTION_KINDS" in actions_src


def test_send_locked_inner_no_longer_nests_dispatch_helpers():
    src = Path(inspect.getsourcefile(SendLoopMixin)).read_text(encoding="utf-8")
    tree = ast.parse(src)
    nested_names: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != "SendLoopMixin":
            continue
        for item in node.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if item.name != "_send_locked_inner":
                continue
            for child in ast.walk(item):
                if (
                    isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and child is not item
                ):
                    nested_names.add(child.name)
    assert not (nested_names & set(DISPATCH_HELPERS)), nested_names & set(DISPATCH_HELPERS)
    assert "execute_turn_actions" not in nested_names


def test_dispatch_route_task_requires_cli():
    act = PilotAction(kind="route_task", instruction="pick a model", arguments={})
    session = SimpleNamespace(
        _append_action_result=MagicMock(),
    )
    import harness.send_loop_dispatch as dispatch

    original = dispatch._puppetmaster_available
    dispatch._puppetmaster_available = lambda: False
    try:
        events = list(dispatch_route_task_action(session, act, "a1", True))
    finally:
        dispatch._puppetmaster_available = original
    assert events[0].kind == "action_result"
    assert "puppetmaster CLI not available" in events[0].data.get("error", "")
    session._append_action_result.assert_called_once()


def test_dispatch_memory_add_queues_in_interactive():
    act = PilotAction(
        kind="memory",
        memory_action="add",
        memory_content="remember the bake flag",
        memory_category="prefs",
    )
    session = SimpleNamespace(
        _auto_mode=False,
        _turn_memory_queue=[],
        _append_action_result=MagicMock(),
    )
    events = list(dispatch_memory_action(session, act, "a2", True))
    assert events[0].kind == "action_result"
    assert events[0].data["types"] == ["memory"]
    assert len(session._turn_memory_queue) == 1
    assert session._turn_memory_queue[0]["text"] == "remember the bake flag"
    session._append_action_result.assert_called_once()


def test_dispatch_memory_add_refuses_in_autopilot():
    act = PilotAction(
        kind="memory",
        memory_action="add",
        memory_content="should not persist",
        memory_category="prefs",
    )
    session = SimpleNamespace(
        _auto_mode=True,
        _turn_memory_queue=[],
        _append_action_result=MagicMock(),
    )
    events = list(dispatch_memory_action(session, act, "a3", False))
    assert events[0].data["types"] == ["memory"]
    assert session._turn_memory_queue == []
    appended = session._append_action_result.call_args[0][2]
    assert "Autopilot" in appended


def test_dispatch_implement_requires_workspace():
    act = PilotAction(kind="run_implement", goal="add a test")
    session = SimpleNamespace(
        config=SimpleNamespace(repo=""),
        _append_action_result=MagicMock(),
        _validate_target_repo=MagicMock(),
    )
    gen = dispatch_implement_action(
        session,
        act,
        "a4",
        True,
        turn_actions=[act],
        action_idx=0,
        action_seq=1,
        step=0,
        swarms=0,
    )
    events = list(gen)
    assert any(e.kind == "action_result" and "No workspace" in e.data.get("error", "") for e in events)
    session._append_action_result.assert_called_once()


def test_dispatch_parallel_requires_goals():
    act = PilotAction(kind="run_parallel", goals=[])
    session = SimpleNamespace(
        config=SimpleNamespace(repo="/repo"),
        _append_action_result=MagicMock(),
        _validate_target_repo=MagicMock(),
    )
    events = list(
        dispatch_parallel_action(
            session,
            act,
            "a5",
            True,
            turn_actions=[act],
            action_idx=0,
            action_seq=1,
            step=0,
            swarms=0,
        )
    )
    assert "non-empty goals" in events[0].data.get("error", "")
    session._append_action_result.assert_called_once()


def test_dispatch_swarm_registers_and_surfaces_error(monkeypatch):
    act = PilotAction(kind="run_swarm", goal="audit the peel", roles=["explore"])
    session = SimpleNamespace(
        config=SimpleNamespace(repo="/repo"),
        _session_job_ids=[],
        _register_local_job=MagicMock(),
        _finish_local_job=MagicMock(),
        _append_action_result=MagicMock(),
        _display_transcript=[],
    )
    import harness.send_loop_dispatch as dispatch

    monkeypatch.setattr(dispatch, "_non_git_workspace_error", lambda *_a, **_k: None)

    def boom(session, intent, q):
        q.put(("error", RuntimeError("stream failed")))

    original = dispatch.stream_swarm
    dispatch.stream_swarm = boom
    try:
        counters = {"swarms": 0, "demo_swarms": 0}
        events = list(
            dispatch_swarm_action(
                session,
                act,
                "a6",
                True,
                counters=counters,
                turn_findings=[],
            )
        )
    finally:
        dispatch.stream_swarm = original
    kinds = [e.kind for e in events]
    assert "swarm_pending" in kinds
    assert "action_result" in kinds
    assert counters["swarms"] == 0
    session._finish_local_job.assert_called()
    session._append_action_result.assert_called_once()


def test_dispatch_swarm_registers_resolved_git_child(tmp_path):
    """run_swarm local job cwd must be the git child, not the non-git home parent."""
    import os
    import shutil
    import subprocess

    import pytest

    from harness.repo_resolve import clear_effective_repo_cache, resolve_effective_repo

    if shutil.which("git") is None:
        pytest.skip("git not available")

    clear_effective_repo_cache()
    home = tmp_path / "home" / ".marionette"
    home.mkdir(parents=True)
    child = home / "marionette"
    child.mkdir()
    subprocess.run(
        ["git", "init"],
        cwd=str(child),
        check=True,
        capture_output=True,
        text=True,
    )
    act = PilotAction(kind="run_swarm", goal="audit the peel", roles=["explore"])
    session = SimpleNamespace(
        config=SimpleNamespace(repo=str(home)),
        _session_job_ids=[],
        _register_local_job=MagicMock(),
        _finish_local_job=MagicMock(),
        _append_action_result=MagicMock(),
        _display_transcript=[],
    )
    import harness.send_loop_dispatch as dispatch

    def boom(session, intent, q):
        q.put(("error", RuntimeError("stream failed")))

    original = dispatch.stream_swarm
    dispatch.stream_swarm = boom
    try:
        list(
            dispatch_swarm_action(
                session,
                act,
                "a6b",
                True,
                counters={"swarms": 0, "demo_swarms": 0},
                turn_findings=[],
            )
        )
    finally:
        dispatch.stream_swarm = original
        clear_effective_repo_cache()

    expected = resolve_effective_repo(str(home))
    assert session._register_local_job.called
    cwd_kw = session._register_local_job.call_args.kwargs.get("cwd")
    if cwd_kw is None and session._register_local_job.call_args.args:
        # positional fallback if signature changes
        cwd_kw = None
    assert cwd_kw is not None
    assert os.path.normcase(os.path.normpath(cwd_kw)) == os.path.normcase(
        os.path.normpath(expected)
    )
    assert os.path.normcase(os.path.normpath(str(home))) != os.path.normcase(
        os.path.normpath(expected)
    )


def test_dispatch_parallel_uses_resolved_git_child_cwd(tmp_path, monkeypatch):
    """run_parallel must pin worker cwd to the git child under Marionette Home."""
    import os
    import shutil
    import subprocess

    import pytest

    from harness.repo_resolve import clear_effective_repo_cache, resolve_effective_repo

    if shutil.which("git") is None:
        pytest.skip("git not available")

    clear_effective_repo_cache()
    home = tmp_path / "home" / ".marionette"
    home.mkdir(parents=True)
    child = home / "marionette"
    child.mkdir()
    subprocess.run(
        ["git", "init"],
        cwd=str(child),
        check=True,
        capture_output=True,
        text=True,
    )
    expected = resolve_effective_repo(str(home))

    monkeypatch.setattr(
        "harness.implement_guards.check_implement_workspace",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "harness.implement_guards.check_oversized_single_file_rewrite",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "harness.edit_engines.select_edit_engine",
        lambda *_a, **_k: "native",
    )
    monkeypatch.setattr(
        "harness.conversation._prewarm_worker_imports",
        lambda: None,
    )

    act = PilotAction(
        kind="run_parallel",
        goals=["review auth"],
        mode="analysis",
    )
    registered: list = []

    def _register(job_id, goal, role="implement", cwd="", engine="", model=""):
        registered.append({"job_id": job_id, "cwd": cwd, "goal": goal})

    session = SimpleNamespace(
        config=SimpleNamespace(repo=str(home), driver="test"),
        _session_job_ids=[],
        _append_action_result=MagicMock(),
        _validate_target_repo=MagicMock(),
        _resolve_requested_implement_adapter=MagicMock(return_value=("", "")),
        _external_adapter_available=MagicMock(return_value=False),
        _claim_objective=MagicMock(return_value=True),
        _release_objective=MagicMock(),
        _register_local_job=_register,
        _submit_swarm=MagicMock(return_value=True),
        _swarm_inflight=MagicMock(return_value=0),
        _answer_remaining_tool_calls=MagicMock(return_value=iter(())),
        _job_dispatch_label_args=MagicMock(return_value=[]),
    )
    import harness.send_loop_dispatch as dispatch

    monkeypatch.setattr(dispatch, "_puppetmaster_available", lambda: False)

    events = list(
        dispatch_parallel_action(
            session,
            act,
            "a7",
            True,
            turn_actions=[act],
            action_idx=0,
            action_seq=1,
            step=0,
            swarms=0,
        )
    )
    clear_effective_repo_cache()

    assert registered, "expected a local parallel job registration"
    got = registered[0]["cwd"]
    assert os.path.normcase(os.path.normpath(got)) == os.path.normcase(
        os.path.normpath(expected)
    )
    assert os.path.normcase(os.path.normpath(str(home))) != os.path.normcase(
        os.path.normpath(expected)
    )
    start = next(e for e in events if e.kind == "action_start")
    assert os.path.normcase(os.path.normpath(start.data["cwd"])) == os.path.normcase(
        os.path.normpath(expected)
    )


# ---------------------------------------------------------------------------
# Swarm quality gate: thin/generic findings must not read as a green success.

def test_substantive_artifact_gate():
    from harness.send_loop_dispatch import _is_substantive_artifact

    # Generic one-liner stubs: not substantive.
    assert not _is_substantive_artifact({"type": "finding", "headline": "Audit complete."})
    assert not _is_substantive_artifact({"type": "finding", "headline": "No issues found in the repository."})

    # Short but file-backed: substantive.
    assert _is_substantive_artifact(
        {"type": "finding", "headline": "Unbounded cache growth in harness/server.py line 210"})
    assert _is_substantive_artifact(
        {"type": "risk", "headline": "webapp/src/lib/api.ts:88 swallows fetch errors silently"})

    # Long prose without a path: substantive (real analysis parked in body).
    assert _is_substantive_artifact({
        "type": "finding",
        "headline": "clip",
        "body": "x" * 250,
    })

    # Malformed payloads fail closed (never paint green on parse errors).
    assert not _is_substantive_artifact({"type": "finding", "body": None, "headline": None})
    # Outer-gate exceptions also fail closed (non-mapping payloads).
    assert not _is_substantive_artifact(None)  # type: ignore[arg-type]


def test_dispatch_swarm_no_pending_when_register_fails(monkeypatch):
    """swarm_pending must not fire without a successful tracker row."""
    act = PilotAction(kind="run_swarm", goal="audit peel", roles=["explore"])
    session = SimpleNamespace(
        config=SimpleNamespace(repo="/repo"),
        _session_job_ids=[],
        _register_local_job=MagicMock(side_effect=RuntimeError("store down")),
        _finish_local_job=MagicMock(),
        _append_action_result=MagicMock(),
        _display_transcript=[],
    )
    import harness.send_loop_dispatch as dispatch

    monkeypatch.setattr(dispatch, "_non_git_workspace_error", lambda *_a, **_k: None)
    events = list(
        dispatch_swarm_action(
            session,
            act,
            "a-reg",
            True,
            counters={"swarms": 0, "demo_swarms": 0},
            turn_findings=[],
        )
    )
    assert not any(e.kind == "swarm_pending" for e in events)
    results = [e for e in events if e.kind == "action_result"]
    assert results
    assert "tracker register failed" in (results[0].data.get("error") or "")
    assert session._session_job_ids == []
    session._append_action_result.assert_called_once()


def test_is_untracked_pm_start_tool():
    assert is_untracked_pm_start_tool("puppetmaster_start_cursor_swarm")
    assert is_untracked_pm_start_tool("user-puppetmaster/start_implement")
    assert is_untracked_pm_start_tool("start_swarm")
    assert not is_untracked_pm_start_tool("puppetmaster_codegraph_search")
    assert not is_untracked_pm_start_tool("query_wiki")
    assert not is_untracked_pm_start_tool("")
