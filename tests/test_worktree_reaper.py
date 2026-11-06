"""Worktree subprocess reaper: kill registered Marionette orphans before removal."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest

import harness.worktrees as wt


@pytest.fixture(autouse=True)
def _isolate_worktree_module_dependencies(monkeypatch):
    private_os = SimpleNamespace(**vars(os))
    private_os.path = SimpleNamespace(**vars(os.path))
    monkeypatch.setattr(wt, "os", private_os)
    monkeypatch.setattr(wt, "subprocess", SimpleNamespace(**vars(subprocess)))


def setup_function():
    wt.clear_managed_process_registry_for_tests()


def _managed_worktree_path(tmp_path) -> str:
    wt = tmp_path / ".pmharness-worktrees" / "pmedit-test"
    wt.mkdir(parents=True)
    return str(wt)


def test_reap_no_matches_returns_zero(tmp_path, monkeypatch):
    assert wt.reap_worktree_processes(_managed_worktree_path(tmp_path)) == 0


def test_reap_posix_signals_only_registered_pids(tmp_path, monkeypatch):
    monkeypatch.setattr(wt.os, "name", "posix")
    wtpath = os.path.realpath(_managed_worktree_path(tmp_path))
    me = os.getpid()
    parent = os.getppid()
    fake_target = 424242

    wt.register_worktree_process(wtpath, fake_target, kind="indexer")

    killed: list[tuple[int, int]] = []

    def fake_kill(pid, sig):
        if sig == 0:
            raise ProcessLookupError()
        killed.append((pid, sig))

    monkeypatch.setattr(wt.os, "kill", fake_kill)

    n = wt.reap_worktree_processes(wtpath)

    assert n == 1
    sigterms = [pid for pid, sig in killed if sig == signal.SIGTERM]
    assert sigterms == [fake_target]
    for pid, _sig in killed:
        assert pid not in (1, me, parent)


def test_simulated_process_dependencies_do_not_mutate_host_modules(monkeypatch):
    host = (os.name, os.kill, subprocess.run)

    def fake_kill(*_args, **_kwargs):
        return None

    def fake_run(*_args, **_kwargs):
        return None

    with monkeypatch.context() as patch:
        patch.setattr(wt.os, "name", "nt")
        patch.setattr(wt.os, "kill", fake_kill)
        patch.setattr(wt.subprocess, "run", fake_run)
        observed = (os.name, os.kill, subprocess.run)

    assert (os.name, os.kill, subprocess.run) == host
    assert observed == host


def test_reap_windows_taskkills_only_registered_pids(tmp_path, monkeypatch):
    monkeypatch.setattr(wt.os, "name", "nt")
    wtpath = os.path.realpath(_managed_worktree_path(tmp_path))
    me = os.getpid()
    parent = os.getppid()
    fake_target = 424242

    wt.register_worktree_process(wtpath, fake_target, kind="worker")

    taskkills: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        if cmd and cmd[0] == "taskkill":
            taskkills.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(wt.subprocess, "run", fake_run)

    n = wt.reap_worktree_processes(wtpath)

    assert n == 1
    assert len(taskkills) == 1
    assert taskkills[0] == ["taskkill", "/PID", str(fake_target), "/T", "/F"]
    for cmd in taskkills:
        pid = int(cmd[2])
        assert pid not in (1, me, parent)


def test_managed_worktree_root_resolves_nested_cwd(tmp_path):
    managed = tmp_path / ".pmharness-worktrees" / "pmedit-abc"
    sub = managed / "nested"
    sub.mkdir(parents=True)
    root = wt.managed_worktree_root(str(sub))
    assert root == os.path.realpath(str(managed))


def test_register_worktree_process_ignores_non_managed_paths(tmp_path):
    wt.register_worktree_process(str(tmp_path), 12345, kind="worker")
    assert wt.reap_worktree_processes(str(tmp_path)) == 0


def test_bind_and_release_worktree_subprocess_spawn_site(tmp_path, monkeypatch):
    """Spawn-site helpers must register on start and unregister on finish."""
    monkeypatch.setattr(wt.os, "name", "nt")
    wtpath = os.path.realpath(_managed_worktree_path(tmp_path))
    fake_pid = 777001
    proc = SimpleNamespace(pid=fake_pid)

    wt.bind_worktree_subprocess(wtpath, proc, kind="worker")
    assert wt._registered_pids_for_worktree(wtpath) == [fake_pid]

    taskkills: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        if cmd and cmd[0] == "taskkill":
            taskkills.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(wt.subprocess, "run", fake_run)

    # Still registered -> reaper would target it.
    assert wt.reap_worktree_processes(wtpath) == 1
    assert taskkills and int(taskkills[0][2]) == fake_pid

    # Simulate child finish: unregister so a reused PID cannot be killed later.
    wt.clear_managed_process_registry_for_tests()
    wt.bind_worktree_subprocess(wtpath, proc, kind="indexer")
    wt.release_worktree_subprocess(wtpath, proc)
    assert wt._registered_pids_for_worktree(wtpath) == []

    taskkills.clear()
    assert wt.reap_worktree_processes(wtpath) == 0
    assert taskkills == []


def test_unregister_prevents_pid_reuse_kill(tmp_path, monkeypatch):
    monkeypatch.setattr(wt.os, "name", "posix")
    wtpath = os.path.realpath(_managed_worktree_path(tmp_path))
    pid = 888002
    wt.register_worktree_process(wtpath, pid, kind="worker")
    wt.unregister_worktree_process(wtpath, pid)

    killed: list[tuple[int, int]] = []

    def fake_kill(pid_arg, sig):
        killed.append((pid_arg, sig))

    monkeypatch.setattr(wt.os, "kill", fake_kill)
    assert wt.reap_worktree_processes(wtpath) == 0
    assert killed == []


def test_reap_leaves_a_recycled_pid_alone(tmp_path, monkeypatch):
    # A child that exited without release leaves its pid registered; the OS can
    # give that pid to an unrelated process before the worktree is removed.
    wtpath = os.path.realpath(_managed_worktree_path(tmp_path))
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        wt.register_worktree_process(wtpath, other.pid, kind="worker")
        key = wt._normalize_worktree_path(wt.managed_worktree_root(wtpath))
        wt._managed_processes[key][other.pid] = ("worker", "an-earlier-process")
        assert wt.reap_worktree_processes(wtpath) == 0
        assert other.poll() is None
    finally:
        other.kill()
        other.wait()


def test_reap_stops_the_registered_process(tmp_path):
    wtpath = os.path.realpath(_managed_worktree_path(tmp_path))
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        wt.register_worktree_process(wtpath, child.pid, kind="worker")
        assert wt.reap_worktree_processes(wtpath) == 1
        assert child.wait(timeout=10) is not None
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
