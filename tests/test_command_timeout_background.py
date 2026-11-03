from __future__ import annotations

"""A background command outlives the foreground limit, and a timeout says so."""

import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

from harness.command_hints import command_timeout_hint
from harness.command_jobs import start_background_run_command
from harness.command_policy import background_command_timeout
from harness.local_jobs import LocalJobsMixin
from harness.pilot import PilotAction
from harness.tool_dispatch import ToolDispatchMixin


def test_background_timeout_is_the_hard_ceiling_by_default():
    assert background_command_timeout({}) == 900
    assert background_command_timeout({"HARNESS_COMMAND_TIMEOUT": "1200"}) == 1200
    assert background_command_timeout({"HARNESS_COMMAND_HARD_CEILING": "60"}) == 120
    assert background_command_timeout({"HARNESS_COMMAND_HARD_CEILING": "0"}) is None


def test_timeout_hint_names_background_only_when_it_runs_longer():
    longer = command_timeout_hint(120, 900)
    assert "background=true (limit 900 s)" in longer
    assert "check them before a rerun" in longer
    assert "background=true (no time limit)" in command_timeout_hint(120, None)
    same = command_timeout_hint(900, 900)
    assert "background" not in same
    assert "Split the work" in same


class _JobSession(LocalJobsMixin):
    def __init__(self, state_dir, repo):
        self._local_jobs = {}
        self._local_jobs_lock = threading.RLock()
        self._local_job_cancels = {}
        self._local_jobs_path = f"{state_dir}/swarm_local_jobs.json"
        self.harness_session_id = "sess-timeout"
        self._state_dir_or_tempdir = state_dir
        self.state_dir = state_dir
        self.config = SimpleNamespace(repo=repo, driver="stub-oracle-v2")
        self._auto_mode = False
        self._auto_command_guard = False
        self._cancel = None


def test_background_job_runs_with_the_background_limit(tmp_path, monkeypatch):
    monkeypatch.delenv("HARNESS_COMMAND_TIMEOUT", raising=False)
    monkeypatch.delenv("HARNESS_COMMAND_HARD_CEILING", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    sess = _JobSession(str(tmp_path), str(repo))
    seen = []

    def fake_run(command, cwd=None, timeout=None, cancel_event=None):
        seen.append(timeout)
        return ("done\n", 0, "ok")

    act = PilotAction(kind="run_command", command="make build", background=True)
    with patch("harness.command_policy.run_cancellable", side_effect=fake_run):
        job_id = start_background_run_command(sess, act, "a-long")["job_id"]
        deadline = time.time() + 3.0
        while time.time() < deadline and not seen:
            time.sleep(0.02)
        while time.time() < deadline:
            if (sess.get_local_job(job_id) or {}).get("status") == "completed":
                break
            time.sleep(0.02)
    assert seen == [900]


def test_foreground_timeout_payload_carries_the_background_hint(tmp_path, monkeypatch):
    monkeypatch.delenv("HARNESS_COMMAND_TIMEOUT", raising=False)
    monkeypatch.delenv("HARNESS_COMMAND_HARD_CEILING", raising=False)
    session = SimpleNamespace(
        config=SimpleNamespace(repo=str(tmp_path), state_dir=""),
        state_dir="",
        harness_session_id="sess-timeout",
        _auto_mode=False,
        _auto_command_guard=False,
        _cancel=None,
    )
    act = PilotAction(kind="run_command", command="make build")
    timed_out = ("partial\n\n[TimeoutExpired after 120 seconds]", -1, "timeout")
    with patch("harness.command_policy.run_cancellable", return_value=timed_out):
        ok, status, val = ToolDispatchMixin._do_run_command(session, act)
    assert ok is False and status == "timeout"
    assert "120 s foreground limit" in val["hint"]
    assert "background=true (limit 900 s)" in val["hint"]


def test_a_clean_exit_gets_no_timeout_hint(tmp_path):
    session = SimpleNamespace(
        config=SimpleNamespace(repo=str(tmp_path), state_dir=""),
        state_dir="",
        harness_session_id="sess-timeout",
        _auto_mode=False,
        _auto_command_guard=False,
        _cancel=None,
    )
    act = PilotAction(kind="run_command", command="echo ok")
    with patch("harness.command_policy.run_cancellable", return_value=("ok\n", 0, "ok")):
        ok, status, val = ToolDispatchMixin._do_run_command(session, act)
    assert ok is True and "hint" not in val
