"""Swarm/session history must persist across a backend restart.

Regression for a real defect: with no HARNESS_STATE_DIR set, config.state_dir was
left blank, so the pilot and session each fell back to their OWN tempfile.mkdtemp()
-- a fresh throwaway dir every backend launch. Swarm history (swarm_local_jobs.json),
transcripts, and the sqlite job store landed in a temp dir nothing ever read again,
so the Swarm Tracker showed "No swarm jobs yet" after every close/reopen. The server
now anchors state_dir to a stable per-install dir when none is provided.

This test verifies the persistence CONTRACT at the layer we control (a stable
state_dir makes local-job history survive a fresh session on the same dir),
without importing the server module (which binds real ports / global state).
"""
import os
import tempfile

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession


def test_history_survives_restart_with_stable_state_dir():
    stable = tempfile.mkdtemp(prefix="pmh-stable-")
    first = ConversationalSession(HarnessConfig(state_dir=stable, driver="claude-sonnet-4-5"))
    first._register_local_job("job-A", "audit the platform")
    first._finish_local_job("job-A", ok=True, summary="done", tokens=120000)
    first._register_local_job("job-B", "fix the parser")  # left running

    # Fresh process on the SAME stable dir = a backend restart.
    second = ConversationalSession(HarnessConfig(state_dir=stable, driver="claude-sonnet-4-5"))
    reloaded = {j["id"]: j for j in second.live_local_jobs()}
    assert "job-A" in reloaded and "job-B" in reloaded, "history must reload after restart"
    assert reloaded["job-A"]["status"] in ("completed", "done")
    # A job left running when the old process died must not show a ghost spinner.
    assert reloaded["job-B"]["status"] in ("cancelled", "interrupted", "failed")


def test_ephemeral_dirs_do_not_share_history():
    # Sanity: two DIFFERENT dirs (the old broken behavior) do NOT share history --
    # which is exactly why the stable-dir anchor is required for persistence.
    a = tempfile.mkdtemp(prefix="pmh-a-")
    b = tempfile.mkdtemp(prefix="pmh-b-")
    s1 = ConversationalSession(HarnessConfig(state_dir=a, driver="claude-sonnet-4-5"))
    s1._register_local_job("only-in-a", "x")
    s2 = ConversationalSession(HarnessConfig(state_dir=b, driver="claude-sonnet-4-5"))
    assert all(j["id"] != "only-in-a" for j in s2.live_local_jobs())
