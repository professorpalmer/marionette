from __future__ import annotations

from harness.codegraph_inject import working_query, wrap_slice
from harness.environment_fingerprint import (
    format_prior_findings_block,
    normalize_prior_findings,
)
from harness.pilot import PilotAction, from_wire
from harness.task_transaction import TaskTransaction


def test_prior_findings_normalize_and_format():
    assert normalize_prior_findings(None) == []
    assert normalize_prior_findings("  leftover tracker flicker  ") == [
        "leftover tracker flicker"
    ]
    block = format_prior_findings_block(["a", "a", "b"])
    assert "PRIOR FINDINGS" in block
    assert block.count("- a") == 1
    assert "- b" in block


def test_cancel_job_and_job_findings_wire():
    cancel = from_wire("cancel_job", {"job_id": "local-abc"})
    assert cancel.kind == "cancel_job"
    assert cancel.arguments["job_id"] == "local-abc"
    findings = from_wire("job_findings", {"job_id": "job_deadbeef0123"})
    assert findings.kind == "job_findings"
    assert findings.arguments["job_id"] == "job_deadbeef0123"
    try:
        from_wire("cancel_job", {})
        assert False, "expected PilotError"
    except Exception as exc:
        assert "job_id" in str(exc)


def test_job_findings_bulk_and_empty(tmp_path, monkeypatch):
    from tests.test_peek_tools import _session

    session, _state = _session(tmp_path, monkeypatch)
    job_id = "local-findings1"
    session._register_local_job(job_id, goal="audit", role="explore", engine="native")
    session._local_jobs[job_id]["artifacts"] = [
        {"type": "ROUTING", "id": "r1", "headline": "route", "body": "skip"},
        {
            "type": "FINDING",
            "id": "f1",
            "headline": "list_dir symlink",
            "body": "HEAD-" + ("x" * 200) + "-TAIL",
        },
        {"type": "RISK", "id": "k1", "headline": "write restore", "body": "restore"},
    ]
    session._local_jobs[job_id]["status"] = "complete"
    ok, status, text = session._do_job_findings(
        PilotAction(kind="job_findings", arguments={"job_id": job_id})
    )
    assert ok and status == "success"
    assert "FINDING f1" in text
    assert "RISK k1" in text
    assert "ROUTING" not in text
    empty_ok, _, empty = session._do_job_findings(
        PilotAction(kind="job_findings", arguments={"job_id": "local-missing"})
    )
    assert empty_ok
    assert "empty findings" in empty


def test_job_findings_reads_pm_artifact_payload_and_status(tmp_path, monkeypatch):
    # Puppetmaster findings keep their text in payload (claim, mitigation) and
    # their refs in evidence; there is no headline/body attribute. Reading the
    # attributes returned 13 bare "## FINDING <id>" headers, so the pilot could
    # not verify a single claim and reported the bodies empty.
    from dataclasses import replace
    from types import SimpleNamespace

    from puppetmaster.models import Artifact, ArtifactType, JobStatus, Task
    from puppetmaster.store_factory import create_store
    from tests.test_peek_tools import _session

    session, _state = _session(tmp_path, monkeypatch)
    store = create_store("sqlite", tmp_path / "pm", mode="ensure")
    store.init()
    job = store.create_job("audit", origin="marionette", session_id="s")
    store.save_job(replace(job, status=JobStatus.COMPLETE))
    task = Task(job.id, "explore", "audit")
    store.save_task(task)
    store.save_artifact(Artifact(
        job_id=job.id, task_id=task.id, type=ArtifactType.FINDING, created_by="worker",
        payload={"claim": "[High] rm -r -f / evades the rule", "mitigation": "Merge option tokens."},
        evidence=["harness/command_policy.py:316"], confidence=0.9,
    ))
    monkeypatch.setattr(session, "_internal_uri_context", lambda: SimpleNamespace(store=lambda: store))
    ok, status, text = session._do_job_findings(
        PilotAction(kind="job_findings", arguments={"job_id": job.id})
    )
    assert ok and status == "success"
    assert "status=complete" in text
    assert "rm -r -f / evades the rule" in text
    assert "Merge option tokens." in text
    assert "harness/command_policy.py:316" in text


def test_cancel_job_local_event(tmp_path, monkeypatch):
    from tests.test_peek_tools import _session

    session, _state = _session(tmp_path, monkeypatch)
    job_id = "local-cancel1"
    session._register_local_job(job_id, goal="work", role="explore", engine="native")
    session._local_jobs[job_id]["status"] = "running"
    ok, status, text = session._do_cancel_job(
        PilotAction(kind="cancel_job", arguments={"job_id": job_id})
    )
    assert ok and status == "success"
    assert job_id in text
    missing_ok, missing_status, _ = session._do_cancel_job(
        PilotAction(kind="cancel_job", arguments={"job_id": "local-nope"})
    )
    assert not missing_ok
    assert missing_status == "not_found"


def test_codegraph_working_query_includes_touched_files():
    session = type("S", (), {"_task_tx": TaskTransaction(files=["harness/a.py", "webapp/b.ts"])})()
    query = working_query(session, "where is list_dir")
    assert "where is list_dir" in query
    assert "harness/a.py" in query
    wrapped, symbols = wrap_slice("- **list_dir**\n#### ToolDispatchMixin")
    assert "CODEGRAPH HAS ALREADY BEEN QUERIED" in wrapped
    assert "verbatim on-disk" in wrapped
    assert "authoritative starting points" not in wrapped
    assert symbols >= 1


def test_peek_artifact_head_and_tail(tmp_path, monkeypatch):
    from tests.test_peek_tools import _session

    session, _state = _session(tmp_path, monkeypatch)
    job_id = "local-peek-ht"
    session._register_local_job(job_id, goal="peek", role="explore", engine="native")
    body = "HEADTOKEN" + ("m" * 4000) + "TAILTOKEN"
    session._local_jobs[job_id]["artifacts"] = [
        {"type": "finding", "id": "a1", "headline": "h", "body": body},
    ]
    ok, status, text = session._do_peek_artifact(
        PilotAction(
            kind="peek_artifact",
            arguments={"job_id": job_id, "artifact_id": "a1", "max_bytes": 512},
        )
    )
    assert ok and status == "success"
    assert "truncated=true" in text
    assert "HEADTOKEN" in text
    assert "TAILTOKEN" in text
    assert "middle omitted" in text
