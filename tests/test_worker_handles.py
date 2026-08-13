"""Handle-first worker/swarm result formatting."""
from __future__ import annotations

from harness.pilot import PilotAction
from harness.worker_handles import format_handle_first_result


def test_format_handle_first_includes_job_id_and_fetch_hint():
    arts = [
        {"type": "finding", "id": "job1-finding-0", "headline": "Alpha risk in auth"},
        {"type": "risk", "id": "job1-finding-1", "headline": "Beta race on writes"},
        {"type": "decision", "id": "job1-finding-2", "headline": "Gamma keep gate"},
        {"type": "finding", "id": "job1-finding-3", "headline": "Delta ignored"},
    ]
    text = format_handle_first_result("job1", arts, max_headlines=3)
    assert "job_id=job1" in text
    assert "artifact://job1/job1-finding-0" in text
    assert "Alpha risk" in text
    assert "FETCH" in text
    assert "peek_artifact" in text
    assert "Delta ignored" not in text
    assert "+1 more" in text


def test_format_handle_first_empty_arts():
    text = format_handle_first_result("job_empty", [])
    assert "job_id=job_empty" in text
    assert "no artifacts" in text
    assert "FETCH" in text


def test_sync_swarm_default_is_handle_first(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    import harness.send_loop_dispatch as dispatch
    from harness.send_loop_dispatch import dispatch_swarm_action

    monkeypatch.delenv("HARNESS_ALLOW_DEMO_SWARM", raising=False)
    monkeypatch.setattr(dispatch, "_non_git_workspace_error", lambda *_a, **_k: None)

    findings = [
        {
            "type": "finding",
            "headline": f"Finding number {i} with evidence in harness/foo.py:{i}",
            "body": ("DETAIL " * 80) + f" #{i}",
            "evidence": [{"path": f"harness/foo.py", "line": i}],
        }
        for i in range(6)
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
    session = SimpleNamespace(
        config=SimpleNamespace(repo="/repo"),
        state_dir="/tmp",
        _session_job_ids=[],
        _register_local_job=MagicMock(),
        _finish_local_job=MagicMock(),
        _append_action_result=MagicMock(),
        _display_transcript=[],
    )
    list(dispatch_swarm_action(
        session,
        PilotAction(kind="run_swarm", goal="audit", roles=["explore"], arguments={}),
        "a-1",
        True,
        counters={"swarms": 0, "demo_swarms": 0},
        turn_findings=[],
    ))
    text = session._append_action_result.call_args.args[2]
    assert "job_id=" in text
    assert "FETCH" in text
    assert "peek_artifact" in text
    # Default must be much shorter than a full digest paste of all bodies.
    assert len(text) < 2500
    assert "DETAIL DETAIL" not in text

    # full_digest=true restores verbose digest lines.
    session._append_action_result.reset_mock()
    list(dispatch_swarm_action(
        session,
        PilotAction(
            kind="run_swarm",
            goal="audit",
            roles=["explore"],
            arguments={"full_digest": True},
        ),
        "a-2",
        True,
        counters={"swarms": 0, "demo_swarms": 0},
        turn_findings=[],
    ))
    full = session._append_action_result.call_args.args[2]
    assert "[finding]" in full
    assert "job_id=" in full or "job=" in full
    # Verbose path still keeps safety notes available; body includes digest rows.
    assert len(full) > len(text)


def test_drain_swarm_results_history_is_handle_first(monkeypatch, tmp_path):
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
    assert "job_id=job_drain_hf" in hist[0]
    assert "Drain headline one" in hist[0]
    assert "artifact://" in hist[0]
    assert fat_summary not in hist[0]
    # UI/display still gets the full summary for cards.
    display = [d for d in session._display_transcript if d.get("type") == "swarm_result"]
    assert display
    assert display[0].get("summary") == fat_summary
