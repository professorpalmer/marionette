"""Complete swarm delivery receipts for model-visible history."""
from __future__ import annotations

from harness.pilot import PilotAction
from harness.repo_resolve import resolve_effective_repo


def test_sync_swarm_pushes_every_artifact_into_receipt_and_synthesis(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    import harness.send_loop_dispatch as dispatch
    from harness.send_loop_dispatch import dispatch_swarm_action

    monkeypatch.delenv("HARNESS_ALLOW_DEMO_SWARM", raising=False)
    monkeypatch.setattr(dispatch, "_non_git_workspace_error", lambda *_a, **_k: None)

    findings = [
        {
            "type": "finding",
            "id": f"artifact-{i}",
            "job_id": "job_hf",
            "task_id": f"task-{i % 5}",
            "sha256": f"sha-{i}",
            "headline": f"Finding number {i} with evidence in harness/foo.py:{i}",
            "body": ("DETAIL " * 80) + f" #{i}",
            "evidence": [{"path": f"harness/foo.py", "line": i}],
        }
        for i in range(17)
    ]
    result = SimpleNamespace(
        job_id="job_hf",
        adapter="agentic",
        mode="swarm",
        status="done",
        num_artifacts=len(findings),
        artifact_types=["finding"],
        artifacts=findings,
        auth_failure="",
        summary="fat summary " + ("x" * 4000),
    )
    monkeypatch.setattr(
        dispatch, "stream_swarm", lambda session, intent, q, *_a: q.put(("done", result)),
    )
    complete_rows = [
        SimpleNamespace(
            id=f"artifact-{i}",
            task_id=f"task-{i % 5}",
            sha256=f"sha-{i}",
            type="finding",
        )
        for i in range(17)
    ]
    session = SimpleNamespace(
        config=SimpleNamespace(repo="/repo"),
        state_dir="/tmp",
        _session_job_ids=[],
        _register_local_job=MagicMock(),
        _finish_local_job=MagicMock(),
        _append_action_result=MagicMock(),
        _display_transcript=[],
        # ConversationalSession.state() is a UI status string; the ledger is
        # session.durable. Tests must stub that property, not state().
        state=lambda: "thinking",
        durable=SimpleNamespace(
            store=SimpleNamespace(list_artifacts=lambda _job_id: complete_rows),
            format_artifacts=lambda _rows: findings,
        ),
    )
    events = list(dispatch_swarm_action(
        session,
        PilotAction(
            kind="run_swarm",
            goal="audit",
            roles=[
                "explore",
                "pipeline-mapper",
                "decision-explainer",
                "conflict-auditor",
                "test-coverage-reviewer",
            ],
            arguments={},
        ),
        "a-1",
        True,
        counters={"swarms": 0, "demo_swarms": 0},
        turn_findings=[],
    ))

    action_result = next(event.data for event in events if event.kind == "action_result")
    assert len(action_result["artifacts"]) == 12
    assert "artifact_delivery" not in action_result
    swarm_result = next(event.data["result"] for event in events if event.kind == "swarm_result")
    assert [row["id"] for row in swarm_result["artifacts"]] == [
        f"artifact-{i}" for i in range(17)
    ]
    assert swarm_result["artifact_delivery"] == {
        "pm_artifacts": 17,
        "available_to_inspect": 17,
        "complete": True,
        "missing": [],
    }
    assert swarm_result["cwd"] == resolve_effective_repo("/repo")

    text = session._append_action_result.call_args.args[2]
    assert "PM artifacts: 17" in text
    assert "Available to inspect: 17/17" in text
    assert "peek_artifact" not in text
    for i in range(17):
        assert f"artifact://job_hf/artifact-{i}" in text
        assert f"sha-{i}" in text
    assert "DETAIL DETAIL" not in text
    raw_rows = [
        SimpleNamespace(
            id=f"artifact-{i}",
            task_id=f"task-{i % 5}",
            sha256=f"sha-{i}",
            type="finding",
        )
        for i in range(17)
    ]
    session.durable = SimpleNamespace(
        store=SimpleNamespace(list_artifacts=lambda _job_id: raw_rows),
        # Simulate one PM row that could not be projected into Mari's inspectable ledger.
        format_artifacts=lambda _rows: findings[:16],
    )
    result.artifacts = findings[:16]
    result.num_artifacts = 16
    session._append_action_result.reset_mock()

    partial_events = list(dispatch_swarm_action(
        session,
        PilotAction(kind="run_swarm", goal="partial audit", roles=["explore"], arguments={}),
        "a-2",
        True,
        counters={"swarms": 0, "demo_swarms": 0},
        turn_findings=[],
    ))
    partial_action_result = next(event.data for event in partial_events if event.kind == "action_result")
    assert len(partial_action_result["artifacts"]) == 12
    assert "artifact_delivery" not in partial_action_result
    partial_result = next(
        event.data["result"] for event in partial_events if event.kind == "swarm_result"
    )
    assert len(partial_result["artifacts"]) == 16
    assert partial_result["artifact_delivery"] == {
        "pm_artifacts": 17,
        "available_to_inspect": 16,
        "complete": False,
        "missing": [{"id": "artifact-16", "task_id": "task-1"}],
    }
    partial_manifest = session._append_action_result.call_args.args[2]
    assert "Synthesis continued with incomplete PM evidence" in partial_manifest
    assert "missing artifact-16 task=task-1" in partial_manifest
    assert session._append_action_result.call_args.kwargs["force_inline"] is True


def test_sync_swarm_store_miss_does_not_claim_complete(monkeypatch):
    """ConversationalSession.state() is a UI status string; missing durable must not green the receipt."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    import harness.send_loop_dispatch as dispatch
    from harness.send_loop_dispatch import dispatch_swarm_action

    monkeypatch.delenv("HARNESS_ALLOW_DEMO_SWARM", raising=False)
    monkeypatch.setattr(dispatch, "_non_git_workspace_error", lambda *_a, **_k: None)
    monkeypatch.setattr(
        dispatch, "stream_swarm",
        lambda session, intent, q, *_a: q.put(("done", SimpleNamespace(
            job_id="job_miss",
            adapter="agentic",
            mode="swarm",
            status="done",
            num_artifacts=2,
            artifact_types=["finding"],
            artifacts=[
                {
                    "type": "finding",
                    "id": "artifact-0",
                    "job_id": "job_miss",
                    "task_id": "task-0",
                    "sha256": "sha-0",
                    "headline": "Finding in harness/foo.py:1",
                    "evidence": [{"path": "harness/foo.py", "line": 1}],
                },
                {
                    "type": "finding",
                    "id": "artifact-1",
                    "job_id": "job_miss",
                    "task_id": "task-1",
                    "sha256": "sha-1",
                    "headline": "Finding in harness/foo.py:2",
                    "evidence": [{"path": "harness/foo.py", "line": 2}],
                },
            ],
            auth_failure="",
            summary="ok",
        ))),
    )
    session = SimpleNamespace(
        config=SimpleNamespace(repo="/repo"),
        state_dir="/tmp",
        _session_job_ids=[],
        _register_local_job=MagicMock(),
        _finish_local_job=MagicMock(),
        _append_action_result=MagicMock(),
        _display_transcript=[],
        state=lambda: "thinking",
    )
    events = list(dispatch_swarm_action(
        session,
        PilotAction(kind="run_swarm", goal="audit", roles=["explore"], arguments={}),
        "a-miss",
        True,
        counters={"swarms": 0, "demo_swarms": 0},
        turn_findings=[],
    ))
    swarm_result = next(event.data["result"] for event in events if event.kind == "swarm_result")
    assert swarm_result["artifact_delivery"]["complete"] is False
    assert swarm_result["artifact_delivery"]["missing"] == [
        {"id": "pm-store", "task_id": "unavailable"},
    ]
    manifest = session._append_action_result.call_args.args[2]
    assert "Synthesis continued with incomplete PM evidence" in manifest
    assert "missing pm-store task=unavailable" in manifest
    assert "artifact://job_miss/artifact-0" in manifest
    assert "artifact://job_miss/artifact-1" in manifest


def test_session_durable_ignores_ui_status_string():
    from types import SimpleNamespace

    from harness.send_loop_dispatch import _session_durable

    ledger = SimpleNamespace(
        store=SimpleNamespace(list_artifacts=lambda _job_id: []),
        format_artifacts=lambda _rows: [],
    )
    session = SimpleNamespace(state=lambda: "thinking", durable=ledger)
    assert _session_durable(session) is ledger


def test_drain_swarm_results_history_is_complete_delivery(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "harness.conversation.RuleStore",
        lambda *a, **k: __import__("harness.rule_store", fromlist=["RuleStore"]).RuleStore(
            path=str(tmp_path / "rules.json")
        ),
    )
    monkeypatch.setattr("harness.memory_store.MEMORY_PATH", tmp_path / "mem.json")
    import tempfile

    from harness.config import HarnessConfig
    from harness.conversation import ConversationalSession

    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    session = ConversationalSession(cfg)
    fat_summary = "FULL SUMMARY " + ("word " * 500)
    session._swarm_results.put({
        "job_id": "job_drain_hf",
        "objective": "handle first drain",
        "result": {
            "applied": True,
            "files": ["a.py"],
            "summary": fat_summary,
            "artifacts": [
                {
                    "type": "finding",
                    "id": "job_drain_hf-finding-0",
                    "headline": "Drain headline one",
                },
            ],
            "analysis_ok": True,
        },
    })
    events = list(session.drain_swarm_results())
    assert any(e.kind == "swarm_result" for e in events)
    hist = [
        m["content"] for m in session._history
        if m.get("role") == "assistant" and "swarm result for" in (m.get("content") or "")
    ]
    assert hist
    assert "PM SWARM ARTIFACT MANIFEST:" in hist[0]
    assert "job_drain_hf-finding-0" in hist[0]
    assert "artifact://job_drain_hf/job_drain_hf-finding-0" in hist[0]
    assert "Drain headline one" in hist[0]
    assert "peek_artifact" not in hist[0]
    assert fat_summary not in hist[0]
    display = [d for d in session._display_transcript if d.get("type") == "swarm_result"]
    assert display
    assert display[0].get("summary") == fat_summary
    assert display[0]["artifact_delivery"]["complete"] is True
    assert [row["id"] for row in display[0]["artifacts"]] == ["job_drain_hf-finding-0"]
