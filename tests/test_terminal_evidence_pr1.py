"""PR 1: complete terminal evidence for every single job.

Public-boundary RED: exact (job_id, task_id, artifact_id, sha256, type) sets
must agree across the job snapshot, the durable receipt, and the pilot
synthesis manifest. First-N / handle-first projections are not authority.
"""
from __future__ import annotations

import json
import tempfile
from unittest.mock import patch

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.local_job_artifacts import normalize_finding_artifacts


def _identity(job_id, row):
    return (
        str(job_id),
        str(row.get("task_id") or ""),
        str(row.get("id") or ""),
        str(row.get("sha256") or ""),
        str(row.get("type") or ""),
    )


def _session(tmp_path):
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    cfg.repo = str(tmp_path)
    return ConversationalSession(cfg)


def _canonical_rows(job_id, n=17):
    return [
        {
            "type": "finding",
            "id": f"{job_id}-finding-{i}",
            "task_id": f"task-{i % 5}",
            "sha256": f"sha-{i}",
            "headline": f"Finding {i} in harness/foo.py:{i}",
        }
        for i in range(n)
    ]


def test_background_drain_receipt_matches_canonical_identity_set(tmp_path):
    job_id = "job_pr1_bg"
    rows = _canonical_rows(job_id)
    expected = {_identity(job_id, r) for r in rows}
    session = _session(tmp_path)
    session._local_jobs[job_id] = {
        "id": job_id,
        "cwd": str(tmp_path),
        "artifacts": rows,
    }
    session._swarm_results.put({
        "job_id": job_id,
        "objective": "audit the router",
        "result": {
            "applied": True,
            "files": [],
            "summary": "FULL SUMMARY " + ("word " * 400),
            "analysis_ok": True,
            "artifacts": rows,
        },
    })

    events = list(session.drain_swarm_results())
    swarm = next(e.data["result"] for e in events if e.kind == "swarm_result")
    receipt = {_identity(job_id, r) for r in swarm["artifacts"]}
    assert receipt == expected
    assert swarm["artifact_delivery"]["pm_artifacts"] == 17
    assert swarm["artifact_delivery"]["available_to_inspect"] == 17
    assert swarm["artifact_delivery"]["complete"] is True
    assert swarm["artifact_delivery"]["missing"] == []

    display = next(d for d in session._display_transcript if d.get("job_id") == job_id)
    assert {_identity(job_id, r) for r in display["artifacts"]} == expected
    assert display["artifact_delivery"] == swarm["artifact_delivery"]

    hist = next(
        m["content"] for m in session._history
        if m.get("role") == "assistant" and job_id in (m.get("content") or "")
    )
    assert "PM SWARM ARTIFACT MANIFEST:" in hist
    assert "Available to inspect: 17/17" in hist
    assert "peek_artifact" not in hist
    assert "FETCH full bodies" not in hist
    assert "FULL SUMMARY" not in hist
    for i in range(17):
        assert f"{job_id}-finding-{i}" in hist
        assert f"sha-{i}" in hist


def test_failed_and_held_jobs_still_carry_complete_receipt(tmp_path):
    session = _session(tmp_path)
    failed_id = "job_pr1_fail"
    held_id = "job_pr1_held"
    fail_rows = _canonical_rows(failed_id, n=3)
    held_rows = _canonical_rows(held_id, n=4)
    session._swarm_results.put({
        "job_id": failed_id,
        "objective": "failed implement",
        "result": {
            "applied": False,
            "files": [],
            "summary": "worker died",
            "error": "provider timeout",
            "degraded": True,
            "artifacts": fail_rows,
        },
    })
    session._swarm_results.put({
        "job_id": held_id,
        "objective": "held implement",
        "result": {
            "applied": False,
            "files": [],
            "summary": "held",
            "held_for_review": True,
            "artifacts": held_rows,
        },
    })

    events = list(session.drain_swarm_results())
    results = {
        e.data["job_id"]: e.data["result"]
        for e in events if e.kind == "swarm_result"
    }
    assert {_identity(failed_id, r) for r in results[failed_id]["artifacts"]} == {
        _identity(failed_id, r) for r in fail_rows
    }
    assert results[failed_id]["artifact_delivery"]["complete"] is True
    assert {_identity(held_id, r) for r in results[held_id]["artifacts"]} == {
        _identity(held_id, r) for r in held_rows
    }
    assert results[held_id]["artifact_delivery"]["complete"] is True


def test_partial_delivery_names_missing_evidence(tmp_path):
    job_id = "job_pr1_partial"
    rows = _canonical_rows(job_id, n=5)
    expected_plus_missing = rows + [{
        "type": "finding",
        "id": f"{job_id}-finding-missing",
        "task_id": "task-x",
        "sha256": "sha-missing",
        "headline": "ghost",
    }]
    session = _session(tmp_path)
    from types import SimpleNamespace
    from unittest.mock import patch

    ledger = SimpleNamespace(
        store=SimpleNamespace(
            list_artifacts=lambda _jid: [
                SimpleNamespace(
                    id=r["id"], task_id=r["task_id"], sha256=r["sha256"], type=r["type"],
                )
                for r in expected_plus_missing
            ]
        ),
        format_artifacts=lambda _raw: rows,
    )
    with patch("harness.send_loop_dispatch._session_durable", return_value=ledger):
        session._swarm_results.put({
            "job_id": job_id,
            "objective": "partial",
            "result": {
                "applied": True,
                "files": [],
                "summary": "partial",
                "analysis_ok": True,
                "artifacts": rows,
            },
        })
        events = list(session.drain_swarm_results())
    swarm = next(e.data["result"] for e in events if e.kind == "swarm_result")
    assert swarm["artifact_delivery"]["complete"] is False
    assert swarm["artifact_delivery"]["missing"] == [
        {"id": f"{job_id}-finding-missing", "task_id": "task-x"},
    ]
    hist = next(
        m["content"] for m in session._history
        if m.get("role") == "assistant" and job_id in (m.get("content") or "")
    )
    assert "Synthesis continued with incomplete PM evidence" in hist
    assert f"missing {job_id}-finding-missing task=task-x" in hist


def test_await_and_apply_ar_list_is_unbounded(tmp_path):
    import subprocess

    session = _session(tmp_path)
    artifacts = [
        {
            "id": f"art-{i}",
            "task_id": f"task-{i}",
            "sha256": f"sha-{i}",
            "type": "finding",
            "payload": {"claim": f"claim {i}", "report": f"report {i}"},
        }
        for i in range(12)
    ]
    original_run = subprocess.run

    def mock_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and any(arg == "await" for arg in cmd):
            return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")
        if isinstance(cmd, list) and any(arg == "artifacts" for arg in cmd):
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps(artifacts), stderr="",
            )
        return original_run(cmd, *args, **kwargs)

    with patch("subprocess.run", side_effect=mock_run):
        res = session._await_and_apply_job("job_pr1_pm")

    assert len(res["ar_list"]) == 12
    assert len(res["artifacts"]) == 12
    got = {_identity("job_pr1_pm", r) for r in res["ar_list"]}
    expected = {
        ("job_pr1_pm", f"task-{i}", f"art-{i}", f"sha-{i}", "finding")
        for i in range(12)
    }
    assert got == expected


def test_finding_normalization_keeps_every_substantive_row():
    findings = [
        {"type": "finding", "headline": f"Finding {i} in harness/foo.py:{i}"}
        for i in range(25)
    ]
    out = normalize_finding_artifacts("job_pr1_find", findings)
    assert len(out) == 25
    assert [row["id"] for row in out] == [
        f"job_pr1_find-finding-{i}" for i in range(25)
    ]


def test_ephemeral_state_cleanup_does_not_drop_snapshot_refs(tmp_path):
    """Single-job snapshot survives after the PM state_dir is removed."""
    job_id = "job_pr1_snap"
    rows = _canonical_rows(job_id, n=9)
    session = _session(tmp_path)
    session._swarm_results.put({
        "job_id": job_id,
        "objective": "late drain",
        "result": {
            "applied": True,
            "files": ["a.py"],
            "summary": "ok",
            "artifacts": rows,
        },
    })
    events = list(session.drain_swarm_results())
    swarm = next(e.data["result"] for e in events if e.kind == "swarm_result")
    assert {_identity(job_id, r) for r in swarm["artifacts"]} == {
        _identity(job_id, r) for r in rows
    }
    assert swarm["artifact_delivery"]["complete"] is True
