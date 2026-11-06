"""Local swarm-job history must persist across a backend restart.

The Swarm Tracker merges durable Puppetmaster store jobs (already persisted to
sqlite) with in-memory provider-worker jobs held in
ConversationalSession._local_jobs. That dict used to die with the process, so
the tracker lost all provider-worker history on every restart. These jobs are
now mirrored to ``{state_dir}/swarm_local_jobs.json`` and reloaded on init, so
history survives a restart and stays attached to the session's directory.

Hermetic: constructs sessions on a temp state_dir, no network, no real workers.
"""
import tempfile

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession


def _session(state_dir: str) -> ConversationalSession:
    return ConversationalSession(HarnessConfig(state_dir=state_dir))


def test_local_job_survives_restart():
    sd = tempfile.mkdtemp()
    first = _session(sd)
    first._register_local_job("job-a", "do a thing", role="implement")
    first._finish_local_job("job-a", ok=True, summary="done", files=["x.py"])

    # A fresh session on the SAME state_dir simulates a backend restart.
    second = _session(sd)
    reloaded = second._local_jobs.get("job-a")
    assert reloaded is not None, "finished job should reload after restart"
    assert reloaded["status"] in ("completed", "done")


def test_cancel_local_job_marks_cancelled_and_persists():
    sd = tempfile.mkdtemp()
    sess = _session(sd)
    sess._register_local_job("job-c", "cancel me")
    assert sess.cancel_local_job("job-c") is True
    assert sess._local_jobs["job-c"]["status"] == "cancelled"
    # Persisted: a fresh session sees the cancelled terminal state.
    again = _session(sd)
    assert again._local_jobs["job-c"]["status"] == "cancelled"


def test_cancel_unknown_job_returns_false():
    sd = tempfile.mkdtemp()
    sess = _session(sd)
    assert sess.cancel_local_job("nope") is False
