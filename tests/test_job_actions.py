"""Bounded nested worker action sanitization + local-job persistence."""
from __future__ import annotations

import copy
import json
import os

from harness.config import HarnessConfig
from harness.conversation import ConvEvent, ConversationalSession
from harness.job_actions import (
    MAX_ACTION_ERROR_CHARS,
    ingest_worker_events,
    sanitize_action_row,
    sanitize_actions_list,
    sanitize_worker_event,
    settle_running_actions,
    snapshot_actions,
    upsert_action_row,
)


def test_sanitize_strips_secrets_and_bounds_error():
    huge = "x" * (MAX_ACTION_ERROR_CHARS + 50)
    ev = ConvEvent("action_result", {
        "id": "a1",
        "kind": "run_command",
        "goal": "pytest",
        "error": huge,
        "stdout": "SECRET_OUTPUT",
        "args": ["--token", "abc"],
        "env": {"API_KEY": "nope"},
    })
    row = sanitize_worker_event(ev)
    assert row is not None
    assert row["action_id"] == "a1"
    assert row["kind"] == "run_command"
    assert row["goal"] == "pytest"
    assert row["status"] == "failed"
    assert "SECRET_OUTPUT" not in row.values()
    assert "API_KEY" not in str(row)
    assert len(row["error"]) <= MAX_ACTION_ERROR_CHARS
    assert "stdout" not in row
    assert "args" not in row
    assert "env" not in row


def test_command_is_not_used_as_goal_source():
    ev = ConvEvent("action_start", {
        "id": "c1",
        "kind": "run_command",
        "command": "export API_KEY=secret && curl ...",
    })
    row = sanitize_worker_event(ev)
    assert row is not None
    assert row["goal"] == ""
    assert "secret" not in str(row)
    assert "command" not in row


def test_tampered_stdout_env_command_cannot_survive_sanitize_list():
    dirty = [{
        "action_id": "t1",
        "kind": "read_file",
        "goal": "a.py",
        "status": "complete",
        "stdout": "SECRET",
        "env": {"TOKEN": "x"},
        "command": "cat /etc/passwd",
        "duration_ms": 1,
        "error": "",
    }]
    clean = sanitize_actions_list(dirty)
    assert len(clean) == 1
    assert set(clean[0]) <= {
        "action_id", "kind", "goal", "status", "duration_ms", "error", "worker_id",
    }
    assert "SECRET" not in str(clean)
    assert "TOKEN" not in str(clean)
    assert "passwd" not in str(clean)


def test_ingest_preserves_order_and_updates_status():
    events = [
        ConvEvent("action_start", {"id": "r1", "kind": "read_file", "goal": "a.py"}),
        ConvEvent("action_start", {"id": "w1", "kind": "write_file", "goal": "a.py"}),
        ConvEvent("action_result", {"id": "r1", "kind": "read_file", "goal": "a.py"}),
        ConvEvent("action_start", {"id": "r2", "kind": "read_file", "goal": "b.py"}),
        ConvEvent("action_result", {
            "id": "w1", "kind": "write_file", "goal": "a.py", "error": "boom",
        }),
        ConvEvent("thinking", {"text": "ignore me"}),
    ]
    actions = ingest_worker_events(events)
    assert [a["action_id"] for a in actions] == ["r1", "w1", "r2"]
    assert actions[0]["status"] == "complete"
    assert actions[1]["status"] == "failed"
    assert actions[1]["error"] == "boom"
    assert actions[2]["status"] == "running"
    assert actions[0]["kind"] == "read_file"
    assert actions[1]["kind"] == "write_file"


def test_status_is_monotonic_against_late_start_and_failed_upgrade():
    actions = [sanitize_action_row(
        action_id="a1", kind="read_file", goal="x.py", status="complete",
    )]
    late_start = sanitize_action_row(
        action_id="a1", kind="read_file", goal="x.py", status="running",
    )
    out = upsert_action_row(actions, late_start)
    assert out[0]["status"] == "complete"

    failed = [sanitize_action_row(
        action_id="a2", kind="edit_file", goal="y.py", status="failed", error="nope",
    )]
    late_complete = sanitize_action_row(
        action_id="a2", kind="edit_file", goal="y.py", status="complete",
    )
    out2 = upsert_action_row(failed, late_complete)
    assert out2[0]["status"] == "failed"
    assert out2[0]["error"] == "nope"

    # Terminal result may still settle a running row.
    running = [sanitize_action_row(
        action_id="a3", kind="write_file", goal="z.py", status="running",
    )]
    done = sanitize_action_row(
        action_id="a3", kind="write_file", goal="z.py", status="complete", duration_ms=4,
    )
    out3 = upsert_action_row(running, done)
    assert out3[0]["status"] == "complete"
    assert out3[0]["duration_ms"] == 4


def test_upsert_does_not_blank_kind_goal():
    actions = [sanitize_action_row(
        action_id="a1", kind="read_file", goal="x.py", status="running",
    )]
    sparse = sanitize_action_row(action_id="a1", status="complete", duration_ms=12)
    out = upsert_action_row(actions, sparse)
    assert out[0]["kind"] == "read_file"
    assert out[0]["goal"] == "x.py"
    assert out[0]["status"] == "complete"
    assert out[0]["duration_ms"] == 12


def test_settle_running_actions_marks_failed():
    rows = [
        sanitize_action_row(action_id="a", kind="read_file", goal="a.py", status="running"),
        sanitize_action_row(action_id="b", kind="write_file", goal="b.py", status="complete"),
    ]
    settled = settle_running_actions(rows, reason="job finished")
    assert settled[0]["status"] == "failed"
    assert settled[0]["error"] == "job finished"
    assert settled[1]["status"] == "complete"


def test_local_job_actions_persist_and_deep_copy(tmp_path):
    sd = str(tmp_path)
    first = ConversationalSession(HarnessConfig(state_dir=sd))
    first._register_local_job("local-abcd1234", "implement nested", role="implement")
    first._upsert_local_job_action(
        "local-abcd1234",
        ConvEvent("action_start", {"id": "c1", "kind": "read_file", "goal": "f.py"}),
    )
    first._ingest_local_job_events("local-abcd1234", [
        ConvEvent("action_result", {
            "id": "c1", "kind": "read_file", "goal": "f.py", "duration_ms": 9,
        }),
        ConvEvent("action_start", {"id": "c2", "kind": "write_file", "goal": "f.py"}),
        ConvEvent("action_result", {
            "id": "c2", "kind": "write_file", "goal": "f.py", "error": "nope",
        }),
    ])

    live = first.live_local_jobs()
    job = next(j for j in live if j["id"] == "local-abcd1234")
    assert [a["action_id"] for a in job["actions"]] == ["c1", "c2"]
    assert job["actions"][0]["status"] == "complete"
    assert job["actions"][1]["status"] == "failed"

    # Deep-copy: mutating the live snapshot must not mutate session state.
    job["actions"][0]["goal"] = "MUTATED"
    assert first._local_jobs["local-abcd1234"]["actions"][0]["goal"] == "f.py"

    second = ConversationalSession(HarnessConfig(state_dir=sd))
    reloaded = second._local_jobs["local-abcd1234"]
    assert [a["action_id"] for a in reloaded["actions"]] == ["c1", "c2"]
    assert reloaded["actions"][1]["error"] == "nope"


def test_finish_settles_running_actions(tmp_path):
    sess = ConversationalSession(HarnessConfig(state_dir=str(tmp_path)))
    sess._register_local_job("local-fin1", "do work")
    sess._upsert_local_job_action(
        "local-fin1",
        ConvEvent("action_start", {"id": "t1", "kind": "read_file", "goal": "x.py"}),
    )
    sess._finish_local_job("local-fin1", ok=True, summary="done")
    actions = sess._local_jobs["local-fin1"]["actions"]
    assert actions[0]["status"] == "failed"
    assert "finished" in actions[0]["error"]


def test_reload_heals_running_actions_and_strips_tamper(tmp_path):
    path = os.path.join(str(tmp_path), "swarm_local_jobs.json")
    payload = {
        "jobs": [{
            "id": "local-stale",
            "goal": "g",
            "status": "running",
            "role": "implement",
            "adapter": "native",
            "model": "native",
            "session_id": "",
            "cwd": "",
            "label": "",
            "created_at": 1.0,
            "updated_at": 1.0,
            "task_count": 1,
            "tokens": 0,
            "est_cost_usd": 0.0,
            "artifacts": [],
            "tasks": [{"id": "local-stale-w0", "role": "implement (native)",
                       "instruction": "g", "status": "running", "adapter": "native"}],
            "actions": [{
                "action_id": "r1",
                "kind": "read_file",
                "goal": "a.py",
                "status": "running",
                "stdout": "LEAK",
                "env": {"K": "V"},
                "command": "echo secret",
                "duration_ms": None,
                "error": "",
            }],
        }],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f)

    sess = ConversationalSession(HarnessConfig(state_dir=str(tmp_path)))
    job = sess._local_jobs["local-stale"]
    assert job["status"] == "cancelled"
    assert job["actions"][0]["status"] == "failed"
    assert "restart" in job["actions"][0]["error"]
    assert "stdout" not in job["actions"][0]
    assert "env" not in job["actions"][0]
    assert "command" not in job["actions"][0]
    assert "LEAK" not in str(job["actions"])


def test_progressive_upsert_does_not_mutate_display_transcript(tmp_path):
    sess = ConversationalSession(HarnessConfig(state_dir=str(tmp_path)))
    sess._register_local_job("local-job1", "do work")
    sess._display_transcript.append({
        "type": "card",
        "id": "a1",
        "kind": "run_implement",
        "goal": "do work",
        "result": {"job_id": "local-job1", "status": "pending"},
    })
    sess._upsert_local_job_action(
        "local-job1",
        ConvEvent("action_start", {"id": "t1", "kind": "read_file", "goal": "x.py"}),
    )
    assert "actions" not in sess._display_transcript[0]
    # Safe drain mirror attaches for reload durability.
    sess._mirror_local_job_actions_to_display("local-job1")
    card = sess._display_transcript[0]
    assert card["actions"][0]["action_id"] == "t1"
    assert card["worker_id"] == "local-job1"


def test_snapshot_actions_is_independent():
    rows = [{"action_id": "a", "kind": "read_file", "goal": "g", "status": "running",
             "duration_ms": None, "error": ""}]
    snap = snapshot_actions(rows)
    snap[0]["goal"] = "changed"
    assert rows[0]["goal"] == "g"
    assert copy.deepcopy(rows)[0]["goal"] == "g"
