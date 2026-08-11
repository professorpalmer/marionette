"""Stop/interrupt hard-cancels Marionette-owned run_cancellable process trees.

Proves:
1. Owned procs registered under cancel Events are process-group killed on interrupt.
2. Ownership is registry-only — never inferred by cwd scan.
3. Idle is held only after owned cancel/kill (or orphan notice).
4. Unrelated / unregistered PIDs are never signaled.
"""
from __future__ import annotations

import collections
import threading
from types import SimpleNamespace

import harness.command_policy as cp
from harness.busy_control import BusyControlMixin
from harness.steer_mixin import SteerMixin


def setup_function():
    cp.clear_owned_command_registry_for_tests()


def teardown_function():
    cp.clear_owned_command_registry_for_tests()


class _FakeProc:
    def __init__(self, pid: int, *, alive: bool = True):
        self.pid = pid
        self._alive = alive
        self.returncode = None if alive else 0
        self.kill_calls = 0
        self.terminate_calls = 0
        self.wait_calls = 0

    def poll(self):
        return None if self._alive else (self.returncode if self.returncode is not None else 0)

    def kill(self):
        self.kill_calls += 1
        self._alive = False
        self.returncode = -9

    def terminate(self):
        self.terminate_calls += 1
        self._alive = False
        self.returncode = -15

    def wait(self, timeout=None):
        self.wait_calls += 1
        self._alive = False
        if self.returncode is None:
            self.returncode = -9
        return self.returncode


class _InterruptHost(BusyControlMixin, SteerMixin):
    def __init__(self):
        self._cancel = threading.Event()
        self._interrupt_requested = False
        self._stop_holds_idle = False
        self._steer_boundary_drop_on_acquire = False
        self._interrupted_swarms = False
        self._state = "executing"
        self._busy = threading.Lock()
        self._busy.acquire()
        self._busy_since = 1.0
        self._busy_gen = 1
        self._busy_meta = threading.Lock()
        self._local_jobs_lock = threading.Lock()
        self._local_jobs = {
            "local-cmd": {
                "id": "local-cmd",
                "status": "running",
                "job_kind": "run_command",
                "role": "command",
                "launch_checkpoint": {"phase": "running"},
            },
            "local-implement": {
                "id": "local-implement",
                "status": "running",
                "role": "implement",
            },
        }
        self._local_job_cancels = {
            "local-cmd": threading.Event(),
            "local-implement": threading.Event(),
        }
        self._session_job_ids = []
        self.config = SimpleNamespace(repo="/repo")
        self.cancelled_local: list[str] = []
        self._steer_queue = collections.deque()
        self._steer_lock = threading.Lock()
        self._steer_pending = False
        self._pending_steer_drop_notice = None
        self._pending_owned_command_orphan_notice = None
        self._display_transcript: list = []
        self._history: list = []

    @property
    def durable(self):
        return SimpleNamespace(store=None)

    def cancel(self) -> None:
        self._cancel.set()
        self._interrupted_swarms = True

    def cancel_local_job(self, job_id: str) -> bool:
        self.cancelled_local.append(job_id)
        ev = self._local_job_cancels.get(job_id)
        if ev is not None:
            ev.set()
        job = self._local_jobs.get(job_id)
        if not job or job.get("status") != "running":
            return False
        # Mirror production: launch-checkpointed command jobs stay non-terminal
        # until the worker finishes; implement jobs terminalize immediately.
        if job.get("job_kind") == "run_command" and isinstance(
            job.get("launch_checkpoint"), dict
        ):
            return True
        job["status"] = "cancelled"
        return True

    def _drain_session_jobs_dual_store(self, job_ids=None):
        return []


def test_register_owned_command_proc_is_not_cwd_based():
    """Foreign cwd must not create ownership — only explicit register does."""
    owner = threading.Event()
    foreign = _FakeProc(424201)
    # No register → kill for this owner is a no-op even if cwd matches a workspace.
    result = cp.kill_owned_command_procs([owner])
    assert result["killed"] == 0
    assert result["signaled"] == []
    assert foreign.kill_calls == 0
    assert cp.owned_command_pids_for_tests(owner) == []


def test_kill_owned_command_procs_only_registered(monkeypatch):
    owner = threading.Event()
    other = threading.Event()
    owned = _FakeProc(424202)
    stranger = _FakeProc(424203)
    cp.register_owned_command_proc(owner, owned)
    cp.register_owned_command_proc(other, stranger)

    killed_groups: list[int] = []

    def fake_kill_group(proc):
        killed_groups.append(int(proc.pid))
        proc.kill()
        return True

    monkeypatch.setattr(cp, "kill_process_group", fake_kill_group)

    result = cp.kill_owned_command_procs([owner])
    assert result["killed"] == 1
    assert result["signaled"] == [424202]
    assert result["orphaned"] == []
    assert killed_groups == [424202]
    assert owned.kill_calls == 1
    assert stranger.kill_calls == 0
    # Owner bucket cleared; stranger remains under its own owner.
    assert cp.owned_command_pids_for_tests(owner) == []
    assert cp.owned_command_pids_for_tests(other) == [424203]


def test_interrupt_terminates_owned_session_and_job_procs(monkeypatch):
    host = _InterruptHost()
    session_proc = _FakeProc(515101)
    job_proc = _FakeProc(515102)
    cp.register_owned_command_proc(host._cancel, session_proc)
    cp.register_owned_command_proc(host._local_job_cancels["local-cmd"], job_proc)

    signaled: list[int] = []

    def fake_kill_group(proc):
        signaled.append(int(proc.pid))
        proc.kill()
        return True

    monkeypatch.setattr(cp, "kill_process_group", fake_kill_group)

    assert host._stop_holds_idle is False
    host.interrupt()

    assert host._cancel.is_set()
    assert host._local_job_cancels["local-cmd"].is_set()
    assert host._local_job_cancels["local-implement"].is_set()
    assert "local-cmd" in host.cancelled_local
    assert "local-implement" in host.cancelled_local
    assert host._local_jobs["local-implement"]["status"] == "cancelled"
    # Checkpointed command job stays non-terminal for partial-output receipt.
    assert host._local_jobs["local-cmd"]["status"] == "running"
    assert set(signaled) == {515101, 515102}
    assert host._stop_holds_idle is True
    assert host._state == "idle"
    assert host.is_turn_busy() is False
    assert cp.owned_command_pids_for_tests() == []


def test_interrupt_orphan_notice_when_kill_cannot_reap(monkeypatch):
    host = _InterruptHost()
    sticky = _FakeProc(616101, alive=True)
    cp.register_owned_command_proc(host._cancel, sticky)

    def fake_kill_group(proc):
        # Pretend taskkill/killpg was attempted but the handle still polls alive.
        return True

    monkeypatch.setattr(cp, "kill_process_group", fake_kill_group)

    host.interrupt()

    assert host._stop_holds_idle is True
    notice = host._pending_owned_command_orphan_notice
    assert notice is not None
    assert notice["reason"] == "owned_command_orphan"
    assert notice["count"] == 1
    assert 616101 in notice["pids"]
    assert any(
        row.get("type") == "message"
        and "may still be running" in (row.get("text") or "")
        for row in host._display_transcript
    )


def test_interrupt_source_has_no_unsafe_thread_kill_primitives():
    import inspect

    src = inspect.getsource(BusyControlMixin.interrupt)
    src += inspect.getsource(BusyControlMixin._kill_owned_command_procs_on_interrupt)
    banned = (
        "TerminateThread",
        "PyThreadState_SetAsyncExc",
        "ctypes.pythonapi",
        "thread._stop",
        ".kill()",
    )
    for token in banned:
        assert token not in src, f"unsafe kill primitive leaked into interrupt: {token}"


def test_run_cancellable_registers_and_unregisters(monkeypatch):
    owner = threading.Event()
    fake = _FakeProc(717101, alive=False)
    fake.returncode = 0
    fake.stdout = None

    def fake_popen(*_a, **_k):
        return fake

    monkeypatch.setattr(cp.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        "harness.os_sandbox.prepare_sandbox_spawn",
        lambda *_a, **_k: None,
    )

    seen_during = []

    orig_wait = cp._run_cancellable_wait

    def wrapped_wait(proc, **kwargs):
        seen_during.extend(cp.owned_command_pids_for_tests(owner))
        return ("ok\n", 0, "ok")

    monkeypatch.setattr(cp, "_run_cancellable_wait", wrapped_wait)

    out, code, status = cp.run_cancellable(
        "echo hi",
        cancel_event=owner,
        timeout=5,
    )
    assert (out, code, status) == ("ok\n", 0, "ok")
    assert seen_during == [717101]
    assert cp.owned_command_pids_for_tests(owner) == []
    assert callable(orig_wait)
