"""Bounded nested worker action sanitization + local-job persistence."""
from __future__ import annotations

import copy
import json
import os

from harness.config import HarnessConfig
from harness.conversation import ConvEvent, ConversationalSession
from harness.job_actions import (
    MAX_ACTION_ERROR_CHARS,
    MAX_ACTION_GOAL_CHARS,
    MAX_ACTION_ID_CHARS,
    MAX_ACTION_KIND_CHARS,
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


def test_post_terminal_upsert_completed_parent_settles_complete(tmp_path):
    sess = ConversationalSession(HarnessConfig(state_dir=str(tmp_path)))
    sess._register_local_job("local-term1", "do work")
    sess._upsert_local_job_action(
        "local-term1",
        ConvEvent("action_start", {"id": "t1", "kind": "read_file", "goal": "x.py"}),
    )
    sess._finish_local_job("local-term1", ok=True, summary="done")
    assert sess._local_jobs["local-term1"]["status"] == "completed"
    # Late progressive callback after successful finish must not leave a spinner
    # and must not paint red — match the completed parent outcome.
    sess._upsert_local_job_action(
        "local-term1",
        ConvEvent("action_start", {"id": "late", "kind": "write_file", "goal": "y.py"}),
    )
    actions = sess._local_jobs["local-term1"]["actions"]
    assert all(a["status"] != "running" for a in actions)
    late = next(a for a in actions if a["action_id"] == "late")
    assert late["status"] == "complete"
    assert not late.get("error")


def test_post_terminal_upsert_failed_parent_settles_failed(tmp_path):
    sess = ConversationalSession(HarnessConfig(state_dir=str(tmp_path)))
    sess._register_local_job("local-fail1", "do work")
    sess._finish_local_job("local-fail1", ok=False, summary="boom", status="failed")
    assert sess._local_jobs["local-fail1"]["status"] == "failed"
    sess._upsert_local_job_action(
        "local-fail1",
        ConvEvent("action_start", {"id": "late", "kind": "write_file", "goal": "y.py"}),
    )
    actions = sess._local_jobs["local-fail1"]["actions"]
    assert all(a["status"] != "running" for a in actions)
    late = next(a for a in actions if a["action_id"] == "late")
    assert late["status"] == "failed"
    assert "finished" in (late.get("error") or "")


def test_post_terminal_ingest_settles_running(tmp_path):
    sess = ConversationalSession(HarnessConfig(state_dir=str(tmp_path)))
    sess._register_local_job("local-term2", "do work")
    sess._finish_local_job("local-term2", ok=False, summary="boom", status="failed")
    snap = sess._ingest_local_job_events("local-term2", [
        ConvEvent("action_start", {"id": "n1", "kind": "read_file", "goal": "a.py"}),
        ConvEvent("action_result", {"id": "n1", "kind": "read_file", "goal": "a.py"}),
    ])
    assert snap[0]["status"] == "complete"
    # A trailing late start is terminalized to failed for a failed parent.
    snap2 = sess._ingest_local_job_events("local-term2", [
        ConvEvent("action_start", {"id": "n2", "kind": "edit_file", "goal": "b.py"}),
    ])
    assert snap2[-1]["action_id"] == "n2"
    assert snap2[-1]["status"] == "failed"


def test_post_terminal_ingest_completed_parent_late_start_is_complete(tmp_path):
    sess = ConversationalSession(HarnessConfig(state_dir=str(tmp_path)))
    sess._register_local_job("local-term3", "do work")
    sess._finish_local_job("local-term3", ok=True, summary="done")
    snap = sess._ingest_local_job_events("local-term3", [
        ConvEvent("action_start", {"id": "n1", "kind": "edit_file", "goal": "b.py"}),
    ])
    assert snap[-1]["action_id"] == "n1"
    assert snap[-1]["status"] == "complete"
    assert not snap[-1].get("error")


def test_post_terminal_ingest_cancelled_parent_late_start_is_failed(tmp_path):
    sess = ConversationalSession(HarnessConfig(state_dir=str(tmp_path)))
    sess._register_local_job("local-term4", "do work")
    assert sess.cancel_local_job("local-term4") is True
    assert sess._local_jobs["local-term4"]["status"] == "cancelled"
    snap = sess._ingest_local_job_events("local-term4", [
        ConvEvent("action_start", {"id": "n1", "kind": "edit_file", "goal": "b.py"}),
    ])
    assert snap[-1]["action_id"] == "n1"
    assert snap[-1]["status"] == "failed"
    assert snap[-1].get("error") == "cancelled"
    assert all(a["status"] != "running" for a in snap)


def test_parallel_mirror_caps_combined_actions_at_max(tmp_path):
    from harness.job_actions import MAX_JOB_ACTIONS

    sess = ConversationalSession(HarnessConfig(state_dir=str(tmp_path)))
    sess._register_local_job("local-aa", "worker a")
    sess._register_local_job("local-bb", "worker b")
    sess._display_transcript.append({
        "type": "card",
        "id": "parallel-1",
        "kind": "run_parallel",
        "goal": "",
        "result": {"job_id": "local-aa,local-bb", "status": "pending"},
    })
    # Seed more than MAX rows across both workers via upsert + mirror.
    for i in range(MAX_JOB_ACTIONS):
        sess._upsert_local_job_action(
            "local-aa",
            ConvEvent("action_start", {
                "id": f"a{i}", "kind": "read_file", "goal": f"a{i}.py",
            }),
        )
    for i in range(20):
        sess._upsert_local_job_action(
            "local-bb",
            ConvEvent("action_start", {
                "id": f"b{i}", "kind": "write_file", "goal": f"b{i}.py",
            }),
        )
    sess._mirror_local_job_actions_to_display("local-aa")
    sess._mirror_local_job_actions_to_display("local-bb")
    card = sess._display_transcript[0]
    assert len(card["actions"]) <= MAX_JOB_ACTIONS


def test_post_cancel_late_action_start_settles(tmp_path):
    sess = ConversationalSession(HarnessConfig(state_dir=str(tmp_path)))
    sess._register_local_job("local-cancel1", "do work")
    sess._upsert_local_job_action(
        "local-cancel1",
        ConvEvent("action_start", {"id": "t1", "kind": "read_file", "goal": "x.py"}),
    )
    assert sess.cancel_local_job("local-cancel1") is True
    assert sess._local_jobs["local-cancel1"]["status"] == "cancelled"
    sess._upsert_local_job_action(
        "local-cancel1",
        ConvEvent("action_start", {"id": "late", "kind": "edit_file", "goal": "z.py"}),
    )
    actions = sess._local_jobs["local-cancel1"]["actions"]
    assert all(a["status"] != "running" for a in actions)
    late = next(a for a in actions if a["action_id"] == "late")
    assert late["status"] == "failed"
    assert late.get("error") == "cancelled"


def test_upsert_late_duration_keeps_failed_status_and_error():
    """Failed rows may accept late duration_ms without status/error regression."""
    rows = [
        sanitize_action_row(
            action_id="a1",
            kind="read_file",
            goal="x.py",
            status="failed",
            error="boom",
        )
    ]
    assert rows[0] is not None
    late = sanitize_action_row(
        action_id="a1",
        kind="read_file",
        goal="x.py",
        status="complete",
        duration_ms=42,
        error="",
    )
    assert late is not None
    out = upsert_action_row(rows, late)
    assert out[0]["status"] == "failed"
    assert out[0]["error"] == "boom"
    assert out[0]["duration_ms"] == 42


def test_sanitize_bounds_kind_goal_action_id():
    row = sanitize_action_row(
        action_id="i" * (MAX_ACTION_ID_CHARS + 20),
        kind="k" * (MAX_ACTION_KIND_CHARS + 20),
        goal="g" * (MAX_ACTION_GOAL_CHARS + 20),
        status="running",
    )
    assert row is not None
    assert len(row["action_id"]) <= MAX_ACTION_ID_CHARS
    assert len(row["kind"]) <= MAX_ACTION_KIND_CHARS
    assert len(row["goal"]) <= MAX_ACTION_GOAL_CHARS


def test_assistant_done_settles_missing_action_result_cards(tmp_path, monkeypatch):
    """Turn finalization must settle orphan result:null display cards."""
    sess = ConversationalSession(HarnessConfig(state_dir=str(tmp_path)))

    def _fake_send_locked(*_a, **_k):
        yield ConvEvent("action_start", {
            "id": "a-orphan",
            "kind": "read_file",
            "goal": "missing.py",
        })
        yield ConvEvent("assistant_done", {"text": "done"})

    monkeypatch.setattr(sess, "_send_locked", _fake_send_locked)
    list(sess.send("hi"))
    card = next(
        c for c in sess._display_transcript
        if isinstance(c, dict) and c.get("id") == "a-orphan"
    )
    assert card.get("result") is not None
    assert card["result"].get("error") == "missing action_result"
    assert card["result"].get("status") == "interrupted"
