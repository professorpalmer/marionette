"""Wave 4 + S2: safe-boundary cancel/steer + dual-store interrupt drain.

Proves:
1. BusyControlMixin.interrupt cooperatively drains session jobs through the
   canonical harness+CLI dual-store seam (no stranded actionable jobs).
2. Interrupt never uses unsafe thread killing.
3. Shared job_cancel helpers keep HTTP cancel and interrupt membership aligned.
4. S2 Stop↔steer boundary: queued steers are dropped (not injected into an
   abandoned generator / later unrelated send) with a durable notice.
"""
from __future__ import annotations

import collections
import tempfile
import threading
from types import SimpleNamespace

import pytest

from harness.api.jobs import JobServices, post_swarm_cancel
from harness.busy_control import BusyControlMixin
from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.job_cancel import (
    cancel_job_dual_store,
    drain_job_ids_dual_store,
    mark_store_job_cancelled,
)
from harness.steer_mixin import SteerMixin


class _FakeStore:
    def __init__(self, jobs, *, fail_cancel: bool = False):
        self._jobs = list(jobs)
        self.cancelled: list[str] = []
        self._fail_cancel = fail_cancel
        self._known = {
            (j.get("id") if isinstance(j, dict) else getattr(j, "id", ""))
            for j in jobs
            if (j.get("id") if isinstance(j, dict) else getattr(j, "id", None))
        }
        self.statuses: dict[str, str] = {jid: "running" for jid in self._known}

    def list_jobs(self):
        return list(self._jobs)

    def cancel_job(self, job_id: str):
        if job_id not in self._known:
            raise KeyError(job_id)
        if self._fail_cancel:
            raise RuntimeError("cancel_job unavailable")
        self.cancelled.append(job_id)
        self.statuses[job_id] = "cancelled"

    def update_job_status(self, job_id: str, status: str):
        if job_id not in self._known:
            raise KeyError(job_id)
        self.cancelled.append(job_id)
        self.statuses[job_id] = status


class _InterruptHarness(BusyControlMixin, SteerMixin):
    """Minimal host for BusyControlMixin.interrupt dual-store + steer tests."""

    def __init__(self, *, harness_store, repo: str = "/repo"):
        self._cancel = threading.Event()
        self._interrupt_requested = False
        self._stop_holds_idle = False
        self._steer_boundary_drop_on_acquire = False
        self._interrupted_swarms = False
        self._state = "executing"
        self._busy = threading.Lock()
        self._busy_since = 0.0
        self._busy_gen = 0
        self._busy_meta = threading.Lock()
        self._local_jobs_lock = threading.Lock()
        self._local_jobs = {
            "local-running": {"id": "local-running", "status": "running"},
        }
        self._local_job_cancels = {}
        self._session_job_ids = ["job-harness", "job-cli"]
        self.config = SimpleNamespace(repo=repo)
        self._harness_store = harness_store
        self.cancelled_local: list[str] = []
        self.request_cancel_calls: list[str] = []
        self._steer_queue = collections.deque()
        self._steer_lock = threading.Lock()
        self._steer_pending = False
        self._pending_steer_drop_notice = None
        self._display_transcript: list = []
        self._history: list = []

    @property
    def durable(self):
        return SimpleNamespace(store=self._harness_store)

    def cancel(self) -> None:
        self._cancel.set()
        self._interrupted_swarms = True

    def cancel_local_job(self, job_id: str) -> bool:
        self.cancelled_local.append(job_id)
        job = self._local_jobs.get(job_id)
        if job and job.get("status") == "running":
            job["status"] = "cancelled"
            return True
        return False


def _noop(*_a, **_k):
    return None


def _job_services(*, get_pilot, get_session, cfg_repo: str = "/repo") -> JobServices:
    return JobServices(
        cfg=SimpleNamespace(repo=cfg_repo),
        sessions=SimpleNamespace(),
        get_pilot=get_pilot,
        get_session=get_session,
        diag=_noop,
        scoped_jobs_snapshot=lambda **_k: [],
        scoped_jobs_with_stores=lambda **_k: ([], None, None),
        retry_on_locked=lambda fn: fn(),
        swarm_registry=lambda: [],
        job_status_is_terminal=lambda _s: False,
        slim_swarm_list_artifacts=lambda *_a, **_k: [],
        job_swarm_accounting=lambda *_a, **_k: (0, 0, 0),
        task_swarm_accounting=lambda *_a, **_k: {},
        routing_saved_usd=lambda *_a, **_k: 0.0,
        cache_saved_usd_swarm=lambda *_a, **_k: 0.0,
        tokens_cached_swarm=lambda *_a, **_k: 0,
        job_dead_run_failure=lambda *_a, **_k: None,
        job_savings_fields=lambda *_a, **_k: {},
        repo_session_stamped_meters=lambda *_a, **_k: {},
        session_cost_split=lambda *_a, **_k: 0.0,
        cache_savings=lambda *_a, **_k: 0.0,
        tool_output_savings_fields=lambda *_a, **_k: {},
        cost_source_label=lambda *_a, **_k: "",
    )


@pytest.fixture(autouse=True)
def _silence_request_cancel(monkeypatch):
    monkeypatch.setattr(
        "puppetmaster.cancellation.request_cancel",
        lambda job_id: None,
    )


def test_mark_store_job_cancelled_falls_back_to_update_status():
    store = _FakeStore([{"id": "j1"}], fail_cancel=True)
    assert mark_store_job_cancelled(store, "j1") is True
    assert store.statuses["j1"] == "cancelled"


def test_drain_job_ids_marks_both_stores(monkeypatch):
    harness = _FakeStore([{"id": "job-harness"}])
    cli = _FakeStore([{"id": "job-cli"}])
    monkeypatch.setattr(
        "harness.cli_job_merge.open_cli_durable_state",
        lambda _repo="": SimpleNamespace(store=cli),
    )
    drained = drain_job_ids_dual_store(
        ["job-harness", "job-cli", "missing"],
        harness_store=harness,
        repo_root="/repo",
    )
    assert "job-harness" in drained
    assert "job-cli" in drained
    assert harness.cancelled == ["job-harness"]
    assert cli.cancelled == ["job-cli"]
    # Neither store may keep those ids actionable.
    assert harness.statuses["job-harness"] == "cancelled"
    assert cli.statuses["job-cli"] == "cancelled"


class _UpsertingStore:
    """Store that upserts cancelled rows for unknown ids (phantom trap)."""

    def __init__(self, jobs):
        self._jobs = list(jobs)
        self.cancelled: list[str] = []
        self.statuses: dict[str, str] = {
            (j.get("id") if isinstance(j, dict) else getattr(j, "id", "")): "running"
            for j in jobs
            if (j.get("id") if isinstance(j, dict) else getattr(j, "id", None))
        }

    def list_jobs(self):
        return list(self._jobs)

    def update_job_status(self, job_id: str, status: str):
        # Deliberately upserts — membership guard must prevent drain from
        # creating phantom cancelled rows for sibling-store-only jobs.
        self.cancelled.append(job_id)
        self.statuses[job_id] = status
        if not any(
            (j.get("id") if isinstance(j, dict) else getattr(j, "id", None)) == job_id
            for j in self._jobs
        ):
            self._jobs.append({"id": job_id, "status": status})


def test_drain_job_ids_does_not_create_phantom_cancelled_rows(monkeypatch):
    harness = _UpsertingStore([{"id": "job-harness"}])
    cli = _UpsertingStore([{"id": "job-cli"}])
    monkeypatch.setattr(
        "harness.cli_job_merge.open_cli_durable_state",
        lambda _repo="": SimpleNamespace(store=cli),
    )
    drained = drain_job_ids_dual_store(
        ["job-harness", "job-cli"],
        harness_store=harness,
        repo_root="/repo",
    )
    assert set(drained) == {"job-harness", "job-cli"}
    assert harness.cancelled == ["job-harness"]
    assert cli.cancelled == ["job-cli"]
    assert "job-cli" not in harness.statuses
    assert "job-harness" not in cli.statuses
    assert [j["id"] for j in harness.list_jobs()] == ["job-harness"]
    assert [j["id"] for j in cli.list_jobs()] == ["job-cli"]


def test_interrupt_drains_local_and_dual_store_jobs(monkeypatch):
    harness = _FakeStore([{"id": "job-harness"}])
    cli = _FakeStore([{"id": "job-cli"}])
    monkeypatch.setattr(
        "harness.cli_job_merge.open_cli_durable_state",
        lambda _repo="": SimpleNamespace(store=cli),
    )
    request_calls: list[str] = []

    def _track_cancel(job_id):
        request_calls.append(job_id)

    monkeypatch.setattr("puppetmaster.cancellation.request_cancel", _track_cancel)

    host = _InterruptHarness(harness_store=harness)
    host.interrupt()

    assert host._cancel.is_set()
    assert host._interrupt_requested is True
    assert host._stop_holds_idle is True
    assert host._state == "idle"
    assert "local-running" in host.cancelled_local
    assert host._local_jobs["local-running"]["status"] == "cancelled"
    assert set(request_calls) == {"job-harness", "job-cli"}
    # Each store marks its own membership; no actionable strand left.
    assert harness.cancelled == ["job-harness"]
    assert cli.cancelled == ["job-cli"]
    assert harness.statuses["job-harness"] == "cancelled"
    assert cli.statuses["job-cli"] == "cancelled"
    assert "job-cli" not in harness.statuses
    assert "job-harness" not in cli.statuses


def test_interrupt_source_has_no_unsafe_thread_kill():
    import inspect
    import harness.busy_control as bc

    src = inspect.getsource(bc.BusyControlMixin.interrupt)
    src += inspect.getsource(bc.BusyControlMixin._drain_session_jobs_dual_store)
    banned = (
        "TerminateThread",
        "PyThreadState_SetAsyncExc",
        "ctypes.pythonapi",
        "thread._stop",
        ".kill()",
    )
    for token in banned:
        assert token not in src, f"unsafe kill primitive leaked into interrupt: {token}"


def test_http_cancel_and_interrupt_share_dual_store_seam(monkeypatch):
    """Membership cancel (HTTP) and drain (interrupt) use the same helpers."""
    harness = _FakeStore([{"id": "harness-only"}])
    cli = _FakeStore([{"id": "cli-only"}])
    monkeypatch.setattr(
        "harness.cli_job_merge.open_cli_durable_state",
        lambda _repo="": SimpleNamespace(store=cli),
    )

    # HTTP path (membership-gated).
    result = cancel_job_dual_store(
        "cli-only",
        harness_store=harness,
        harness_list_jobs=harness.list_jobs,
        repo_root="/repo",
    )
    assert result == {
        "ok": True, "job_id": "cli-only", "durable": True, "marked": True,
    }
    assert cli.cancelled == ["cli-only"]
    assert harness.cancelled == []

    # Production HTTP handler still resolves CLI-only jobs.
    svc = _job_services(
        get_pilot=lambda: SimpleNamespace(cancel_local_job=lambda _j: False),
        get_session=lambda: SimpleNamespace(
            state=lambda: SimpleNamespace(store=harness, list_jobs=harness.list_jobs)
        ),
    )
    code, body = post_swarm_cancel({"job_id": "harness-only"}, svc)
    assert code == 200
    assert body["ok"] is True
    assert harness.cancelled == ["harness-only"]


# --- S2: Stop ↔ steer boundary -------------------------------------------------


def test_steer_before_stop_is_dropped_with_durable_notice(monkeypatch):
    """Steer enqueued before Stop must not remain queued after interrupt."""
    harness = _FakeStore([{"id": "job-harness"}])
    cli = _FakeStore([{"id": "job-cli"}])
    monkeypatch.setattr(
        "harness.cli_job_merge.open_cli_durable_state",
        lambda _repo="": SimpleNamespace(store=cli),
    )
    host = _InterruptHarness(harness_store=harness)
    host.enqueue_steer("please pivot to auth")
    host.enqueue_steer("also fix the tests")
    assert len(host._steer_queue) == 2

    host.interrupt()

    assert host._stop_holds_idle is True
    assert list(host._steer_queue) == []
    assert host._steer_pending is False
    notice = host._pending_steer_drop_notice
    assert notice is not None
    assert notice["reason"] == "steer_dropped"
    assert notice["count"] == 2
    assert "Dropped 2 queued steer" in notice["message"]
    assert any(
        row.get("type") == "message"
        and "Dropped 2 queued steer" in (row.get("text") or "")
        for row in host._display_transcript
    )


def test_stop_then_late_inject_does_not_contaminate_history(monkeypatch):
    """Abandoned-generator inject path must drop, not piggyback, after Stop."""
    harness = _FakeStore([])
    monkeypatch.setattr(
        "harness.cli_job_merge.open_cli_durable_state",
        lambda _repo="": SimpleNamespace(store=_FakeStore([])),
    )
    host = _InterruptHarness(harness_store=harness)
    host._session_job_ids = []
    host._history = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "t1"}]},
        {"role": "tool", "content": "ok", "tool_call_id": "t1"},
    ]
    host.enqueue_steer("late steer after stop race")
    host.interrupt()
    # Simulate a late enqueue that raced past interrupt's drain.
    with host._steer_lock:
        host._steer_queue.append("raced late steer")

    events = list(host._check_and_inject_steer())

    assert list(host._steer_queue) == []
    assert host._steer_pending is False
    tool_content = host._history[-1].get("content") or ""
    assert "OUT-OF-BAND" not in tool_content
    assert "raced late steer" not in tool_content
    assert any(
        e.kind == "notice" and e.data.get("reason") == "steer_dropped"
        for e in events
    )


def test_new_user_send_after_stop_does_not_receive_stale_steers():
    """A fresh send after Stop must not inherit pre-stop queued steers."""
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    s = ConversationalSession(cfg)
    s.enqueue_steer("stale steer before stop")
    # Abandoned generation: busy still held when Stop lands.
    assert s._busy.acquire(blocking=False)
    s._busy_since = __import__("time").monotonic() - 1.0
    s._state = "executing"
    s.interrupt()
    assert list(s._steer_queue) == []
    assert s._steer_boundary_drop_on_acquire is True
    # While the abandoned generator still owns _busy, refuse new steers.
    s.enqueue_steer("steer while abandoned busy")
    assert list(s._steer_queue) == []
    assert s._pending_steer_drop_notice is not None

    s._state = "idle"
    events = list(s.send("hello again after stop"))
    history_blob = " ".join(str(m.get("content") or "") for m in s._history)
    assert "stale steer before stop" not in history_blob
    assert "steer while abandoned busy" not in history_blob
    assert "OUT-OF-BAND" not in history_blob
    assert s._stop_holds_idle is False
    busy_errs = [
        e for e in events
        if e.kind == "error" and "busy" in str(e.data.get("error", ""))
    ]
    assert not busy_errs
    assert any(
        e.kind == "notice" and e.data.get("reason") == "steer_dropped"
        for e in events
    )


def test_ready_session_steer_after_idle_stop_is_preserved():
    """Idle-session Stop must not discard a later ready-session steer on acquire."""
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    s = ConversationalSession(cfg)
    s.interrupt()
    assert s._stop_holds_idle is True
    assert s._steer_boundary_drop_on_acquire is False
    assert not s._busy.locked()
    # Ready/idle: standard enqueue even while Stop hold is sticky for UI.
    s.enqueue_steer("legitimate ready-session steer")
    assert list(s._steer_queue) == ["legitimate ready-session steer"]

    assert s._busy.acquire(blocking=False)
    s._mark_busy_acquired()
    assert s._stop_holds_idle is False
    # Not an abandoned-generation acquire — keep the ready-session steer.
    assert list(s._steer_queue) == ["legitimate ready-session steer"]
    s._release_busy(s._busy_gen)


def test_post_unwind_steer_survives_next_acquire_after_abandoned_release():
    """After abandoned gen releases busy, intentional post-unwind steers survive.

    Pre-unwind race steers remain covered by interrupt drain + acquire-time drop
    on force-recover paths that free the lock without ``_release_busy``.
    """
    import time as _t

    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    s = ConversationalSession(cfg)
    assert s._busy.acquire(blocking=False)
    s._busy_since = _t.monotonic() - 1.0
    s._busy_gen = 1
    s._state = "executing"
    s.interrupt()
    assert s._steer_boundary_drop_on_acquire is True
    assert s._stop_holds_idle is True
    # Abandoned owner finally unwinds — clears generation-scoped drop boundary.
    s._release_busy(1)
    assert s._steer_boundary_drop_on_acquire is False
    assert not s._busy.locked()
    # Sticky idle hold may remain for UI; enqueue is allowed once busy is free.
    s.enqueue_steer("intentional post-unwind steer")
    assert list(s._steer_queue) == ["intentional post-unwind steer"]

    assert s._busy.acquire(blocking=False)
    s._mark_busy_acquired()
    assert list(s._steer_queue) == ["intentional post-unwind steer"]
    s._release_busy(s._busy_gen)


def test_post_stop_late_steer_cleanup_records_drop_notice():
    """Late steers that race past interrupt must not vanish silently on acquire."""
    import time as _t

    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    s = ConversationalSession(cfg)
    assert s._busy.acquire(blocking=False)
    s._busy_since = _t.monotonic() - 1.0
    s._state = "executing"
    s.interrupt()
    assert s._stop_holds_idle is True
    assert s._steer_boundary_drop_on_acquire is True
    # Simulate a late enqueue that raced past interrupt's drain.
    with s._steer_lock:
        s._steer_queue.append("raced late steer after stop")

    # Force-recover path: new turn acquires after abandoning the prior gen.
    with s._busy_meta:
        s._busy_gen += 1
        s._busy_since = 0.0
        try:
            s._busy.release()
        except RuntimeError:
            pass
    assert s._busy.acquire(blocking=False)
    s._mark_busy_acquired()

    assert list(s._steer_queue) == []
    notice = s._pending_steer_drop_notice
    assert notice is not None
    assert notice["reason"] == "steer_dropped"
    assert notice["count"] == 1
    assert "Dropped 1 queued steer" in notice["message"]
    assert any(
        row.get("type") == "message"
        and "Dropped 1 queued steer" in (row.get("text") or "")
        for row in s._display_transcript
    )


def test_interrupt_source_still_has_no_unsafe_thread_kill_after_steer_boundary():
    """S2 additions must not introduce force-kill primitives."""
    import inspect
    import harness.busy_control as bc
    import harness.steer_mixin as sm

    src = inspect.getsource(bc.BusyControlMixin.interrupt)
    src += inspect.getsource(bc.BusyControlMixin._drain_session_jobs_dual_store)
    src += inspect.getsource(sm.SteerMixin.drop_queued_steers)
    src += inspect.getsource(sm.SteerMixin._check_and_inject_steer)
    banned = (
        "TerminateThread",
        "PyThreadState_SetAsyncExc",
        "ctypes.pythonapi",
        "thread._stop",
        ".kill()",
    )
    for token in banned:
        assert token not in src, f"unsafe kill primitive leaked: {token}"
