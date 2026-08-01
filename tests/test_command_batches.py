"""Wave 3: durable command-batch supervisor on the Wave 2 command-job seam.

Covers partial batch failure, sibling preservation under parent cancel,
duplicate replay (completed children reused), bounded concurrency,
stop-before-start, and no provider-swarm misclassification.
"""
from __future__ import annotations

import json
import os
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from harness.command_batches import (
    COMMAND_BATCH_ADAPTER,
    COMMAND_BATCH_KIND,
    COMMAND_BATCH_ROLE,
    MAX_COMMAND_BATCH_SIZE,
    cancel_command_batch,
    find_command_batch_by_action,
    lookup_command_batch,
    normalize_batch_commands,
    project_command_batch_fields,
    start_command_batch,
)
from harness.command_jobs import command_fingerprint, lookup_command_job
from harness.local_job_swarm_view import project_local_job_for_swarm_live
from harness.local_jobs import LocalJobsMixin
from harness.pilot import PilotAction, parse_tool_calls
from harness.send_loop_phases import dispatch_local_action


class _Session(LocalJobsMixin):
    """Minimal LocalJobsMixin host for command-batch tests."""

    def __init__(self, state_dir, repo, session_id="sess-batch", max_workers=2):
        self._local_jobs = {}
        self._local_jobs_lock = threading.RLock()
        self._local_job_cancels = {}
        self._local_jobs_path = os.path.join(state_dir, "swarm_local_jobs.json")
        self.harness_session_id = session_id
        self._state_dir_or_tempdir = state_dir
        self.state_dir = state_dir
        self.config = SimpleNamespace(
            repo=repo, driver="stub-oracle-v2", max_workers=max_workers,
        )
        self._auto_mode = False
        self._auto_command_guard = False
        self._cancel = None
        self._append_action_result = MagicMock()
        self._submit_swarm = MagicMock(return_value=True)
        self._resource_pressure_admit = MagicMock(return_value=True)

    def live_local_jobs(self):
        with self._local_jobs_lock:
            return [dict(j) for j in self._local_jobs.values()]


@pytest.fixture
def session(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    return _Session(str(tmp_path), str(repo)), str(tmp_path), str(repo)


def _wait_batch_terminal(sess, batch_id, timeout=4.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = lookup_command_batch(sess, batch_id)
        if job and job.get("status") in (
            "completed", "failed", "cancelled", "timeout", "truncated",
        ):
            return job
        time.sleep(0.02)
    return lookup_command_batch(sess, batch_id)


def _wait_child_terminal(sess, job_id, timeout=4.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = lookup_command_job(sess, job_id)
        if job and job.get("status") in (
            "completed", "failed", "cancelled", "timeout", "truncated",
        ):
            return job
        time.sleep(0.02)
    return lookup_command_job(sess, job_id)


def test_normalize_batch_commands_caps_at_six():
    cmds = [f"echo {i}" for i in range(MAX_COMMAND_BATCH_SIZE)]
    assert len(normalize_batch_commands(cmds)) == MAX_COMMAND_BATCH_SIZE
    with pytest.raises(ValueError):
        normalize_batch_commands(cmds + ["echo overflow"])


def test_parse_tool_calls_run_command_batch_opt_in():
    actions = parse_tool_calls([{
        "id": "t1",
        "function": {
            "name": "run_command_batch",
            "arguments": json.dumps({
                "commands": ["echo a", "echo b"],
                "max_concurrency": 2,
            }),
        },
    }])
    assert len(actions) == 1
    assert actions[0].kind == "run_command_batch"
    assert actions[0].commands == ["echo a", "echo b"]
    assert actions[0].max_concurrency == 2


def test_six_command_validation_batch_mixed_terminals(session):
    sess, state_dir, repo = session
    commands = [f"echo cmd-{i}" for i in range(6)]

    def _fake_run(command, cwd=None, timeout=None, cancel_event=None):
        # Fail the third command; succeed the rest.
        if "cmd-2" in command:
            return ("boom\n", 1, "error")
        return (f"ok:{command}\n", 0, "ok")

    with patch(
        "harness.command_policy.run_cancellable",
        side_effect=_fake_run,
    ):
        receipt = start_command_batch(sess, commands, "a-six")
        batch_id = receipt["batch_id"]
        assert receipt["child_count"] == 6
        assert len(receipt["child_job_ids"]) == 6
        job = _wait_batch_terminal(sess, batch_id)

    assert job is not None
    assert job["status"] == "failed"  # mixed with a failure
    assert job["mixed_terminal"] is True
    statuses = {
        c["job_id"]: lookup_command_job(sess, c["job_id"])["status"]
        for c in job["children"]
    }
    assert sum(1 for s in statuses.values() if s == "completed") == 5
    assert sum(1 for s in statuses.values() if s == "failed") == 1
    # Aggregate does not own child stdout — each child has its own receipt.
    for cid, st in statuses.items():
        child = lookup_command_job(sess, cid)
        assert child["terminal_receipt"]["status"] == st
        assert child.get("batch_id") == batch_id


def test_partial_batch_failure_preserves_sibling_receipts(session):
    sess, state_dir, repo = session
    commands = ["echo ok-a", "echo fail-b", "echo ok-c"]

    def _fake_run(command, cwd=None, timeout=None, cancel_event=None):
        if "fail-b" in command:
            return ("fail\n", 2, "error")
        return ("ok\n", 0, "ok")

    with patch(
        "harness.command_policy.run_cancellable",
        side_effect=_fake_run,
    ):
        receipt = start_command_batch(sess, commands, "a-partial")
        batch = _wait_batch_terminal(sess, receipt["batch_id"])

    assert batch["status"] == "failed"
    completed = [
        lookup_command_job(sess, cid)
        for cid in batch["child_job_ids"]
        if lookup_command_job(sess, cid)["status"] == "completed"
    ]
    assert len(completed) == 2
    for child in completed:
        assert child["terminal_receipt"]["status"] == "completed"
        assert child["exit_code"] == 0


def test_parent_cancel_preserves_completed_siblings(session):
    sess, state_dir, repo = session
    started = threading.Event()
    release = threading.Event()

    def _fake_run(command, cwd=None, timeout=None, cancel_event=None):
        if "slow" in command:
            started.set()
            # Cooperative cancel poll.
            deadline = time.time() + 2.0
            while time.time() < deadline:
                if cancel_event is not None and cancel_event.is_set():
                    return ("", -1, "cancelled")
                if release.is_set():
                    break
                time.sleep(0.02)
            return ("slow-done\n", 0, "ok")
        return ("fast\n", 0, "ok")

    with patch(
        "harness.command_policy.run_cancellable",
        side_effect=_fake_run,
    ):
        receipt = start_command_batch(
            sess,
            ["echo fast", "sleep-or-slow slow"],
            "a-cancel",
            max_concurrency=1,
        )
        batch_id = receipt["batch_id"]
        # Wait until first child completes and second is in flight.
        deadline = time.time() + 3.0
        first_id = receipt["child_job_ids"][0]
        while time.time() < deadline:
            first = lookup_command_job(sess, first_id)
            if first and first.get("status") == "completed" and started.is_set():
                break
            time.sleep(0.02)
        assert lookup_command_job(sess, first_id)["status"] == "completed"

        cancel_command_batch(sess, batch_id)
        release.set()
        second_id = receipt["child_job_ids"][1]
        # Checkpointed child settles cooperatively — wait for its receipt.
        second = _wait_child_terminal(sess, second_id)
        batch = _wait_batch_terminal(sess, batch_id)

    first = lookup_command_job(sess, first_id)
    assert first["status"] == "completed"
    assert first["terminal_receipt"]["status"] == "completed"
    # Completed sibling was not discarded by parent cancel.
    assert first.get("output") or first.get("terminal_receipt")
    assert second["status"] == "cancelled"
    assert batch["status"] == "cancelled"
    assert batch["mixed_terminal"] is True


def test_duplicate_replay_reuses_completed_children(session):
    sess, state_dir, repo = session
    calls = []

    def _fake_run(command, cwd=None, timeout=None, cancel_event=None):
        calls.append(command)
        if "fail-once" in command and calls.count(command) == 1:
            return ("fail\n", 1, "error")
        return ("ok\n", 0, "ok")

    with patch(
        "harness.command_policy.run_cancellable",
        side_effect=_fake_run,
    ):
        first = start_command_batch(
            sess, ["echo keep", "echo fail-once"], "a-replay",
        )
        batch_id = first["batch_id"]
        _wait_batch_terminal(sess, batch_id)
        keep_id = first["child_job_ids"][0]
        assert lookup_command_job(sess, keep_id)["status"] == "completed"
        calls_after_first = list(calls)

        second = start_command_batch(
            sess, ["echo keep", "echo fail-once"], "a-replay",
        )
        assert second.get("replayed") is True
        assert second["batch_id"] == batch_id
        # Completed child id reused — not a new job row.
        assert second["child_job_ids"][0] == keep_id
        batch = _wait_batch_terminal(sess, batch_id)

    assert lookup_command_job(sess, keep_id)["status"] == "completed"
    # "echo keep" must not have been executed again on replay.
    keep_runs = [c for c in calls if c == "echo keep"]
    assert len(keep_runs) == 1
    assert keep_runs == [c for c in calls_after_first if c == "echo keep"]
    # Failed fingerprint was restarted and can complete.
    fail_child = lookup_command_job(sess, batch["child_job_ids"][1])
    assert fail_child["status"] == "completed"
    assert fail_child["command_fingerprint"] == command_fingerprint("echo fail-once")


def test_bounded_concurrency_respects_max_workers(session):
    sess, state_dir, repo = session
    sess.config.max_workers = 2
    active = 0
    peak = 0
    lock = threading.Lock()

    def _fake_run(command, cwd=None, timeout=None, cancel_event=None):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.08)
        with lock:
            active -= 1
        return ("ok\n", 0, "ok")

    with patch(
        "harness.command_policy.run_cancellable",
        side_effect=_fake_run,
    ):
        receipt = start_command_batch(
            sess,
            [f"echo c-{i}" for i in range(4)],
            "a-bound",
            max_concurrency=2,
        )
        assert receipt["max_concurrency"] == 2
        _wait_batch_terminal(sess, receipt["batch_id"])

    assert peak <= 2
    assert peak >= 1
    sess._submit_swarm.assert_not_called()


def test_stop_before_start_leaves_honest_terminal_states(session):
    sess, state_dir, repo = session
    gate = threading.Event()

    def _fake_run(command, cwd=None, timeout=None, cancel_event=None):
        gate.wait(timeout=2.0)
        if cancel_event is not None and cancel_event.is_set():
            return ("", -1, "cancelled")
        return ("ok\n", 0, "ok")

    with patch(
        "harness.command_policy.run_cancellable",
        side_effect=_fake_run,
    ):
        # Concurrency 1 so later children stay registered while first runs.
        receipt = start_command_batch(
            sess,
            ["echo first", "echo second", "echo third"],
            "a-stop",
            max_concurrency=1,
        )
        batch_id = receipt["batch_id"]
        # Cancel immediately — later children should not start.
        cancel_command_batch(sess, batch_id)
        gate.set()
        batch = _wait_batch_terminal(sess, batch_id)

    statuses = [
        lookup_command_job(sess, cid)["status"]
        for cid in batch["child_job_ids"]
    ]
    # At least the later children must be cancelled (stop-before-start).
    assert statuses.count("cancelled") >= 2
    for cid in batch["child_job_ids"]:
        child = lookup_command_job(sess, cid)
        assert child["status"] in ("completed", "cancelled", "failed")
        assert child.get("terminal_receipt") is not None
        assert child["terminal_receipt"]["status"] == child["status"]


def test_checkpointed_batch_child_cancel_keeps_partial_output(session):
    """Checkpointed children coop-cancel; unlaunched stay stop-before-start."""
    sess, state_dir, repo = session
    started = threading.Event()
    release = threading.Event()
    partial = "batch-partial\n"

    def _fake_run(command, cwd=None, timeout=None, cancel_event=None):
        if "hold" in command:
            started.set()
            deadline = time.time() + 3.0
            while time.time() < deadline:
                if cancel_event is not None and cancel_event.is_set():
                    break
                time.sleep(0.02)
            # Hold after observing cancel so the test can assert mid-state.
            release.wait(timeout=2.0)
            return (partial + "\n\n[interrupted by user]", 130, "cancelled")
        return ("ok\n", 0, "ok")

    with patch(
        "harness.command_policy.run_cancellable",
        side_effect=_fake_run,
    ):
        receipt = start_command_batch(
            sess,
            ["echo hold-me", "echo later-a", "echo later-b"],
            "a-coop-batch",
            max_concurrency=1,
        )
        batch_id = receipt["batch_id"]
        first_id = receipt["child_job_ids"][0]
        assert started.wait(timeout=2.0)
        # First child must be launch-checkpointed before parent cancel.
        deadline = time.time() + 2.0
        while time.time() < deadline:
            first = lookup_command_job(sess, first_id)
            if first and isinstance(first.get("launch_checkpoint"), dict):
                break
            time.sleep(0.02)
        assert isinstance(
            lookup_command_job(sess, first_id).get("launch_checkpoint"), dict
        )

        cancel_command_batch(sess, batch_id)
        # Unlaunched siblings finalize immediately; checkpointed one stays open
        # until the worker returns partial output.
        mid_first = lookup_command_job(sess, first_id)
        assert mid_first.get("terminal_receipt") is None
        assert mid_first["status"] in ("registered", "running")
        for cid in receipt["child_job_ids"][1:]:
            sibling = lookup_command_job(sess, cid)
            assert sibling["status"] == "cancelled"
            assert sibling["terminal_receipt"]["status"] == "cancelled"
            assert (sibling.get("output") or "") == ""

        release.set()
        first = _wait_child_terminal(sess, first_id)
        batch = _wait_batch_terminal(sess, batch_id)

    assert first["status"] == "cancelled"
    assert "batch-partial" in (first.get("output") or "")
    assert batch["status"] == "cancelled"


def test_projection_is_command_batch_not_provider_swarm(session):
    sess, state_dir, repo = session
    with patch(
        "harness.command_policy.run_cancellable",
        return_value=("ok\n", 0, "ok"),
    ):
        receipt = start_command_batch(sess, ["echo a", "echo b"], "a-proj")
        batch = _wait_batch_terminal(sess, receipt["batch_id"])

    row = project_local_job_for_swarm_live(batch)
    assert row["role"] == COMMAND_BATCH_ROLE
    assert row["adapter"] == COMMAND_BATCH_ADAPTER
    assert row["job_kind"] == COMMAND_BATCH_KIND
    assert row["accounting_owned"] is True
    assert row["accounting_scope"] == "marionette"
    assert row["source"] == "harness"
    assert "command" not in row
    assert isinstance(row.get("children"), list)
    for child in row["children"]:
        assert "command" not in child
        assert child.get("command_fingerprint")
    fields = project_command_batch_fields(batch)
    assert "command" not in fields
    # Children projected as command role, not agentic swarm workers.
    for cid in batch["child_job_ids"]:
        child_row = project_local_job_for_swarm_live(lookup_command_job(sess, cid))
        assert child_row["role"] == "command"
        assert child_row["adapter"] == "command"
        assert child_row["job_kind"] == "run_command"


def test_dispatch_returns_pending_batch_receipt(session):
    sess, state_dir, repo = session
    act = PilotAction(
        kind="run_command_batch",
        commands=["echo x", "echo y"],
        max_concurrency=2,
    )
    with patch(
        "harness.command_policy.run_cancellable",
        return_value=("ok\n", 0, "ok"),
    ):
        events = list(dispatch_local_action(sess, act, "a-dispatch", True, []))
        data = events[0].data
        assert data["status"] == "pending"
        assert data["kind"] == COMMAND_BATCH_KIND
        assert data["adapter"] == COMMAND_BATCH_ADAPTER
        assert data["child_count"] == 2
        assert data["mode"] == "batch"
        _wait_batch_terminal(sess, data["batch_id"])

    found = find_command_batch_by_action(sess, "a-dispatch")
    assert found is not None
    assert found["status"] == "completed"


def test_run_parallel_semantics_untouched_by_command_batch():
    # Structural: command batch is a distinct kind and must not alias run_parallel.
    act = PilotAction(kind="run_command_batch", commands=["echo 1", "echo 2"])
    assert act.kind != "run_parallel"
    assert act.goals == []
    with pytest.raises(Exception):
        PilotAction(kind="run_parallel", goals=[]).validate()
