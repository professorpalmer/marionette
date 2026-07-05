"""Worktree subprocess reaper: kill orphaned indexers whose cwd is inside a
worktree before it is removed. Hermetic -- never spawns or kills real procs."""
from __future__ import annotations

import os
import signal

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


def test_reap_signals_only_safe_matched_pids(tmp_path, monkeypatch):
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
