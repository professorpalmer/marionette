"""PR 2: compose parallel waves from independent child receipts.

Public-boundary RED: each accepted child owns its exact
(job_id, task_id, artifact_id, sha256, type) receipt. The parent owns
membership and aggregate lifecycle only — never a merged artifact set.
"""
from __future__ import annotations

import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession


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


def _child_rows(job_id, *, sha_prefix, n=4, artifact_id="shared-finding"):
    """Identical artifact ids across jobs; identity is namespaced by job_id."""
    return [
        {
            "type": "finding",
            "id": artifact_id if n == 1 else f"{artifact_id}-{i}",
            "task_id": f"task-{i}",
            "sha256": f"{sha_prefix}-{i}",
            "headline": f"{job_id} finding {i}",
            "job_id": job_id,
        }
        for i in range(n)
    ]


def _enqueue(session, job_id, rows, *, applied=True, error=None, degraded=False):
    session._local_jobs.setdefault(job_id, {"id": job_id, "artifacts": rows})
    session._local_jobs[job_id]["artifacts"] = rows
    session._swarm_results.put({
        "job_id": job_id,
        "objective": f"goal for {job_id}",
        "result": {
            "applied": applied,
            "files": [],
            "summary": f"summary {job_id}",
            "error": error,
            "degraded": degraded,
            "analysis_ok": bool(applied) and not error,
            "artifacts": rows,
        },
    })


def _results_by_job(events):
    return {
        e.data["job_id"]: e.data["result"]
        for e in events if e.kind == "swarm_result"
    }


def test_parallel_children_keep_exact_independent_receipts(tmp_path):
    session = _session(tmp_path)
    ok_id = "job_pr2_ok"
    bad_id = "job_pr2_fail"
    ok_rows = _child_rows(ok_id, sha_prefix="sha-ok", n=5)
    bad_rows = _child_rows(bad_id, sha_prefix="sha-bad", n=5)
    # Same artifact ids, different job ids — must not leak.
    assert [r["id"] for r in ok_rows] == [r["id"] for r in bad_rows]

    wave = session._register_parallel_wave(
        "local-wave-pr2",
        child_job_ids=[ok_id, bad_id],
        objective="Parallel wave of goals: ok, fail",
        action_id="a-pr2",
    )
    assert wave["child_job_ids"] == [ok_id, bad_id]
    assert wave["status"] == "running"
    assert wave.get("terminal_receipt") is None
    assert list(wave.get("artifacts") or []) == []

    _enqueue(session, ok_id, ok_rows)
    first = _results_by_job(list(session.drain_swarm_results()))
    assert set(first) == {ok_id}
    assert {_identity(ok_id, r) for r in first[ok_id]["artifacts"]} == {
        _identity(ok_id, r) for r in ok_rows
    }
    assert first[ok_id]["artifact_delivery"]["complete"] is True
    assert first[ok_id]["artifact_delivery"]["pm_artifacts"] == 5

    parent = session._local_jobs["local-wave-pr2"]
    assert parent["status"] == "running"
    assert parent.get("terminal_receipt") is None
    assert ok_id in parent["terminal_job_ids"]
    assert bad_id not in parent["terminal_job_ids"]
    assert list(parent.get("artifacts") or []) == []

    pending = next(
        row for row in session.export_display_transcript()
        if row.get("type") == "swarm_pending"
    )
    assert set(pending["job_ids"]) == {ok_id, bad_id}
    assert set(pending["terminal_job_ids"]) == {ok_id}
    assert pending.get("status") != "done"

    # Reload membership without losing the open parent or the settled child.
    payload = session.export_transcript_data()
    reloaded = _session(tmp_path)
    reloaded._local_jobs = {
        jid: dict(row) for jid, row in session._local_jobs.items()
    }
    reloaded.load_history(payload)
    parent = reloaded._local_jobs["local-wave-pr2"]
    assert parent["status"] == "running"
    assert set(parent["child_job_ids"]) == {ok_id, bad_id}
    display_ok = next(
        row for row in reloaded._display_transcript
        if row.get("type") == "swarm_result" and row.get("job_id") == ok_id
    )
    assert {_identity(ok_id, r) for r in display_ok["artifacts"]} == {
        _identity(ok_id, r) for r in ok_rows
    }

    _enqueue(reloaded, bad_id, bad_rows, applied=False, error="worker died", degraded=True)
    second = _results_by_job(list(reloaded.drain_swarm_results()))
    assert set(second) == {bad_id}
    assert {_identity(bad_id, r) for r in second[bad_id]["artifacts"]} == {
        _identity(bad_id, r) for r in bad_rows
    }
    assert second[bad_id]["artifact_delivery"]["complete"] is True
    # Failed sibling kept its own truth; success receipt is untouched.
    display_ok = next(
        row for row in reloaded._display_transcript
        if row.get("type") == "swarm_result" and row.get("job_id") == ok_id
    )
    assert {_identity(ok_id, r) for r in display_ok["artifacts"]} == {
        _identity(ok_id, r) for r in ok_rows
    }
    assert "sha-bad" not in {r.get("sha256") for r in display_ok["artifacts"]}

    parent = reloaded._local_jobs["local-wave-pr2"]
    assert set(parent["terminal_job_ids"]) == {ok_id, bad_id}
    assert parent["status"] in {"failed", "completed"}
    receipt = parent["terminal_receipt"]
    assert receipt is not None
    assert set(receipt["child_job_ids"]) == {ok_id, bad_id}
    assert "artifacts" not in receipt
    assert list(parent.get("artifacts") or []) == []

    pending = next(
        row for row in reloaded.export_display_transcript()
        if row.get("type") == "swarm_pending"
    )
    assert set(pending["terminal_job_ids"]) == {ok_id, bad_id}
    assert pending.get("status") == "done"


def test_identical_artifact_ids_do_not_leak_through_shared_store(tmp_path):
    session = _session(tmp_path)
    a_id = "job_pr2_a"
    b_id = "job_pr2_b"
    # One artifact id, two jobs, two hashes.
    a_rows = _child_rows(a_id, sha_prefix="sha-a", n=1, artifact_id="shared-id")
    b_rows = _child_rows(b_id, sha_prefix="sha-b", n=1, artifact_id="shared-id")
    session._register_parallel_wave(
        "local-wave-leak",
        child_job_ids=[a_id, b_id],
        objective="leak probe",
        action_id="a-leak",
    )

    mixed = [
        SimpleNamespace(
            id=r["id"], task_id=r["task_id"], sha256=r["sha256"],
            type=r["type"], job_id=r["job_id"],
        )
        for r in a_rows + b_rows
    ]
    ledger = SimpleNamespace(
        store=SimpleNamespace(list_artifacts=lambda _jid: mixed),
        format_artifacts=lambda raw: [
            {
                "id": getattr(x, "id", ""),
                "task_id": getattr(x, "task_id", ""),
                "sha256": getattr(x, "sha256", ""),
                "type": getattr(x, "type", ""),
                "job_id": getattr(x, "job_id", ""),
            }
            for x in raw
        ],
    )
    with patch("harness.send_loop_dispatch._session_durable", return_value=ledger):
        _enqueue(session, a_id, a_rows)
        _enqueue(session, b_id, b_rows)
        results = _results_by_job(list(session.drain_swarm_results()))

    assert {_identity(a_id, r) for r in results[a_id]["artifacts"]} == {
        _identity(a_id, r) for r in a_rows
    }
    assert {_identity(b_id, r) for r in results[b_id]["artifacts"]} == {
        _identity(b_id, r) for r in b_rows
    }
    assert results[a_id]["artifact_delivery"]["pm_artifacts"] == 1
    assert results[b_id]["artifact_delivery"]["pm_artifacts"] == 1
    assert [r.get("sha256") for r in results[a_id]["artifacts"]] == ["sha-a-0"]
    assert [r.get("sha256") for r in results[b_id]["artifacts"]] == ["sha-b-0"]
