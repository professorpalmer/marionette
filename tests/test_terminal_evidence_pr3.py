"""PR 3: replay reused evidence and retire model-directed discovery.

Public-boundary RED: reuse references the complete source
(job_id, task_id, artifact_id, sha256, type) set via the same delivery
projection as executed jobs. Source-job identity, zero new spend, and
prior-vs-narrow-verify stay distinct. First-N / handle-first are gone.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

from harness.pilot import PilotAction
from harness.validation_reuse import compact_delta_digest


def _identity(job_id, row):
    return (
        str(job_id),
        str(row.get("task_id") or ""),
        str(row.get("id") or ""),
        str(row.get("sha256") or ""),
        str(row.get("type") or ""),
    )


def _source_rows(job_id="local-prior", n=17):
    return [
        {
            "type": "finding",
            "id": f"{job_id}-finding-{i}",
            "task_id": f"task-{i % 5}",
            "sha256": f"sha-{i}",
            "headline": f"Finding {i} in harness/foo.py:{i}",
            "body": f"Evidence for finding {i} in harness/foo.py:{i}.",
        }
        for i in range(n)
    ]


def _reuse_decision(job, rows, *, outcome="reuse"):
    return SimpleNamespace(
        outcome=outcome,
        reason="fingerprint_match" if outcome == "reuse" else "subset_invalidated",
        source_job_id=job["id"],
        validation_fingerprint="abc123fingerprint",
        invalidated_paths=["harness/foo.py"] if outcome == "narrow_verify" else [],
        reuse_status="reused" if outcome == "reuse" else "partial",
        digest_text="",
        compact_artifacts=rows[:8],
        candidate={"id": job["id"], "artifacts": rows},
        environment_fingerprint="test-env-fingerprint",
        acceptance_criteria=[],
        narrow_roles=("conflict-auditor",) if outcome == "narrow_verify" else (),
        narrow_goal_suffix=(
            "Re-verify only these invalidated paths"
            if outcome == "narrow_verify"
            else ""
        ),
        as_provenance=lambda: {
            "reuse_status": "reused" if outcome == "reuse" else "partial",
            "source_job_id": job["id"],
            "validation_fingerprint": "abc123fingerprint",
            "reuse_reason": (
                "fingerprint_match" if outcome == "reuse" else "subset_invalidated"
            ),
        },
    )


def _session(job, cwd="/repo"):
    lock = threading.Lock()
    local = {job["id"]: dict(job)}

    def live():
        return list(local.values())

    return SimpleNamespace(
        config=SimpleNamespace(repo=cwd, driver="test-model"),
        _local_jobs=local,
        _local_jobs_lock=lock,
        live_local_jobs=live,
        _session_job_ids=[],
        _register_local_job=MagicMock(),
        _finish_local_job=MagicMock(),
        _fail_or_drop_local_job=MagicMock(),
        _append_action_result=MagicMock(),
        _display_transcript=[],
        _claim_objective=MagicMock(return_value=True),
        _release_objective=MagicMock(),
        _submit_swarm=MagicMock(return_value=True),
        _last_swarm_submit_reason="",
        _swarm_submit_reject_message=MagicMock(return_value="cap"),
        _resolve_requested_implement_adapter=MagicMock(return_value=("", "")),
        _external_adapter_available=MagicMock(return_value=False),
        _answer_remaining_tool_calls=MagicMock(return_value=iter(())),
        _run_provider_worker_background=MagicMock(),
        _validate_target_repo=MagicMock(return_value=(cwd, None)),
        durable=SimpleNamespace(
            store=SimpleNamespace(list_artifacts=lambda _jid: []),
            format_artifacts=lambda _rows: [],
        ),
    )


def test_reuse_receipt_matches_complete_source_identity_set(monkeypatch):
    job_id = "local-prior"
    rows = _source_rows(job_id)
    expected = {_identity(job_id, r) for r in rows}
    job = {
        "id": job_id,
        "goal": "audit the router",
        "artifacts": rows,
    }
    session = _session(job)

    import harness.send_loop_dispatch as dispatch

    monkeypatch.setattr(dispatch, "_non_git_workspace_error", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "harness.validation_reuse.evaluate_reuse_gate",
        lambda *a, **k: _reuse_decision(job, rows),
    )

    def boom(*_a, **_k):
        raise AssertionError("stream_swarm must not run on complete reuse")

    monkeypatch.setattr(dispatch, "stream_swarm", boom)
    events = list(dispatch.dispatch_swarm_action(
        session,
        PilotAction(kind="run_swarm", goal="audit the router", roles=["explore"]),
        "a-reuse",
        True,
        counters={"swarms": 0, "demo_swarms": 0},
        turn_findings=[],
    ))

    result = next(e.data["result"] for e in events if e.kind == "swarm_result")
    assert result["source_job_id"] == job_id
    assert result["reuse_status"] == "reused"
    assert result["adapter"] == "reuse"
    got = {_identity(job_id, r) for r in result["artifacts"]}
    assert got == expected
    assert result["artifact_delivery"] == {
        "pm_artifacts": 17,
        "available_to_inspect": 17,
        "complete": True,
        "missing": [],
    }

    action = next(e.data for e in events if e.kind == "action_result")
    assert action["num"] == 17
    assert {_identity(job_id, r) for r in action["artifacts"]} == expected
    assert "artifact_delivery" not in action

    finish = session._finish_local_job.call_args.kwargs
    assert finish["tokens"] == 0
    assert finish["est_cost_usd"] == 0.0
    assert finish["reuse_status"] == "reused"
    assert finish["source_job_id"] == job_id
    assert len(finish["findings"]) == 17

    text = session._append_action_result.call_args.args[2]
    assert "zero new execution spend" in text
    assert "PM SWARM ARTIFACT MANIFEST:" in text
    assert "Available to inspect: 17/17" in text
    assert "peek_artifact" not in text
    assert "FETCH full bodies" not in text
    for i in range(17):
        assert f"{job_id}-finding-{i}" in text
        assert f"sha-{i}" in text
        assert f"artifact://{job_id}/{job_id}-finding-{i}" in text


def test_narrow_verify_is_new_spend_and_cites_complete_prior_set(monkeypatch):
    job_id = "local-prior"
    rows = _source_rows(job_id)
    job = {"id": job_id, "goal": "audit the router", "artifacts": rows}
    session = _session(job)

    text, refs = compact_delta_digest(
        source_job_id=job_id,
        artifacts=rows,
        reuse_status="partial",
        invalidated_paths=["harness/foo.py"],
        reason="subset_invalidated",
    )
    assert len(refs) == 17
    assert {r["id"] for r in refs} == {r["id"] for r in rows}
    assert "subset_invalidated" in text
    assert "harness/foo.py" in text

    import harness.send_loop_dispatch as dispatch

    called = {"stream": False}
    monkeypatch.setattr(dispatch, "_non_git_workspace_error", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "harness.validation_reuse.evaluate_reuse_gate",
        lambda *a, **k: _reuse_decision(job, rows, outcome="narrow_verify"),
    )

    def fake_stream(_session, _intent, q, *_a, **_k):
        called["stream"] = True
        q.put(("done", SimpleNamespace(
            job_id="job_narrow",
            adapter="agentic",
            mode="swarm",
            status="done",
            num_artifacts=1,
            artifact_types=["verification"],
            artifacts=[{
                "type": "verification",
                "id": "v1",
                "task_id": "task-n",
                "sha256": "sha-n",
                "headline": "rechecked harness/foo.py",
            }],
            auth_failure="",
            summary="narrow ok",
        )))

    monkeypatch.setattr(dispatch, "stream_swarm", fake_stream)
    events = list(dispatch.dispatch_swarm_action(
        session,
        PilotAction(kind="run_swarm", goal="audit the router", roles=["explore"]),
        "a-narrow",
        True,
        counters={"swarms": 0, "demo_swarms": 0},
        turn_findings=[],
    ))
    assert called["stream"] is True
    kinds = [e.kind for e in events]
    assert "swarm_result" in kinds
    result = next(e.data["result"] for e in events if e.kind == "swarm_result")
    assert result.get("reuse_status") == "partial"
    assert result.get("source_job_id") == job_id
    assert result.get("adapter") != "reuse"
    assert session._finish_local_job.called


def test_parallel_reuse_child_receipt_keeps_complete_source_set(monkeypatch):
    job_id = "local-prior"
    rows = _source_rows(job_id)
    expected = {_identity(job_id, r) for r in rows}
    job = {"id": job_id, "goal": "review auth.py CSRF", "artifacts": rows}
    session = _session(job)

    import harness.send_loop_dispatch as dispatch

    monkeypatch.setattr(dispatch, "_non_git_workspace_error", lambda *_a, **_k: None)
    monkeypatch.setattr(dispatch, "_puppetmaster_available", lambda: False)
    monkeypatch.setattr(
        "harness.edit_engines.select_edit_engine",
        lambda *_a, **_k: "native",
    )
    monkeypatch.setattr("harness.conversation._prewarm_worker_imports", lambda: None)
    monkeypatch.setattr("harness.repo_resolve.resolve_effective_repo", lambda p: p)
    monkeypatch.setattr(
        "harness.implement_guards.check_implement_workspace",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "harness.validation_reuse.evaluate_reuse_gate",
        lambda *a, **k: _reuse_decision(job, rows),
    )

    events = list(dispatch.dispatch_parallel_action(
        session,
        PilotAction(
            kind="run_parallel",
            goals=["review auth.py CSRF"],
            mode="analysis",
        ),
        "p-reuse",
        True,
        turn_actions=[],
        action_idx=0,
        action_seq=1,
        step=0,
        swarms=0,
    ))
    session._submit_swarm.assert_not_called()
    result = next(e.data["result"] for e in events if e.kind == "swarm_result")
    assert result["source_job_id"] == job_id
    assert result["reuse_status"] == "reused"
    assert {_identity(job_id, r) for r in result["artifacts"]} == expected
    assert result["artifact_delivery"]["pm_artifacts"] == 17
    assert result["artifact_delivery"]["complete"] is True
    finish = session._finish_local_job.call_args.kwargs
    assert finish["tokens"] == 0
    assert finish["est_cost_usd"] == 0.0
    assert len(finish["findings"]) == 17


def test_compact_delta_digest_cites_every_source_artifact():
    rows = _source_rows("local-prior", n=17)
    text, refs = compact_delta_digest(
        source_job_id="local-prior",
        artifacts=rows,
        reuse_status="reused",
        reason="fingerprint_match",
    )
    assert len(refs) == 17
    assert {r["id"] for r in refs} == {row["id"] for row in rows}
    for i in range(17):
        assert f"local-prior-finding-{i}" in text
        assert f"sha-{i}" in text
    assert "peek_artifact" not in text
    assert "FETCH" not in text


def test_handle_first_formatter_is_gone():
    try:
        import harness.worker_handles as wh
    except ImportError:
        return
    assert not hasattr(wh, "format_handle_first_result")


def test_peek_artifact_schema_is_inspect_known_handle_only():
    from harness.pilot import build_tools_schema

    schema = build_tools_schema()
    peek = next(
        item for item in schema
        if item.get("function", {}).get("name") == "peek_artifact"
    )
    desc = peek["function"]["description"].lower()
    assert "known" in desc or "inspect" in desc
    assert "discover" in desc or "which evidence" in desc or "receipt" in desc
