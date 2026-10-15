from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from harness import mcp_supervisor
from harness.mcp_client import StdioMcpClient

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")

ROOT = str(Path(__file__).resolve().parents[1])
FAKE = str(Path(__file__).parent / "fixtures" / "fake_mcp_server.py")


def _sleeper():
    return subprocess.Popen(["sleep", "60"], start_new_session=True)


def _group_gone(proc, timeout=5.0):
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return False
    try:
        os.killpg(proc.pid, 0)
    except ProcessLookupError:
        return True
    return False


def _cleanup(*procs):
    for p in procs:
        if p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass
            p.wait(timeout=5)


@pytest.fixture
def link(monkeypatch):
    monkeypatch.delenv("HARNESS_MCP_SUPERVISOR", raising=False)
    fresh = mcp_supervisor._Link()
    monkeypatch.setattr(mcp_supervisor, "_LINK", fresh)
    yield fresh
    fresh.close()


def test_supervisor_kills_registered_group_at_stdin_eof():
    child = _sleeper()
    sup = subprocess.Popen(
        [sys.executable, "-m", "harness.mcp_supervisor"],
        stdin=subprocess.PIPE, cwd=ROOT, start_new_session=True,
    )
    try:
        sup.stdin.write(b'{"op":"add","pgid":%d}\n' % child.pid)
        sup.stdin.flush()
        sup.stdin.close()
        assert _group_gone(child)
        assert sup.wait(timeout=5) == 0
    finally:
        _cleanup(child, sup)


def test_unregistered_group_survives_close(link):
    keep, kill = _sleeper(), _sleeper()
    try:
        mcp_supervisor.register(keep.pid)
        mcp_supervisor.register(kill.pid)
        mcp_supervisor.unregister(keep.pid)
        sup = link._proc
        mcp_supervisor.close()
        assert _group_gone(kill)
        sup.wait(timeout=5)
        assert keep.poll() is None
    finally:
        _cleanup(keep, kill)


def test_dead_supervisor_respawns_and_replays_set(link):
    first, second = _sleeper(), _sleeper()
    try:
        mcp_supervisor.register(first.pid)
        old = link._proc
        old.kill()
        old.wait(timeout=5)
        mcp_supervisor.register(second.pid)
        assert link._proc is not old and link._proc.poll() is None
        mcp_supervisor.close()
        assert _group_gone(first)
        assert _group_gone(second)
    finally:
        _cleanup(first, second)


def test_spawn_failure_never_blocks_registration(link, monkeypatch):
    def boom(*a, **k):
        raise OSError("no python")

    monkeypatch.setattr(mcp_supervisor, "_Popen", boom)
    mcp_supervisor.register(424242)
    mcp_supervisor.unregister(424242)
    assert link._proc is None


def test_stdio_client_registers_and_unregisters(link, monkeypatch):
    seen = []
    monkeypatch.setattr(mcp_supervisor, "register", lambda pgid: seen.append(("add", pgid)))
    monkeypatch.setattr(mcp_supervisor, "unregister", lambda pgid: seen.append(("del", pgid)))
    client = StdioMcpClient(name="fake", command=sys.executable, args=[FAKE])
    client.start()
    pid = client._proc.pid
    assert os.getpgid(pid) == pid
    client.stop()
    assert seen == [("add", pid), ("del", pid)]


def test_stdio_server_dies_when_harness_side_pipe_closes(link):
    client = StdioMcpClient(name="fake", command=sys.executable, args=[FAKE])
    client.start()
    proc = client._proc
    try:
        mcp_supervisor.close()  # same as the harness dying: EOF on stdin
        assert _group_gone(proc)
    finally:
        _cleanup(proc)
        client.stop()


def test_dead_groups_are_pruned_before_eof(monkeypatch):
    import threading
    import time

    live, dead = _sleeper(), _sleeper()
    os.killpg(dead.pid, signal.SIGKILL)
    dead.wait(timeout=5)
    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(sys, "stdin", os.fdopen(read_fd, "rb", buffering=0))
    terminated = []
    monkeypatch.setattr(mcp_supervisor, "terminate_groups", lambda groups: terminated.append(set(groups)))

    def feed():
        os.write(write_fd, b'{"op":"add","pgid":%d}\n{"op":"add","pgid":%d}\n' % (live.pid, dead.pid))
        time.sleep(0.4)
        os.close(write_fd)

    feeder = threading.Thread(target=feed)
    feeder.start()
    try:
        assert mcp_supervisor.main(prune_interval_s=0.05) == 0
        feeder.join(timeout=5)
        assert terminated == [{live.pid}]
    finally:
        _cleanup(live)
