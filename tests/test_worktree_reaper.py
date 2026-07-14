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


def test_parse_lsof_cwd_output():
    stdout = "p12345\nn/tmp/worktree\np67890\nn/home/user\n"
    assert wt._parse_lsof_cwd_output(stdout) == [
        (12345, "/tmp/worktree"),
        (67890, "/home/user"),
    ]
    assert wt._parse_lsof_cwd_output("") == []
    assert wt._parse_lsof_cwd_output("pnotanint\nn/ignored\n") == []


def test_posix_lsof_success_returns_parsed_pairs(monkeypatch):
    def fake_run(cmd, **kwargs):
        assert cmd[:4] == ["lsof", "-a", "-d", "cwd"]
        return subprocess.CompletedProcess(
            cmd, 0, "p4242\nn/var/wt/sub\n", "",
        )

    monkeypatch.setattr(wt.subprocess, "run", fake_run)
    assert wt._worktree_pid_cwds_lsof() == [(4242, "/var/wt/sub")]


def test_posix_lsof_success_empty_skips_proc_fallback(monkeypatch):
    proc_called: list[bool] = []

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def fake_proc():
        proc_called.append(True)
        return [(999, "/proc/fallback")]

    monkeypatch.setattr(wt.subprocess, "run", fake_run)
    monkeypatch.setattr(wt, "_worktree_pid_cwds_proc_linux", fake_proc)
    monkeypatch.setattr(wt.sys, "platform", "linux")

    assert wt._worktree_pid_cwds_posix() == []
    assert proc_called == []


def test_posix_lsof_failure_linux_falls_back_to_proc(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("lsof")

    def fake_proc():
        return [(111, "/wt/orphan"), (222, "/elsewhere")]

    monkeypatch.setattr(wt.subprocess, "run", fake_run)
    monkeypatch.setattr(wt, "_worktree_pid_cwds_proc_linux", fake_proc)
    monkeypatch.setattr(wt.sys, "platform", "linux")

    assert wt._worktree_pid_cwds_posix() == [(111, "/wt/orphan"), (222, "/elsewhere")]


def test_posix_lsof_nonzero_linux_falls_back_to_proc(monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", "permission denied")

    def fake_proc():
        return [(333, "/wt/from-proc")]

    monkeypatch.setattr(wt.subprocess, "run", fake_run)
    monkeypatch.setattr(wt, "_worktree_pid_cwds_proc_linux", fake_proc)
    monkeypatch.setattr(wt.sys, "platform", "linux")

    assert wt._worktree_pid_cwds_posix() == [(333, "/wt/from-proc")]


def test_posix_lsof_failure_darwin_returns_empty(monkeypatch):
    proc_called: list[bool] = []

    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("lsof")

    def fake_proc():
        proc_called.append(True)
        return [(444, "/should-not-run")]

    monkeypatch.setattr(wt.subprocess, "run", fake_run)
    monkeypatch.setattr(wt, "_worktree_pid_cwds_proc_linux", fake_proc)
    monkeypatch.setattr(wt.sys, "platform", "darwin")

    assert wt._worktree_pid_cwds_posix() == []
    assert proc_called == []


def test_proc_linux_enumerates_cwds(monkeypatch):
    def fake_listdir(path):
        assert path.replace("\\", "/") == "/proc"
        return ["1", "self", "4242", "not-a-pid", "5678"]

    def fake_isdir(path):
        return path.replace("\\", "/") == "/proc"

    def fake_readlink(path):
        norm = path.replace("\\", "/")
        if norm == "/proc/4242/cwd":
            return "/tmp/worktree-a"
        if norm == "/proc/5678/cwd":
            raise PermissionError("denied")
        raise OSError("missing")

    monkeypatch.setattr("harness.worktrees.os.listdir", fake_listdir)
    monkeypatch.setattr("harness.worktrees.os.path.isdir", fake_isdir)
    monkeypatch.setattr("harness.worktrees.os.readlink", fake_readlink)

    assert wt._worktree_pid_cwds_proc_linux() == [(4242, "/tmp/worktree-a")]


def test_proc_linux_returns_empty_on_error(monkeypatch):
    monkeypatch.setattr("harness.worktrees.os.path.isdir", lambda path: True)
    monkeypatch.setattr(
        "harness.worktrees.os.listdir",
        lambda path: (_ for _ in ()).throw(OSError("boom")),
    )

    assert wt._worktree_pid_cwds_proc_linux() == []
