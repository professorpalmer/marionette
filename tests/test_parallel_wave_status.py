"""Parallel-wave aggregate status, child task projection, and retryable stamps."""
from __future__ import annotations

import tempfile

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.local_jobs import aggregate_parallel_wave_status


def _session(tmp_path):
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    cfg.repo = str(tmp_path)
    sess = ConversationalSession(cfg)
    sess.harness_session_id = "sess-wave"
    return sess


def _put_child(session, job_id, *, status, role="implement", goal="", **extra):
    row = {
        "id": job_id,
        "goal": goal or job_id,
        "status": status,
        "role": role,
        "adapter": extra.pop("adapter", "agentic"),
        "model": extra.pop("model", "agentic/test"),
        "artifacts": [],
        "tasks": [{
            "id": f"{job_id}-w0",
            "role": role,
            "instruction": goal or job_id,
            "status": status,
            "adapter": "agentic",
        }],
    }
    row.update(extra)
    session._local_jobs[job_id] = row
    return row


def _pending(session):
    return next(
        row for row in session.export_display_transcript()
        if row.get("type") == "swarm_pending"
    )


def test_aggregate_status_helpers():
    assert aggregate_parallel_wave_status(["completed", "completed"]) == "completed"
    assert aggregate_parallel_wave_status(["completed", "failed"]) == "partial"
    assert aggregate_parallel_wave_status(["failed", "failed"]) == "failed"
    assert aggregate_parallel_wave_status(["timeout", "timeout"]) == "timed_out"
    assert aggregate_parallel_wave_status(["timeout", "failed"]) == "failed"
    assert aggregate_parallel_wave_status(["completed", "timeout"]) == "partial"
    assert aggregate_parallel_wave_status(["cancelled", "cancelled"]) == "cancelled"
    assert aggregate_parallel_wave_status(["completed", "cancelled"]) == "cancelled"


def test_all_success_wave_completed_pending_done(tmp_path):
    session = _session(tmp_path)
    _put_child(session, "c1", status="completed", goal="one")
    _put_child(session, "c2", status="completed", goal="two")
    wave = session._register_parallel_wave(
        "local-wave-ok",
        child_job_ids=["c1", "c2"],
        objective="all ok",
    )
    session._sync_parallel_wave_from_children("local-wave-ok")
    parent = session._local_jobs["local-wave-ok"]
    assert parent["status"] == "completed"
    assert parent["review_required"] is False
    assert parent["mixed_terminal"] is False
    assert list(parent.get("artifacts") or []) == []
    assert wave["child_job_ids"] == ["c1", "c2"]
    assert _pending(session).get("status") == "done"


def test_partial_success_wave_projects_child_tasks(tmp_path):
    session = _session(tmp_path)
    ids = [f"c{i}" for i in range(8)]
    for i, cid in enumerate(ids):
        status = "completed" if i < 4 else "failed"
        _put_child(
            session, cid, status=status, goal=f"goal {i}",
            error="" if status == "completed" else "agentic_error",
            failure_stage="" if status == "completed" else "agentic_error",
            failure_reason="" if status == "completed" else "adapter boom",
        )
    session._register_parallel_wave(
        "local-wave-mix",
        child_job_ids=ids,
        objective="mixed implement",
    )
    session._sync_parallel_wave_from_children("local-wave-mix")
    parent = session._local_jobs["local-wave-mix"]
    assert parent["status"] == "partial"
    assert parent["review_required"] is True
    assert parent["mixed_terminal"] is True
    assert len(parent["tasks"]) == 8
    assert parent["task_count"] == 8
    assert [t["id"] for t in parent["tasks"]] == ids
    assert parent["tasks"][0]["role"] == "implement"
    assert parent["tasks"][0]["instruction"] == "goal 0"
    receipt = parent["terminal_receipt"]
    assert receipt["status"] == "partial"
    assert "wave partial:" in receipt["summary"]
    assert receipt["child_statuses"]["completed"] == 4
    assert receipt["child_statuses"]["failed"] == 4
    assert receipt["review_required"] is True
    assert len(receipt["children"]) == 8
    assert receipt["children"][4]["failure_stage"] == "agentic_error"
    assert receipt["children"][4]["failure_reason"] == "adapter boom"
    assert parent["tasks"][4]["failure_stage"] == "agentic_error"
    assert parent["tasks"][4]["failure_reason"] == "adapter boom"
    assert list(parent.get("artifacts") or []) == []
    assert _pending(session).get("status") == "partial"


def test_two_child_partial_task_len(tmp_path):
    session = _session(tmp_path)
    _put_child(session, "ok", status="completed")
    _put_child(session, "bad", status="failed", error="boom")
    session._register_parallel_wave(
        "local-wave-2",
        child_job_ids=["ok", "bad"],
        objective="pair",
    )
    session._sync_parallel_wave_from_children("local-wave-2")
    parent = session._local_jobs["local-wave-2"]
    assert parent["status"] == "partial"
    assert len(parent["tasks"]) == 2


def test_all_fail_wave(tmp_path):
    session = _session(tmp_path)
    _put_child(session, "a", status="failed")
    _put_child(session, "b", status="failed")
    session._register_parallel_wave(
        "local-wave-fail",
        child_job_ids=["a", "b"],
        objective="all fail",
    )
    session._sync_parallel_wave_from_children("local-wave-fail")
    parent = session._local_jobs["local-wave-fail"]
    assert parent["status"] == "failed"
    assert parent["review_required"] is False
    assert _pending(session).get("status") == "failed"


def test_timeout_only_none_succeeded(tmp_path):
    session = _session(tmp_path)
    _put_child(session, "t1", status="timeout")
    _put_child(session, "t2", status="timeout")
    session._register_parallel_wave(
        "local-wave-to",
        child_job_ids=["t1", "t2"],
        objective="timeouts",
    )
    session._sync_parallel_wave_from_children("local-wave-to")
    parent = session._local_jobs["local-wave-to"]
    assert parent["status"] == "timed_out"
    assert _pending(session).get("status") == "failed"


def test_analysis_partial_not_review_required(tmp_path):
    session = _session(tmp_path)
    _put_child(session, "a1", status="completed", role="analysis")
    _put_child(session, "a2", status="failed", role="analysis")
    session._register_parallel_wave(
        "local-wave-an",
        child_job_ids=["a1", "a2"],
        objective="analysis wave",
    )
    session._sync_parallel_wave_from_children("local-wave-an")
    parent = session._local_jobs["local-wave-an"]
    assert parent["status"] == "partial"
    assert parent["review_required"] is False


def test_rate_limit_child_stamps_retryable(tmp_path):
    session = _session(tmp_path)
    session._register_local_job("local-rl", "rate limit goal", role="implement")
    session._finish_local_job(
        "local-rl",
        ok=False,
        summary="provider rate limited",
        worker_provenance={
            "failure_stage": "agentic_provider_rate_limited",
            "failure_reason": "too many requests",
            "http_status": 429,
            "retry_after": "2",
            "error": "agentic_provider_rate_limited",
        },
    )
    job = session._local_jobs["local-rl"]
    assert job["retryable"] is True
    assert job["failure_stage"] == "agentic_provider_rate_limited"
    assert job["worker_provenance"]["retryable"] is True
    assert job["http_status"] == 429


def test_generic_agentic_error_is_not_retryable(tmp_path):
    session = _session(tmp_path)
    session._register_local_job("local-ge", "incomplete", role="implement")
    session._finish_local_job(
        "local-ge",
        ok=False,
        summary="Agentic engine error: swarm exited with incomplete tasks",
        worker_provenance={
            "failure_stage": "agentic_error",
            "failure_reason": "",
            "error": "agentic_error",
            "http_status": None,
        },
    )
    job = session._local_jobs["local-ge"]
    assert job["retryable"] is False


def test_timeout_finish_maps_child_status(tmp_path):
    session = _session(tmp_path)
    session._register_local_job("local-to", "slow", role="implement")
    session._finish_local_job(
        "local-to",
        ok=False,
        summary="deadline",
        worker_provenance={
            "failure_stage": "agentic_timeout",
            "failure_reason": "worker exceeded deadline",
            "error": "agentic_timeout",
        },
    )
    job = session._local_jobs["local-to"]
    assert job["status"] == "timeout"
    assert job["retryable"] is True


def test_coordinator_with_no_children_stays_non_complete(tmp_path):
    session = _session(tmp_path)
    wave = session._register_parallel_wave(
        "local-wave-empty",
        child_job_ids=[],
        objective="empty coordinator",
    )
    session._sync_parallel_wave_from_children("local-wave-empty")
    parent = session._local_jobs["local-wave-empty"]
    assert parent["status"] != "completed"
    assert parent["status"] == "running"
    assert parent.get("terminal_receipt") is None
    assert wave["child_count"] == 0


def test_retryable_timeout_wave_relaunches_once(tmp_path, monkeypatch):
    session = _session(tmp_path)
    _put_child(
        session, "retry-me", status="timeout", goal="retry goal",
        retryable=True, retry_count=0, cwd=str(tmp_path),
    )
    launched = []

    def _fake_submit(fn, *args, **kwargs):
        launched.append((fn, args))
        return True

    monkeypatch.setattr(session, "_submit_swarm", _fake_submit)
    session._register_parallel_wave(
        "local-wave-retry",
        child_job_ids=["retry-me"],
        objective="retry",
    )
    session._note_parallel_child_receipt("retry-me")
    child = session._local_jobs["retry-me"]
    assert child["retry_count"] == 1
    assert child["status"] == "queued"
    parent = session._local_jobs["local-wave-retry"]
    assert parent["status"] == "running"
    assert parent.get("wave_auto_retry_attempted") is True
    assert launched
    assert launched[0][1][0] == "retry-me"


def test_file_overlap_skips_retry(tmp_path, monkeypatch):
    session = _session(tmp_path)
    _put_child(
        session, "ok", status="completed", goal="ok",
        applied=True, files=["src/a.py"],
    )
    _put_child(
        session, "bad", status="timeout", goal="bad",
        retryable=True, retry_count=0, files=["src/a.py"],
    )
    launched = []
    monkeypatch.setattr(session, "_submit_swarm", lambda *a, **k: launched.append(a) or True)
    session._register_parallel_wave(
        "local-wave-overlap",
        child_job_ids=["ok", "bad"],
        objective="overlap",
    )
    session._note_parallel_child_receipt("bad")
    assert launched == []
    assert session._local_jobs["bad"]["status"] == "timeout"
    assert session._local_jobs["local-wave-overlap"]["status"] == "partial"


def test_retry_launch_failure_settles_child(tmp_path, monkeypatch):
    session = _session(tmp_path)
    _put_child(
        session, "retry-me", status="timeout", goal="retry goal",
        retryable=True, retry_count=0, cwd=str(tmp_path),
    )

    def _boom(*_a, **_k):
        raise RuntimeError("pool full")

    monkeypatch.setattr(session, "_submit_swarm", _boom)
    session._register_parallel_wave(
        "local-wave-retry-fail",
        child_job_ids=["retry-me"],
        objective="retry",
    )
    session._note_parallel_child_receipt("retry-me")
    child = session._local_jobs["retry-me"]
    assert child["status"] == "failed"
    assert child["failure_stage"] == "retry_launch"
    assert "pool full" in child["failure_reason"]
    parent = session._local_jobs["local-wave-retry-fail"]
    assert parent["status"] == "failed"


def test_enrich_uses_summary_when_finish_reason_empty():
    from harness.conversation_jobs import _enrich_worker_provenance
    from harness.worker import WorkerResult

    res = WorkerResult(
        ok=False,
        error="agentic_error",
        summary=(
            "Agentic engine error: swarm exited with incomplete tasks\n"
            "events: worker.tool_error\n"
            "tasks: implement=failed"
        ),
        finish_reason="",
    )
    prov = _enrich_worker_provenance({"error": "agentic_error"}, res)
    assert prov["failure_stage"] == "agentic_error"
    assert "events: worker.tool_error" in prov["failure_reason"]
    assert prov["retryable"] is False
