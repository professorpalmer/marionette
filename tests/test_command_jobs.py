"""Wave 2: opt-in durable background ``run_command`` jobs.

Covers registration-before-start, terminal persistence, output caps/spill
metadata, restart-safe lookup, accounting ownership, and secret-free command
metadata. Foreground ``run_command`` must remain synchronous and must not
create a local job unless ``background=True`` is explicit.
"""
from __future__ import annotations

import json
import os
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from harness.command_jobs import (
    COMMAND_JOB_ADAPTER,
    COMMAND_JOB_KIND,
    COMMAND_JOB_ROLE,
    build_pending_receipt,
    command_fingerprint,
    is_background_run_command,
    lookup_command_job,
    project_command_job_fields,
    secret_free_command_preview,
    start_background_run_command,
)
from harness.local_job_swarm_view import project_local_job_for_swarm_live
from harness.local_jobs import LocalJobsMixin
from harness.pilot import PilotAction, parse_tool_calls
from harness.send_loop_phases import dispatch_local_action


class _Session(LocalJobsMixin):
    """Minimal LocalJobsMixin host (no ConversationalSession)."""

    def __init__(self, state_dir, repo, session_id="sess-cmd"):
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


def test_background_is_opt_in_never_inferred_from_duration():
    assert is_background_run_command(
        PilotAction(kind="run_command", command="sleep 999", background=False)
    ) is False
    assert is_background_run_command(
        PilotAction(kind="run_command", command="pytest -q")
    ) is False
    assert is_background_run_command(
        PilotAction(kind="run_command", command="echo hi", background=True)
    ) is True
    # Non-run_command kinds never background via this helper.
    assert is_background_run_command(
        PilotAction(kind="read_file", path="x", background=True)
    ) is False


def test_parse_tool_calls_background_flag_and_mode_alias():
    actions = parse_tool_calls([{
        "id": "t1",
        "function": {
            "name": "run_command",
            "arguments": json.dumps({
                "command": "echo hi",
                "background": True,
            }),
        },
    }])
    assert len(actions) == 1
    assert actions[0].background is True

    actions2 = parse_tool_calls([{
        "id": "t2",
        "function": {
            "name": "run_command",
            "arguments": json.dumps({
                "command": "echo hi",
                "mode": "background",
            }),
        },
    }])
    assert actions2[0].background is True

    actions3 = parse_tool_calls([{
        "id": "t3",
        "function": {
            "name": "run_command",
            "arguments": json.dumps({"command": "sleep 999"}),
        },
    }])
    assert actions3[0].background is False


def test_secret_free_command_metadata_redacts_tokens():
    cmd = "curl -H 'Authorization: Bearer sk-abcdef0123456789' https://example.com"
    preview = secret_free_command_preview(cmd)
    assert "sk-abcdef0123456789" not in preview
    assert "REDACTED" in preview
    fp = command_fingerprint(cmd)
    assert len(fp) == 64
    assert fp == command_fingerprint(cmd)
    assert "Bearer" not in fp


def test_register_before_start_and_pending_receipt(session):
    sess, state_dir, repo = session
    act = PilotAction(kind="run_command", command="echo wave2", background=True)
    launched = []

    def _capture_thread(*args, **kwargs):
        # Do not start the real thread — prove register happened first.
        target = kwargs.get("target") or (args[0] if args else None)
        targs = kwargs.get("args") or ()
        launched.append((target, targs))
        return MagicMock()

    with patch("harness.command_jobs.threading.Thread", side_effect=_capture_thread):
        receipt = start_background_run_command(sess, act, "a-1")

    job_id = receipt["job_id"]
    assert job_id.startswith("local-cmd-")
    # Registration persisted before launch.
    assert os.path.isfile(sess._local_jobs_path)
    on_disk = json.loads(
        open(sess._local_jobs_path, encoding="utf-8").read()
    )
    assert any(j.get("id") == job_id for j in on_disk["jobs"])
    row = sess._local_jobs[job_id]
    assert row["status"] == "registered"
    assert row["action_id"] == "a-1"
    assert row["command_fingerprint"] == command_fingerprint("echo wave2")
    assert "command" not in row or row.get("command") in (None, "")
    assert receipt["status"] == "pending"
    assert receipt["session_id"] == "sess-cmd"
    assert receipt["action_id"] == "a-1"
    assert receipt["cwd"] == repo
    assert receipt["started_at"]
    assert receipt["terminal_receipt"] is None
    assert receipt["accounting_owned"] is True
    assert receipt["accounting_scope"] == "marionette"
    assert receipt["source"] == "harness"
    # Thread was scheduled after register with the registered job_id.
    assert launched
    assert launched[0][1][1] == job_id


def test_terminal_persistence_and_restart_safe_lookup(session):
    sess, state_dir, repo = session
    act = PilotAction(kind="run_command", command="echo done", background=True)

    with patch(
        "harness.command_policy.run_cancellable",
        return_value=("done\n", 0, "ok"),
    ):
        receipt = start_background_run_command(sess, act, "a-term")
        job_id = receipt["job_id"]
        # Allow the daemon thread to finish.
        deadline = time.time() + 3.0
        while time.time() < deadline:
            job = sess.get_local_job(job_id)
            if job and job.get("status") in ("completed", "failed", "cancelled"):
                break
            time.sleep(0.02)

    job = lookup_command_job(sess, job_id)
    assert job is not None
    assert job["status"] == "completed"
    assert job["terminal_receipt"]["status"] == "completed"
    assert job["exit_code"] == 0
    assert job["accounting_owned"] is True
    assert job["accounting_scope"] == "marionette"
    assert job["source"] == "harness"

    # Simulate backend restart: new host loads the same path.
    restarted = _Session(state_dir, repo, session_id="sess-cmd")
    restarted._load_local_jobs()
    reloaded = lookup_command_job(restarted, job_id)
    assert reloaded is not None
    assert reloaded["status"] == "completed"
    assert reloaded["terminal_receipt"]["status"] == "completed"
    assert reloaded["command_fingerprint"] == command_fingerprint("echo done")


def test_registered_job_healed_on_restart(session):
    sess, state_dir, repo = session
    row = sess._register_command_job(
        "local-cmd-stale",
        command="sleep 999",
        action_id="a-stale",
        cwd=repo,
    )
    assert row["status"] == "registered"
    sess._persist_local_jobs()

    restarted = _Session(state_dir, repo, session_id="sess-cmd")
    restarted._load_local_jobs()
    healed = restarted.get_local_job("local-cmd-stale")
    assert healed["status"] == "cancelled"
    assert healed["terminal_receipt"]["status"] == "cancelled"
    # Wave 4: registered without launch_checkpoint → cancelled before launch.
    summary = healed["terminal_receipt"]["summary"].lower()
    assert "restart" in summary
    assert "before launch" in summary


def test_output_cap_and_spill_metadata(session):
    sess, state_dir, repo = session
    huge = "X" * (60 * 1024)
    act = PilotAction(kind="run_command", command="yes", background=True)

    with patch(
        "harness.command_policy.run_cancellable",
        return_value=(huge, 0, "ok"),
    ):
        receipt = start_background_run_command(sess, act, "a-spill")
        job_id = receipt["job_id"]
        deadline = time.time() + 3.0
        while time.time() < deadline:
            job = sess.get_local_job(job_id)
            if job and job.get("status") == "completed":
                break
            time.sleep(0.02)

    job = sess.get_local_job(job_id)
    assert job["status"] == "completed"
    assert job["output_chars"] == len(huge)
    assert job.get("spill_uri") or job.get("spill_path")
    assert job["terminal_receipt"]["output_spilled"] is True
    # Inline raw output should not retain the full 60KB when spilled.
    assert len(job.get("output") or "") < len(huge)
    assert len(job.get("output_preview") or "") < len(huge)


def test_projection_is_command_not_provider_swarm(session):
    sess, state_dir, repo = session
    sess._register_command_job(
        "local-cmd-proj",
        command="pytest -q",
        action_id="a-proj",
        cwd=repo,
    )
    sess._finish_command_job(
        "local-cmd-proj",
        status="completed",
        summary="exit 0 · ok",
        exit_code=0,
        output="ok\n",
    )
    job = sess.get_local_job("local-cmd-proj")
    row = project_local_job_for_swarm_live(job)
    assert row["role"] == COMMAND_JOB_ROLE
    assert row["adapter"] == COMMAND_JOB_ADAPTER
    assert row["job_kind"] == COMMAND_JOB_KIND
    assert row["accounting_owned"] is True
    assert row["accounting_scope"] == "marionette"
    assert row["source"] == "harness"
    assert row["command_fingerprint"]
    assert "command" not in row  # raw command never projected
    fields = project_command_job_fields(job)
    assert "command" not in fields


def test_foreground_dispatch_does_not_create_command_job(session):
    sess, state_dir, repo = session
    act = PilotAction(kind="run_command", command="echo fg", background=False)
    host = SimpleNamespace(
        config=SimpleNamespace(repo=repo),
        _do_run_command=MagicMock(
            return_value=(
                True,
                "success",
                {"output": "fg\n", "exit_code": 0, "status": "ok"},
            ),
        ),
        _append_action_result=MagicMock(),
        _register_command_job=MagicMock(),
    )
    events = list(dispatch_local_action(host, act, "a-fg", True, []))
    assert events[0].data["status"] == "ok"
    assert "job_id" not in events[0].data
    host._register_command_job.assert_not_called()
    host._do_run_command.assert_called_once()


def test_background_dispatch_returns_pending_receipt(session):
    sess, state_dir, repo = session
    act = PilotAction(kind="run_command", command="echo bg", background=True)

    with patch(
        "harness.command_policy.run_cancellable",
        return_value=("bg\n", 0, "ok"),
    ):
        events = list(dispatch_local_action(sess, act, "a-bg", True, []))

    assert len(events) == 1
    data = events[0].data
    assert data["status"] == "pending"
    assert data["job_id"].startswith("local-cmd-")
    assert data["action_id"] == "a-bg"
    assert data["command_fingerprint"] == command_fingerprint("echo bg")
    assert data["adapter"] == "command"
    assert data["mode"] == "background"
    assert data["accounting_owned"] is True
    assert data["accounting_scope"] == "marionette"
    assert data["terminal_receipt"] is None
    # Wait for terminal so the suite does not leak a live process.
    deadline = time.time() + 3.0
    while time.time() < deadline:
        job = sess.get_local_job(data["job_id"])
        if job and job.get("status") in ("completed", "failed", "cancelled"):
            break
        time.sleep(0.02)
    receipt = build_pending_receipt(sess.get_local_job(data["job_id"]))
    assert receipt["terminal_receipt"]["status"] == "completed"


def test_background_false_on_non_command_fails_validate():
    with pytest.raises(Exception):
        PilotAction(kind="read_file", path="x.py", background=True).validate()


def test_background_cancel_preserves_partial_output(session):
    """Launched cancel must keep partial stdout (worker owns the receipt)."""
    sess, state_dir, repo = session
    started = threading.Event()
    release = threading.Event()
    partial = "partial-stdout-line\n"

    def _fake_run(command, cwd=None, timeout=None, cancel_event=None):
        started.set()
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                break
            time.sleep(0.02)
        release.wait(timeout=2.0)
        return (partial + "\n\n[interrupted by user]", 130, "cancelled")

    act = PilotAction(kind="run_command", command="echo partial", background=True)
    with patch(
        "harness.command_policy.run_cancellable",
        side_effect=_fake_run,
    ):
        receipt = start_background_run_command(sess, act, "a-partial-cancel")
        job_id = receipt["job_id"]
        assert started.wait(timeout=2.0)
        # Launch checkpoint must exist so cancel is cooperative, not empty-finish.
        assert isinstance(sess.get_local_job(job_id).get("launch_checkpoint"), dict)
        assert sess.cancel_local_job(job_id) is True
        # Must not have emptied the receipt before the worker returns.
        mid = sess.get_local_job(job_id)
        assert mid["status"] in ("registered", "running")
        assert mid.get("terminal_receipt") is None
        release.set()

        deadline = time.time() + 3.0
        while time.time() < deadline:
            job = sess.get_local_job(job_id)
            if job and job.get("status") in ("completed", "failed", "cancelled"):
                break
            time.sleep(0.02)

    job = sess.get_local_job(job_id)
    assert job["status"] == "cancelled"
    assert job["terminal_receipt"]["status"] == "cancelled"
    assert "partial-stdout-line" in (job.get("output") or "")
    # Exactly one terminal receipt — late empty cancel must not have won first.
    assert job["terminal_receipt"].get("summary")
    pub = build_pending_receipt(job, include_output=True)
    assert "partial-stdout-line" in pub.get("output", "")


def test_unlaunched_cancel_is_immediate_empty(session):
    """Registered without launch checkpoint → honest stop-before-start."""
    sess, state_dir, repo = session
    sess._register_command_job(
        "local-cmd-unlaunch",
        command="sleep 999",
        action_id="a-unlaunch",
        cwd=repo,
    )
    assert sess.cancel_local_job("local-cmd-unlaunch") is True
    job = sess.get_local_job("local-cmd-unlaunch")
    assert job["status"] == "cancelled"
    assert job["terminal_receipt"]["status"] == "cancelled"
    assert (job.get("output") or "") == ""


def test_cancel_before_run_cancellable_does_not_execute(session):
    """Cancel after checkpoint but before run_cancellable must not launch."""
    sess, state_dir, repo = session
    from harness.command_jobs import _run_registered_command_job

    sess._register_command_job(
        "local-cmd-pre-run",
        command="echo never",
        action_id="a-pre-run",
        cwd=repo,
    )
    assert sess._checkpoint_command_job_launch("local-cmd-pre-run") is True
    assert sess.cancel_local_job("local-cmd-pre-run") is True

    calls = []

    def _blocked_run(*args, **kwargs):
        calls.append(args)
        return ("should-not-run\n", 0, "ok")

    with patch(
        "harness.command_policy.run_cancellable",
        side_effect=_blocked_run,
    ):
        _run_registered_command_job(sess, "local-cmd-pre-run", "echo never", repo)

    assert calls == []
    job = sess.get_local_job("local-cmd-pre-run")
    assert job["status"] == "cancelled"
    assert job["terminal_receipt"]["status"] == "cancelled"
    assert (job.get("output") or "") == ""


def test_null_command_auto_guard_does_not_fail_open(session):
    """None command must not AttributeError-skip the full-auto danger gate.

    Pre-fix: command.encode threw, except Exception: pass swallowed it, and
    execution continued with command=None (fail-open). Foreground tool_dispatch
    already used ``act.command or ""``; background must match.
    """
    sess, state_dir, repo = session
    from harness.command_jobs import _run_registered_command_job

    sess._auto_mode = True
    sess._auto_command_guard = True
    sess._register_command_job(
        "local-cmd-none",
        command="placeholder",
        action_id="a-none",
        cwd=repo,
    )
    ran = []

    def _track_run(cmd, **kwargs):
        ran.append(cmd)
        return ("", 0, "ok")

    with patch(
        "harness.command_policy.run_cancellable",
        side_effect=_track_run,
    ):
        _run_registered_command_job(sess, "local-cmd-none", None, repo)

    # Gate normalized None -> "" (non-danger) and proceeded; never ran None.
    assert ran == [""]
    job = sess.get_local_job("local-cmd-none")
    assert job["status"] == "completed"


def test_public_receipt_omits_spill_path(session):
    """build_pending_receipt must never expose local filesystem spill_path."""
    sess, state_dir, repo = session
    huge = "Y" * (60 * 1024)
    act = PilotAction(kind="run_command", command="yes", background=True)

    with patch(
        "harness.command_policy.run_cancellable",
        return_value=(huge, 0, "ok"),
    ):
        receipt = start_background_run_command(sess, act, "a-spill-pub")
        job_id = receipt["job_id"]
        deadline = time.time() + 3.0
        while time.time() < deadline:
            job = sess.get_local_job(job_id)
            if job and job.get("status") == "completed":
                break
            time.sleep(0.02)

    job = sess.get_local_job(job_id)
    assert job.get("spill_path") or job.get("spill_uri")
    # Internal row may retain the path for recovery.
    pub = build_pending_receipt(job, include_output=True)
    assert "spill_path" not in pub
    assert pub.get("output_spilled") is True
    assert pub.get("spill_uri") or pub.get("output")
    assert "output_chars" in pub
    # Nested terminal_receipt must also stay path-free.
    term = pub.get("terminal_receipt") or {}
    assert "spill_path" not in term


def test_auto_guard_classify_error_blocks_job(session):
    """When classify_command raises in the full-auto danger gate, the job
    must be marked failed and run_cancellable must NEVER be called (fail-
    closed, not fail-open)."""
    from harness.command_jobs import _run_registered_command_job

    sess, state_dir, repo = session

    sess._auto_mode = True
    sess._auto_command_guard = True
    sess._register_command_job(
        "local-cmd-guard-err",
        command="echo hello",
        action_id="a-guard-err",
        cwd=repo,
    )

    called_run_cancellable = []

    def _track_run(cmd, **kwargs):
        called_run_cancellable.append(cmd)
        return ("", 0, "ok")

    with patch(
        "harness.command_policy.classify_command",
        side_effect=RuntimeError("simulated classify failure"),
    ), patch(
        "harness.command_policy.run_cancellable",
        side_effect=_track_run,
    ):
        _run_registered_command_job(sess, "local-cmd-guard-err", None, repo)

    # Must never have called run_cancellable (fail-closed).
    assert called_run_cancellable == []

    job = sess.get_local_job("local-cmd-guard-err")
    assert job is not None
    assert job["status"] == "failed"
    # _finish_command_job stores the summary in the terminal_receipt (and as the
    # artifact headline), not as a top-level job["summary"] key.
    receipt = job.get("terminal_receipt") or {}
    assert "BLOCKED: auto guard error:" in receipt.get("summary", "")
    assert "simulated classify failure" in receipt.get("summary", "")
    assert job.get("exit_code") == -1
