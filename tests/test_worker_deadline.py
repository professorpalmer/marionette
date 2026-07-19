"""Bounded background edit worker (audit finding #4).

A wedged provider call or a runaway agentic loop must not occupy a _swarm_pool
slot forever. _run_edit_worker_bounded enforces a hard wall-clock deadline: on
expiry it returns None so the caller frees the slot, while success and exceptions
pass through unchanged."""

import tempfile
import threading
import time

import harness.edit_engines as edit_engines
from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.worker import WorkerResult


def _session():
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    return ConversationalSession(cfg)


def test_bounded_worker_returns_none_on_timeout(monkeypatch):
    monkeypatch.setenv("HARNESS_WORKER_DEADLINE_SECONDS", "0.3")
    s = _session()

    started = threading.Event()

    def _slow(*args, **kwargs):
        started.set()
        time.sleep(5)  # far past the deadline
        return WorkerResult(ok=True, summary="too late")

    monkeypatch.setattr(edit_engines, "run_edit_worker", _slow)

    t0 = time.monotonic()
    res = s._run_edit_worker_bounded("do the thing", "")
    elapsed = time.monotonic() - t0

    assert res is None                 # timed out -> caller frees the pool slot
    assert started.is_set()            # the work really started
    assert elapsed < 3                 # returned near the deadline, not after 5s


def test_bounded_worker_passes_through_success(monkeypatch):
    monkeypatch.setenv("HARNESS_WORKER_DEADLINE_SECONDS", "5")
    s = _session()

    sentinel = WorkerResult(ok=True, summary="done", tokens_out=10, tokens_in=3)
    monkeypatch.setattr(edit_engines, "run_edit_worker", lambda *a, **k: sentinel)

    res = s._run_edit_worker_bounded("goal", "native")
    assert res is sentinel


def test_bounded_worker_propagates_exception(monkeypatch):
    monkeypatch.setenv("HARNESS_WORKER_DEADLINE_SECONDS", "5")
    s = _session()

    def _boom(*args, **kwargs):
        raise RuntimeError("engine exploded")

    monkeypatch.setattr(edit_engines, "run_edit_worker", _boom)

    try:
        s._run_edit_worker_bounded("goal", "")
        assert False, "exception should propagate"
    except RuntimeError as e:
        assert "engine exploded" in str(e)


def test_deadline_zero_disables_timeout_but_cancel_still_settles(monkeypatch, tmp_path):
    """HARNESS_WORKER_DEADLINE_SECONDS=0 is an intentional disable; cancel still settles."""
    monkeypatch.setenv("HARNESS_WORKER_DEADLINE_SECONDS", "0")
    from harness.config import HarnessConfig
    from harness.conversation import ConvEvent, ConversationalSession

    sess = ConversationalSession(HarnessConfig(state_dir=str(tmp_path)))
    assert sess._worker_deadline_seconds() == 0.0
    sess._register_local_job("local-dl0", "work")
    sess._upsert_local_job_action(
        "local-dl0",
        ConvEvent("action_start", {"id": "t1", "kind": "read_file", "goal": "a.py"}),
    )
    assert sess.cancel_local_job("local-dl0") is True
    actions = sess._local_jobs["local-dl0"]["actions"]
    assert actions
    assert all(a["status"] != "running" for a in actions)
