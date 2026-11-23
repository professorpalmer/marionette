"""S3 ownership wiring: WarmAcpSession close/reap on owner paths."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional

import pytest

from harness.busy_control import BusyControlMixin
from harness.session_runners import SessionRunnerRegistry
from pmharness.drivers import cursor_acp
from pmharness.drivers.cursor_acp import AcpTransport, WarmAcpSession


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


# Stands in for ``agent acp``: it completes the handshake, starts a helper
# (the real agent starts MCP servers), and does not exit at stdin EOF.
_STAND_IN_AGENT = """
import json, os, subprocess, sys, time

helper = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(120)"],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
with open("pids.json", "w", encoding="utf-8") as fh:
    json.dump({"agent": os.getpid(), "helper": helper.pid}, fh)
for line in sys.stdin:
    msg = json.loads(line)
    if "id" in msg:
        result = {"sessionId": "s1"} if msg["method"] == "session/new" else {}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}) + "\\n")
        sys.stdout.flush()
time.sleep(120)
"""

# A process that warms a session and exits with no close call.
_EXIT_WITH_NO_CLOSE = """
import sys
from pmharness.drivers import cursor_acp

cursor_acp.keychain_missing_reason = lambda: None
cursor_acp.resolve_agent_exec = lambda: [sys.executable, sys.argv[1]]
cursor_acp.WarmAcpSession(model="m", cwd=sys.argv[2]).ensure()
"""

_posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")


def _running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    state = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True,
    ).stdout.strip()
    return bool(state) and not state.startswith("Z")


def _still_running(pids: dict, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and any(_running(p) for p in pids.values()):
        time.sleep(0.05)
    return {name: pid for name, pid in pids.items() if _running(pid)}


def _stop(workspace: Path) -> None:
    try:
        pids = json.loads((workspace / "pids.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    for pid in pids.values():
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


@_posix_only
def test_interpreter_exit_ends_agent_and_its_children(tmp_path):
    """A process that exits with a warm session must not leave ``agent acp``."""
    agent = tmp_path / "stand_in_agent.py"
    agent.write_text(_STAND_IN_AGENT, encoding="utf-8")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    try:
        done = subprocess.run(
            [sys.executable, "-c", _EXIT_WITH_NO_CLOSE, str(agent), str(workspace)],
            cwd=str(Path(__file__).resolve().parents[1]),
            capture_output=True, text=True, timeout=60,
        )
        assert done.returncode == 0, done.stderr
        pids = json.loads((workspace / "pids.json").read_text(encoding="utf-8"))
        assert _still_running(pids) == {}
    finally:
        _stop(workspace)


@_posix_only
def test_close_ends_agent_and_its_children(tmp_path, monkeypatch):
    """Close must end the helpers of the agent, not only the top process."""
    agent = tmp_path / "stand_in_agent.py"
    agent.write_text(_STAND_IN_AGENT, encoding="utf-8")
    monkeypatch.setattr(cursor_acp, "keychain_missing_reason", lambda: None)
    monkeypatch.setattr(
        cursor_acp, "resolve_agent_exec", lambda: [sys.executable, str(agent)],
    )
    session = WarmAcpSession(model="m", cwd=str(tmp_path))
    try:
        session.ensure()
        pids = json.loads((tmp_path / "pids.json").read_text(encoding="utf-8"))
        assert _still_running(pids, timeout=0.0) == pids
        session.close()
        assert _still_running(pids) == {}
    finally:
        session.close()
        _stop(tmp_path)


class _IdleProc:
    """A child that stays alive until terminate."""

    def __init__(self) -> None:
        self.pid = None
        self.stdin = None
        self.stdout = None
        self.terminated = False

    def poll(self) -> Optional[int]:
        return 0 if self.terminated else None

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: Optional[float] = None) -> int:
        return 0


def test_exit_hook_closes_transport_that_no_owner_closed():
    proc = _IdleProc()
    transport = AcpTransport(proc)  # type: ignore[arg-type]
    assert transport.alive()
    cursor_acp.close_open_transports()
    assert proc.terminated
    assert not transport.alive()
    assert transport not in cursor_acp._OPEN_TRANSPORTS



# Findings of the release audit of the acp teardown (hpf, 2026-11-23).

_posix_only = pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except OSError:
        return False
    return True


@_posix_only
def test_close_ends_the_group_after_the_leader_was_reaped():
    leader = subprocess.Popen(
        ["sh", "-c", "sleep 30 & exit 0"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, start_new_session=True)
    leader.wait()
    assert _group_alive(leader.pid)
    try:
        AcpTransport(leader, own_group=True).close()
        deadline = time.monotonic() + 2
        while _group_alive(leader.pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _group_alive(leader.pid)
    finally:
        try:
            os.killpg(leader.pid, signal.SIGKILL)
        except OSError:
            pass


@_posix_only
def test_close_kills_a_group_member_that_ignores_sigterm(tmp_path):
    pid_file = tmp_path / "pid"
    # The member writes its pid only after it ignores SIGTERM.
    member_code = (
        "import os, signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(60)"
    )
    code = (
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {member_code!r}])\n"
        "time.sleep(60)\n"
    )
    leader = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, text=True, start_new_session=True)
    deadline = time.monotonic() + 10
    while not (pid_file.exists() and pid_file.read_text()) and time.monotonic() < deadline:
        time.sleep(0.05)
    member = int(pid_file.read_text())
    try:
        AcpTransport(leader, own_group=True).close()
        deadline = time.monotonic() + 2
        while _alive(member) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(member)
    finally:
        try:
            os.killpg(leader.pid, signal.SIGKILL)
        except OSError:
            pass


def test_agent_acp_gets_the_agent_child_env(monkeypatch):
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured.update(kwargs)
        return _IdleProc()

    monkeypatch.setenv("CURSOR_CLI_LOGIN", "key-abc-1234567890")
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    monkeypatch.setattr(cursor_acp, "keychain_missing_reason", lambda *a, **k: None)
    monkeypatch.setattr(cursor_acp, "resolve_agent_exec", lambda: ["agent"])
    monkeypatch.setattr(cursor_acp.subprocess, "Popen", fake_popen)

    transport = WarmAcpSession(model="m", cwd=None)._spawn_transport()
    try:
        env = captured["env"]
        assert env["CURSOR_API_KEY"] == "key-abc-1234567890"
        assert env["MARIONETTE_TRACKABLE_SWARMS"] == "1"
        assert env["PATH"].split(os.pathsep)[0] == os.path.dirname(os.path.abspath(sys.executable))
    finally:
        transport._own_group = False  # the fake child has no process group
        transport.close()


@_posix_only
def test_agent_child_env_puts_the_venv_folder_first_not_the_symlink_target(tmp_path, monkeypatch):
    from pmharness.drivers import cursor_cli

    base = tmp_path / "base" / "bin"
    venv = tmp_path / "venv" / "bin"
    base.mkdir(parents=True)
    venv.mkdir(parents=True)
    (base / "python3").write_text("", encoding="utf-8")
    (venv / "python").symlink_to(base / "python3")
    monkeypatch.setattr(sys, "executable", str(venv / "python"))
    monkeypatch.setenv("PATH", "/usr/bin")

    assert cursor_cli._agent_child_env()["PATH"].split(os.pathsep)[0] == str(venv)
