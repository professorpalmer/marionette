"""Worktree subprocess reaper: kill orphaned indexers whose cwd is inside a
worktree before it is removed. Hermetic -- never spawns or kills real procs."""
from __future__ import annotations

import os
import signal
import subprocess

import harness.worktrees as wt


def test_cwd_under_rejects_sibling(tmp_path):
    # pmedit-89 must NOT match pmedit-8941d0fa (prefix-without-separator trap).
    base = tmp_path
    short = os.path.join(str(base), "pmedit-89")
    long = os.path.join(str(base), "pmedit-8941d0fa")
    os.makedirs(short)
    os.makedirs(long)
    child = os.path.join(long, "sub")
    os.makedirs(child)

    assert wt._cwd_under(long, long) is True
    assert wt._cwd_under(child, long) is True
    assert wt._cwd_under(short, long) is False   # sibling, not under
    assert wt._cwd_under(long, short) is False


def test_cwd_under_bad_inputs():
    assert wt._cwd_under("", "/x") is False
    assert wt._cwd_under("/x", "") is False


def test_reap_no_matches_returns_zero(tmp_path, monkeypatch):
    # No processes report a cwd under the path -> 0, no raise.
    monkeypatch.setattr(wt, "_worktree_pid_cwds", lambda: [])
    assert wt.reap_worktree_processes(str(tmp_path)) == 0


def test_reap_posix_signals_only_safe_matched_pids(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "name", "posix")
    wtpath = os.path.realpath(str(tmp_path))
    me = os.getpid()
    parent = os.getppid()
    fake_target = 424242

    # Enumerator returns a matched target PLUS unsafe pids that must be skipped.
    monkeypatch.setattr(wt, "_worktree_pid_cwds", lambda: [
        (fake_target, os.path.join(wtpath, "sub")),  # matched, safe
        (1, wtpath),                                  # init -- must skip
        (me, wtpath),                                 # self -- must skip
        (parent, wtpath),                             # parent -- must skip
        (999999, "/somewhere/else"),                 # not under path -- skip
    ])

    killed: list[tuple[int, int]] = []

    def fake_kill(pid, sig):
        # signal 0 is the liveness probe; report "dead" so no SIGKILL follows.
        if sig == 0:
            raise ProcessLookupError()
        killed.append((pid, sig))

    monkeypatch.setattr(os, "kill", fake_kill)

    n = wt.reap_worktree_processes(str(tmp_path))

    assert n == 1
    sigterms = [pid for pid, sig in killed if sig == signal.SIGTERM]
    assert sigterms == [fake_target]
    for pid, _sig in killed:
        assert pid not in (1, me, parent)


def test_reap_windows_taskkills_only_safe_matched_pids(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    wtpath = os.path.realpath(str(tmp_path))
    me = os.getpid()
    parent = os.getppid()
    fake_target = 424242

    monkeypatch.setattr(wt, "_worktree_pid_cwds", lambda: [
        (fake_target, os.path.join(wtpath, "sub")),
        (1, wtpath),
        (me, wtpath),
        (parent, wtpath),
        (999999, "/somewhere/else"),
    ])

    taskkills: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        if cmd and cmd[0] == "taskkill":
            taskkills.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(wt.subprocess, "run", fake_run)

    n = wt.reap_worktree_processes(str(tmp_path))

    assert n == 1
    assert len(taskkills) == 1
    assert taskkills[0] == ["taskkill", "/PID", str(fake_target), "/T", "/F"]
    for cmd in taskkills:
        pid = int(cmd[2])
        assert pid not in (1, me, parent)


def test_worktree_pid_cwds_routes_to_windows_branch(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    calls: list[str] = []

    def fake_windows():
        calls.append("windows")
        return []

    def fake_posix():
        calls.append("posix")
        return []

    monkeypatch.setattr(wt, "_worktree_pid_cwds_windows", fake_windows)
    monkeypatch.setattr(wt, "_worktree_pid_cwds_posix", fake_posix)

    assert wt._worktree_pid_cwds() == []
    assert calls == ["windows"]


def test_worktree_pid_cwds_routes_to_posix_branch(monkeypatch):
    monkeypatch.setattr(os, "name", "posix")
    calls: list[str] = []

    def fake_windows():
        calls.append("windows")
        return []

    def fake_posix():
        calls.append("posix")
        return []

    monkeypatch.setattr(wt, "_worktree_pid_cwds_windows", fake_windows)
    monkeypatch.setattr(wt, "_worktree_pid_cwds_posix", fake_posix)

    assert wt._worktree_pid_cwds() == []
    assert calls == ["posix"]
