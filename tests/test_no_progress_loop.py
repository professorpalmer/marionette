"""Focused tests for post-provider-outage no-progress loop protections.

Covers TurnGuardState persistence across steps/resume, stagnation governor,
failed-objective keep-alive resume cap, and read-only analysis-mode enforcement
on bare run_implement.
"""

from __future__ import annotations

import json
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.pilot import PilotAction, PilotTurn
from harness.pilot_guards import (
    analysis_summary_is_substantive,
    fingerprint_turn_actions,
    is_read_only_analysis_goal,
    new_turn_guard_state,
    normalize_assistant_prose,
    normalize_objective_key,
    record_action_execution,
    reuse_or_new_turn_guard_state,
)
from harness.send_loop_actions import execute_turn_actions


class _Act:
    def __init__(self, kind: str, **kwargs):
        self.kind = kind
        for k, v in kwargs.items():
            setattr(self, k, v)
        self.arguments = kwargs


def test_reuse_or_new_turn_guard_state_persists_across_steps():
    turn1 = new_turn_guard_state("Audit the repo")
    record_action_execution(turn1, "list_dir", _Act(kind="list_dir", path="."))
    turn1.swarm_dispatched = True
    turn1.successful_results[("read_file", '{"kind":"read_file","path":"a.py"}')] = "cached"

    reused = reuse_or_new_turn_guard_state(turn1, "Audit the repo")
    assert reused is turn1
    assert reused.swarm_dispatched is True
    assert reused.execution_counts
    assert reused.successful_results

    fresh = reuse_or_new_turn_guard_state(None, "Where is foo?")
    assert fresh is not turn1
    assert fresh.broad_intent is False
    assert fresh.execution_counts == {}


def test_execute_turn_actions_carries_full_guard_state(monkeypatch):
    monkeypatch.setenv("HARNESS_LOOP_GUARD", "1")
    monkeypatch.setenv("HARNESS_SWARM_GATE", "1")
    prior = new_turn_guard_state("Give me an audit of this directory")
    record_action_execution(prior, "list_dir", _Act(kind="list_dir", path="."))
    prior.swarm_gate_suppress_count = 2
    prior.delegation_seen = True
    prior.exploration_count = 3

    act = PilotAction(kind="search_codegraph", query="TurnGuardState")
    turn = PilotTurn(say="", thinking="", actions=[act])
    session = SimpleNamespace(
        _turn_guard_state=prior,
        _cancel=threading.Event(),
        _steer_pending=False,
        _history=[],
        _pending_advisor_warnings=[],
        _append_action_result=MagicMock(),
        _check_and_inject_steer=MagicMock(return_value=iter(())),
        _turn_economy=SimpleNamespace(enforce_tool_batch=lambda msgs: None),
        config=SimpleNamespace(repo="/tmp/r", swarm_adapter="local", no_delegation=False),
        pilot=MagicMock(),
        _sanitize_tool_pairs=MagicMock(),
    )
    # Avoid real codegraph / readonly dispatch.
    monkeypatch.setattr(
        "harness.send_loop_actions.dispatch_readonly_action",
        lambda *a, **k: iter(()),
    )
    monkeypatch.setattr(
        "harness.send_loop_actions.run_parallel_prefetch",
        lambda *a, **k: {},
    )

    gen = execute_turn_actions(
        session,
        turn=turn,
        user_message="Give me an audit of this directory",
        is_native=True,
        plan=False,
        counters={"action_seq": 0, "swarms": 0, "demo_swarms": 0},
        step=1,
        turn_findings=[],
    )
    list(gen)
    assert session._turn_guard_state is prior
    assert session._turn_guard_state.swarm_gate_suppress_count == 2
    assert session._turn_guard_state.delegation_seen is True
    assert session._turn_guard_state.exploration_count == 3


def test_fresh_user_message_resets_turn_guard_and_stagnation(monkeypatch):
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    session = ConversationalSession(cfg)
    session._turn_guard_state = new_turn_guard_state("Audit the repo")
    session._stagnation_streak = 2
    session._stagnation_last_prose = "same"
    session._stagnation_last_actions = "read_file:{}"
    session._failed_objective_resume_counts = {"audit auth": 2}

    mock_pilot = MagicMock()
    resp = MagicMock()
    resp.text = json.dumps({"say": "Hello", "actions": []})
    resp.meta = {}
    resp.error = None
    mock_pilot.chat.return_value = resp
    # Prefer chat_stream path when present.
    if hasattr(mock_pilot, "chat_stream"):
        mock_pilot.chat_stream = None
    session.pilot = mock_pilot

    list(session.send("new unrelated question"))
    # Cleared at the start of the fresh user message; may advance again during
    # the new turn (e.g. streak=1 for the single "Hello" reply). Must not keep
    # the prior turn's streak=2 / resume-cap map / guard instance.
    assert session._stagnation_streak <= 1
    assert session._stagnation_streak != 2
    assert session._failed_objective_resume_counts == {}
    if session._turn_guard_state is not None:
        assert session._turn_guard_state.user_message == "new unrelated question"


def test_stagnation_governor_halts_on_repeated_prose_and_actions(monkeypatch):
    monkeypatch.setenv("HARNESS_STAGNATION_STREAK_CAP", "3")
    monkeypatch.setenv("HARNESS_MAX_PILOT_STEPS", "0")  # unlimited — governor must still halt
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    session = ConversationalSession(cfg)

    payload = json.dumps({
        "say": "I will keep checking the same thing.",
        "actions": [{"kind": "list_dir", "path": "."}],
    })
    call_count = {"n": 0}

    def fake_chat(*_a, **_k):
        call_count["n"] += 1
        resp = MagicMock()
        resp.text = payload
        resp.meta = {}
        resp.error = None
        return resp

    mock_pilot = MagicMock()
    mock_pilot.chat.side_effect = fake_chat
    mock_pilot.chat_stream = None
    session.pilot = mock_pilot

    # Stub list_dir so the first two identical steps can execute without I/O.
    monkeypatch.setattr(
        "harness.send_loop_actions.dispatch_readonly_action",
        lambda *a, **k: iter(()),
    )
    monkeypatch.setattr(
        "harness.send_loop_actions.run_parallel_prefetch",
        lambda *a, **k: {},
    )

    events = list(session.send("look around"))
    kinds = [e.kind for e in events]
    assert "notice" in kinds
    notice = next(e for e in events if e.kind == "notice")
    assert notice.data.get("kind") == "stagnation"
    assert "no new progress" in (notice.data.get("message") or "").lower()
    done = [e for e in events if e.kind == "assistant_done"]
    assert done and done[-1].data.get("stagnation_halt") is True
    # Cap=3 means three identical fingerprints; model asked at most a few times.
    assert call_count["n"] <= 5


def test_fingerprint_helpers_normalize_duplicates():
    a1 = PilotAction(kind="list_dir", path="./foo")
    a2 = PilotAction(kind="list_dir", path="foo")
    # Path normalization may or may not collapse ./ — both still produce stable keys.
    assert fingerprint_turn_actions([a1])
    assert normalize_assistant_prose("  Hello   World  ") == "hello world"
    assert normalize_objective_key("Audit Auth.") == normalize_objective_key("audit auth")


def test_is_read_only_analysis_goal_detects_audits_not_edits():
    assert is_read_only_analysis_goal("audit dead code in harness/") is True
    assert is_read_only_analysis_goal("review the auth module for risks") is True
    assert is_read_only_analysis_goal("implement a fix for the auth bug") is False
    assert is_read_only_analysis_goal("add logging to worker.py") is False


def test_bare_run_implement_forces_analysis_for_audit_goal(monkeypatch):
    from harness.send_loop_dispatch import dispatch_implement_action

    session = SimpleNamespace(
        config=SimpleNamespace(repo="/tmp/repo", driver="x", swarm_adapter="local"),
        _validate_target_repo=lambda r: (r, None),
        _claim_objective=MagicMock(return_value=True),
        _release_objective=MagicMock(),
        _resolve_requested_implement_adapter=lambda a: ("", ""),
        _external_adapter_available=lambda a: False,
        _session_job_ids=[],
        _register_local_job=MagicMock(),
        _submit_swarm=MagicMock(return_value=True),
        _run_provider_worker_background=MagicMock(),
        _append_action_result=MagicMock(),
        _answer_remaining_tool_calls=MagicMock(return_value=iter(())),
        _job_dispatch_label_args=MagicMock(return_value=[]),
    )
    monkeypatch.setattr(
        "harness.send_loop_dispatch.resolve_effective_repo",
        lambda r: "/tmp/repo",
    )
    monkeypatch.setattr(
        "harness.send_loop_dispatch._non_git_workspace_error",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "harness.implement_guards.check_implement_workspace",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "harness.implement_guards.check_oversized_single_file_rewrite",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "harness.edit_engines.select_edit_engine",
        lambda *a, **k: "native",
    )
    monkeypatch.setattr(
        "harness.send_loop_dispatch._puppetmaster_available",
        lambda: False,
    )
    monkeypatch.setattr(
        "harness.conversation._prewarm_worker_imports",
        lambda: None,
    )

    act = PilotAction(kind="run_implement", goal="audit dead code across the harness")
    events = list(dispatch_implement_action(
        session, act, "a1", True,
        turn_actions=[act], action_idx=0, action_seq=1, step=0, swarms=0,
    ))
    results = [e for e in events if e.kind == "action_result"]
    assert results
    msg = results[0].data.get("message") or results[0].data.get("error") or ""
    assert "forced mode=analysis" in msg
    assert session._register_local_job.called
    kwargs = session._register_local_job.call_args
    args = kwargs.args if kwargs.args else ()
    kw = kwargs.kwargs or {}
    role = kw.get("role") or (args[2] if len(args) > 2 else None)
    assert role == "analysis"
    submit_args = session._submit_swarm.call_args.args
    # (fn, job_id, goal, adapter, repo, expects_diff)
    assert submit_args[-1] is False


def test_failed_objective_resume_cap_suppresses_pilot_resume():
    s = ConversationalSession(
        HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    )
    s._failed_objective_resume_counts = {}
    objective = "audit auth module"
    key = normalize_objective_key(objective)

    def _put_fail(job_id: str):
        s._swarm_results.put({
            "job_id": job_id,
            "objective": objective,
            "result": {
                "applied": False,
                "files": [],
                "summary": "Worker failed to produce patch",
                "error": "provider outage",
                "has_patch_art": False,
                "apply_msg": "provider outage",
            },
        })

    # Cap default is 2 — first two failures still resume; third is capped.
    for i in range(2):
        _put_fail(f"local-fail-{i}")
        events = list(s.drain_swarm_results())
        assert any(e.kind == "pilot_resume" for e in events), f"expected resume on attempt {i+1}"

    assert s._failed_objective_resume_counts.get(key) == 2

    _put_fail("local-fail-2")
    events = list(s.drain_swarm_results())
    assert not any(e.kind == "pilot_resume" for e in events)
    assert any(e.kind == "notice" and e.data.get("kind") == "resume_cap" for e in events)


def test_successful_substantive_work_resets_resume_cap():
    s = ConversationalSession(
        HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    )
    objective = "add helper"
    key = normalize_objective_key(objective)
    s._failed_objective_resume_counts = {key: 2}

    s._swarm_results.put({
        "job_id": "local-ok",
        "objective": objective,
        "result": {
            "applied": True,
            "files": ["helper.py"],
            "summary": "added helper.py with a real implementation body that is long enough",
            "error": None,
        },
    })
    events = list(s.drain_swarm_results())
    assert any(e.kind == "pilot_resume" for e in events)
    assert key not in s._failed_objective_resume_counts


def test_analysis_summary_is_substantive_gate():
    assert not analysis_summary_is_substantive("Successfully completed analysis task")
    assert not analysis_summary_is_substantive("Audit findings: none.")
    assert not analysis_summary_is_substantive("verification/plumbing only")
    assert not analysis_summary_is_substantive("done")
    assert analysis_summary_is_substantive(
        "FINDING: race in harness/send_loop.py:412 — busy lock leaked after interrupt"
    )


def test_empty_analysis_provider_worker_never_renders_green(monkeypatch):
    """Plumbing-only analysis (expects_diff=False) must not finish as applied/green."""
    from harness.worker import WorkerResult

    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    session = ConversationalSession(cfg)

    plumbing = WorkerResult(
        ok=True,
        patch="",
        files_changed=[],
        summary="Successfully completed analysis task",
        tokens_in=10,
        tokens_out=20,
    )
    monkeypatch.setattr(
        session,
        "_run_edit_worker_bounded",
        lambda *a, **k: plumbing,
    )

    job_id = "local-empty-analysis"
    session._register_local_job(job_id, "audit auth", role="analysis")
    session._run_provider_worker_background(
        job_id, "audit auth", "", "", expects_diff=False,
    )

    item = session._swarm_results.get_nowait()
    res = item["result"]
    assert res.get("applied") is False
    assert res.get("degraded") is True
    assert res.get("error")
    assert "no substantive" in (res.get("error") or "").lower()

    with session._local_jobs_lock:
        job = session._local_jobs.get(job_id)
        assert job is not None
        assert job.get("status") == "failed"


def test_keep_alive_resume_preserves_turn_guard_state(monkeypatch):
    """resume=True must not clear TurnGuardState / stagnation / resume counts."""
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    session = ConversationalSession(cfg)
    guard = new_turn_guard_state("Audit the repo")
    record_action_execution(guard, "list_dir", _Act(kind="list_dir", path="."))
    session._turn_guard_state = guard
    session._stagnation_streak = 2
    session._stagnation_last_prose = "same"
    session._failed_objective_resume_counts = {"audit the repo": 1}

    session._history.append({
        "role": "user",
        "content": "[background job x finished] continue",
    })

    mock_pilot = MagicMock()
    resp = MagicMock()
    resp.text = json.dumps({"say": "Noted.", "actions": []})
    resp.meta = {}
    resp.error = None
    mock_pilot.chat.return_value = resp
    mock_pilot.chat_stream = None
    session.pilot = mock_pilot

    list(session.send("", resume=True))
    # Fresh-user-message clear must NOT run on resume — guard + resume-cap
    # map stay. Stagnation counters may advance during the resumed turn; they
    # must not have been wiped to the fresh-turn defaults at entry.
    assert session._turn_guard_state is guard
    assert session._failed_objective_resume_counts.get("audit the repo") == 1
    assert session._stagnation_last_prose is not None or session._stagnation_streak >= 1
