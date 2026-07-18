"""S3 ownership wiring: WarmAcpSession close/reap on owner paths."""

from __future__ import annotations

import threading
from typing import List, Optional

from harness.busy_control import BusyControlMixin
from harness.session_runners import SessionRunnerRegistry
from pmharness.drivers.cursor_acp import WarmAcpSession


class _FakeRunner(BusyControlMixin):
    """Minimal interrupt surface with release_warm_acp tracking."""

    def __init__(self) -> None:
        self._cancel = threading.Event()
        self._interrupt_requested = False
        self._stop_holds_idle = False
        self._state = "idle"
        self._local_jobs_lock = threading.Lock()
        self._local_jobs: dict = {}
        self._session_job_ids: list = []
        self.reasons: list[str] = []

    def cancel(self) -> None:
        self._cancel.set()

    def cancel_local_job(self, _jid) -> None:
        return None

    def drop_queued_steers(self) -> list:
        return []

    def release_warm_acp(self, *, reason: str = "close", cwd=None) -> None:
        self.reasons.append(reason)


def test_interrupt_releases_warm_acp():
    runner = _FakeRunner()
    runner.interrupt()
    assert "interrupt" in runner.reasons


def test_registry_drop_releases_warm_acp_on_session_switch():
    reg = SessionRunnerRegistry(max_concurrent_sessions=3)
    runner = _FakeRunner()
    reg.get_or_create("s1", lambda: runner)
    dropped = reg.drop("s1")
    assert dropped is runner
    assert runner.reasons == ["session_switch"]


def test_registry_drop_idempotent_when_missing():
    reg = SessionRunnerRegistry(max_concurrent_sessions=3)
    assert reg.drop("missing") is None


class _BarrierTransport:
    """Deterministic fake: blocks in initialize until released; idempotent close."""

    def __init__(
        self,
        *,
        entered: threading.Event,
        release: threading.Event,
        created: list,
    ) -> None:
        self._entered = entered
        self._release = release
        self.close_calls = 0
        self._closed = False
        self._alive = True
        created.append(self)

    def alive(self) -> bool:
        return self._alive and not self._closed

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._alive = False
        self.close_calls += 1

    def request(self, method: str, params: dict, timeout: Optional[float] = None) -> dict:
        if method == "initialize":
            self._entered.set()
            assert self._release.wait(timeout=5.0), "handshake release timed out"
            if self._closed:
                return {"error": {"message": "closed"}}
            return {"result": {}}
        if method == "session/new":
            return {"result": {"sessionId": "sess-race-1"}}
        if method == "session/set_mode":
            return {"result": {}}
        if method == "authenticate":
            return {"result": {}}
        return {"result": {}}

    def notify(self, _method: str, _params: dict) -> None:
        return


def test_close_during_ensure_reaps_inflight_exactly_once():
    """Close mid-handshake must reap the owned transport exactly once."""
    entered = threading.Event()
    release = threading.Event()
    created: List[_BarrierTransport] = []

    def factory() -> _BarrierTransport:
        return _BarrierTransport(entered=entered, release=release, created=created)

    session = WarmAcpSession(model="m", cwd=None, transport_factory=factory)
    errors: list = []

    def run_ensure() -> None:
        try:
            session.ensure()
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=run_ensure, name="warm-acp-ensure-race")
    worker.start()
    assert entered.wait(timeout=5.0), "ensure never reached handshake"
    # Close while transport exists only as in-flight (not yet published).
    session.close()
    release.set()
    worker.join(timeout=5.0)
    assert not worker.is_alive()

    assert len(created) == 1
    assert created[0].close_calls == 1
    assert session.transport is None
    assert session.session_id is None
    assert session._inflight is None
    assert errors, "ensure must abort after close during handshake"
    assert "closed during ensure" in str(errors[0]).lower() or "closed" in str(
        errors[0]
    ).lower()


def test_ensure_after_close_still_warms_normally():
    """Normal warm path is preserved: ensure after a prior close still works."""
    created: List[_BarrierTransport] = []
    # Non-blocking transport: release immediately.
    always = threading.Event()
    always.set()

    def factory() -> _BarrierTransport:
        return _BarrierTransport(
            entered=threading.Event(),
            release=always,
            created=created,
        )

    session = WarmAcpSession(model="m", cwd=None, transport_factory=factory)
    session.close()  # idle close is a no-op on children
    transport = session.ensure()
    assert transport is created[-1]
    assert session.session_id == "sess-race-1"
    assert created[-1].close_calls == 0
    # Unrelated second transport is never closed by a later idle close of a
    # different session instance.
    other_created: List[_BarrierTransport] = []

    def other_factory() -> _BarrierTransport:
        return _BarrierTransport(
            entered=threading.Event(),
            release=always,
            created=other_created,
        )

    other = WarmAcpSession(model="m", cwd=None, transport_factory=other_factory)
    other.ensure()
    session.close()
    assert created[-1].close_calls == 1
    assert other_created[-1].close_calls == 0
    other.close()
    assert other_created[-1].close_calls == 1
