"""Wave 4: command-job launch checkpoints, restart recovery, and reattach.

Covers launch-before-process ordering, exactly-one terminal receipt (first
wins), late/duplicate callbacks, restart rehydration from durable facts, and
stream-checkpoint helpers. SSE/renderer loss must never reopen a terminal
child or discard completed siblings.
"""
from __future__ import annotations

import json
import os
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from harness.api.streams import should_force_transcript_checkpoint
from harness.command_batches import start_command_batch
from harness.command_jobs import (
    COMMAND_TERMINAL_STATES,
    command_job_recovery_state,
    launch_registered_command_job,
    lookup_command_job,
    start_background_run_command,
)
from harness.local_jobs import LocalJobsMixin
from harness.pilot import PilotAction


class _Session(LocalJobsMixin):
    """Minimal LocalJobsMixin host (no ConversationalSession)."""

    def __init__(self, state_dir, repo, session_id="sess-reattach"):
        self._local_jobs = {}
        self._local_jobs_lock = threading.RLock()
        self._local_job_cancels = {}
        self._local_jobs_path = os.path.join(state_dir, "swarm_local_jobs.json")
        self.harness_session_id = session_id
        self._state_dir_or_tempdir = state_dir
        self.state_dir = state_dir
        self.config = SimpleNamespace(repo=repo, driver="stub-oracle-v2")
        self._auto_mode = False
        self._auto_command_guard = False
        self._cancel = None
        self._append_action_result = MagicMock()


@pytest.fixture
def session(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    return _Session(str(tmp_path), str(repo)), str(tmp_path), str(repo)


def _wait_terminal(sess, job_id, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = sess.get_local_job(job_id)
        if job and job.get("status") in COMMAND_TERMINAL_STATES:
            return job
        time.sleep(0.02)
    return sess.get_local_job(job_id)


def test_launch_checkpoint_persisted_before_thread_start(session):
    sess, state_dir, repo = session
    act = PilotAction(kind="run_command", command="echo ckpt", background=True)
    order = []

    real_checkpoint = sess._checkpoint_command_job_launch

    def _wrap_checkpoint(job_id):
        order.append(("checkpoint", job_id))
        ok = real_checkpoint(job_id)
        # Durable fact must be on disk before any worker thread starts.
        on_disk = json.loads(
            open(sess._local_jobs_path, encoding="utf-8").read()
        )
        row = next(j for j in on_disk["jobs"] if j.get("id") == job_id)
        assert isinstance(row.get("launch_checkpoint"), dict)
        assert row["launch_checkpoint"]["phase"] == "pre_launch"
        assert row["status"] == "registered"
        return ok

    def _capture_thread(*args, **kwargs):
        order.append(("thread", kwargs.get("args", (None, None))[1]))
        return MagicMock()

    with patch.object(sess, "_checkpoint_command_job_launch", side_effect=_wrap_checkpoint):
        with patch("harness.command_jobs.threading.Thread", side_effect=_capture_thread):
            receipt = start_background_run_command(sess, act, "a-ckpt")

    job_id = receipt["job_id"]
    assert order[0] == ("checkpoint", job_id)
    assert order[1] == ("thread", job_id)
    job = sess.get_local_job(job_id)
    assert job["launch_checkpoint"]["phase"] == "pre_launch"
    assert command_job_recovery_state(job) == "recoverable_running"


def test_finish_is_idempotent_first_terminal_wins(session):
    sess, state_dir, repo = session
    sess._register_command_job(
        "local-cmd-once",
        command="echo once",
        action_id="a-once",
        cwd=repo,
    )
    assert sess._checkpoint_command_job_launch("local-cmd-once") is True
    first = sess._finish_command_job(
        "local-cmd-once",
        status="completed",
        summary="exit 0 · first",
        exit_code=0,
        output="first\n",
    )
    second = sess._finish_command_job(
        "local-cmd-once",
        status="failed",
        summary="late overwrite",
        exit_code=99,
        output="late\n",
    )
    assert first is True
    assert second is False
    job = sess.get_local_job("local-cmd-once")
    assert job["status"] == "completed"
    assert job["terminal_receipt"]["status"] == "completed"
    assert job["terminal_receipt"]["summary"] == "exit 0 · first"
    assert job["exit_code"] == 0
    assert job["output"] == "first\n"


def test_late_worker_result_cannot_reopen_terminal_child(session):
    sess, state_dir, repo = session
    sess._register_command_job(
        "local-cmd-late",
        command="echo late",
        action_id="a-late",
        cwd=repo,
    )
    assert launch_registered_command_job(sess, "local-cmd-late", "echo late", repo) is True
    # Simulate cancel settling the row before the worker returns.
    sess._finish_command_job(
        "local-cmd-late",
        status="cancelled",
        summary="Cancelled by user",
        exit_code=-1,
        output="",
    )
    # Late launch / late finish must both refuse.
    assert launch_registered_command_job(sess, "local-cmd-late", "echo late", repo) is False
    assert sess._finish_command_job(
        "local-cmd-late",
        status="completed",
        summary="late success",
        exit_code=0,
        output="nope\n",
    ) is False
    job = sess.get_local_job("local-cmd-late")
    assert job["status"] == "cancelled"
    assert job["terminal_receipt"]["status"] == "cancelled"
    assert command_job_recovery_state(job) == "terminal"


def test_restart_preserves_terminal_and_heals_unfinished(session):
    sess, state_dir, repo = session
    # Completed child — must survive restart unchanged.
    sess._register_command_job(
        "local-cmd-done",
        command="echo done",
        action_id="a-done",
        cwd=repo,
    )
    sess._checkpoint_command_job_launch("local-cmd-done")
    sess._finish_command_job(
        "local-cmd-done",
        status="completed",
        summary="exit 0 · done",
        exit_code=0,
        output="done\n",
    )
    # Registered never launched — honest cancelled, not rerun.
    sess._register_command_job(
        "local-cmd-unlaunched",
        command="sleep 999",
        action_id="a-un",
        cwd=repo,
    )
    # Launch-checkpointed but still running when process dies.
    sess._register_command_job(
        "local-cmd-inflight",
        command="sleep 1",
        action_id="a-in",
        cwd=repo,
    )
    sess._checkpoint_command_job_launch("local-cmd-inflight")
    sess._mark_command_job_running("local-cmd-inflight")
    sess._persist_local_jobs()

    restarted = _Session(state_dir, repo, session_id="sess-reattach")
    restarted._load_local_jobs()

    done = restarted.get_local_job("local-cmd-done")
    assert done["status"] == "completed"
    assert done["terminal_receipt"]["status"] == "completed"
    assert done["terminal_receipt"]["summary"] == "exit 0 · done"
    assert command_job_recovery_state(done) == "terminal"

    unlaunched = restarted.get_local_job("local-cmd-unlaunched")
    assert unlaunched["status"] == "cancelled"
    assert "before launch" in unlaunched["terminal_receipt"]["summary"].lower()
    assert unlaunched["terminal_receipt"].get("had_launch_checkpoint") is False

    inflight = restarted.get_local_job("local-cmd-inflight")
    assert inflight["status"] == "cancelled"
    assert "restart" in inflight["terminal_receipt"]["summary"].lower()
    assert inflight["terminal_receipt"].get("had_launch_checkpoint") is True
    # Never left as recoverable_running after process death.
    assert command_job_recovery_state(inflight) == "terminal"


def test_stream_loss_does_not_rerun_completed_batch_sibling(session):
    """SSE/renderer disappearance must not discard a completed sibling."""
    sess, state_dir, repo = session
    with patch(
        "harness.command_policy.run_cancellable",
        return_value=("ok\n", 0, "ok"),
    ):
        receipt = start_command_batch(
            sess, ["echo a", "echo b"], "a-stream-loss",
        )
        batch_id = receipt["batch_id"]
        deadline = time.time() + 3.0
        while time.time() < deadline:
            batch = sess.get_local_job(batch_id)
            if batch and batch.get("status") in COMMAND_TERMINAL_STATES:
                break
            time.sleep(0.02)

    child_ids = list(receipt["child_job_ids"])
    for cid in child_ids:
        child = lookup_command_job(sess, cid)
        assert child["status"] == "completed"
        assert child.get("terminal_receipt") is not None
        assert child.get("launch_checkpoint") is not None

    # Simulate renderer/SSE loss + backend restart: reload from disk.
    restarted = _Session(state_dir, repo, session_id="sess-reattach")
    restarted._load_local_jobs()
    for cid in child_ids:
        child = lookup_command_job(restarted, cid)
        assert child["status"] == "completed"
        assert child["terminal_receipt"]["status"] == "completed"
        # Late finish cannot overwrite.
        assert restarted._finish_command_job(
            cid,
            status="failed",
            summary="late after stream loss",
            exit_code=1,
            output="nope\n",
        ) is False
        assert launch_registered_command_job(
            restarted, cid, "echo a", repo,
        ) is False


def test_should_force_transcript_checkpoint_for_command_jobs():
    pending = SimpleNamespace(
        kind="action_result",
        data={"id": "a1", "job_id": "local-cmd-1", "status": "pending"},
    )
    assert should_force_transcript_checkpoint(pending) is True
    start = SimpleNamespace(
        kind="action_start",
        data={"id": "a1", "job_id": "local-cmd-1", "mode": "background"},
    )
    assert should_force_transcript_checkpoint(start) is True
    delta = SimpleNamespace(kind="message_delta", data={"text": "hi"})
    assert should_force_transcript_checkpoint(delta) is False


def test_live_recoverable_running_before_restart(session):
    sess, state_dir, repo = session
    act = PilotAction(kind="run_command", command="echo live", background=True)
    gate = threading.Event()

    def _blocked_run(command, cwd=None, timeout=None, cancel_event=None):
        gate.wait(timeout=2.0)
        return ("live\n", 0, "ok")

    with patch("harness.command_policy.run_cancellable", side_effect=_blocked_run):
        receipt = start_background_run_command(sess, act, "a-live")
        job_id = receipt["job_id"]
        deadline = time.time() + 2.0
        while time.time() < deadline:
            job = sess.get_local_job(job_id)
            if job and job.get("status") == "running":
                break
            time.sleep(0.02)
        job = sess.get_local_job(job_id)
        assert job["status"] == "running"
        assert isinstance(job.get("launch_checkpoint"), dict)
        # Same-process SSE disconnect: still recoverable_running (not healed).
        assert command_job_recovery_state(job) == "recoverable_running"
        gate.set()
        settled = _wait_terminal(sess, job_id)
        assert settled["status"] == "completed"
        assert command_job_recovery_state(settled) == "terminal"
