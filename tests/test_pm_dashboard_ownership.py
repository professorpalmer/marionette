"""Marionette owns at most one dashboard and never leaves one behind."""
import json
import threading
import time

import pytest

import harness.pm_dashboard as pd


class FakeProc:
    _next = 900000

    def __init__(self, alive=True):
        FakeProc._next += 1
        self.pid = FakeProc._next
        self.alive = alive
        self.terminated = False

    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        self.terminated = True
        self.alive = False

    def wait(self, *_a):
        return 0

    def kill(self):
        self.alive = False


@pytest.fixture
def board(monkeypatch, tmp_path):
    monkeypatch.setattr(pd, "_owned", {})
    monkeypatch.setattr(pd, "_ledger_path", str(tmp_path / "ledger.json"))
    monkeypatch.setattr(pd, "_exit_registered", True)
    runfiles = {}
    spawned = []

    def spawn(state_dir, host, port, job_id, *, popen):
        proc = FakeProc()
        spawned.append((state_dir, proc))
        threading.Timer(0.05, lambda: runfiles.__setitem__(
            state_dir, {"pid": proc.pid, "host": host, "port": port})).start()
        return proc

    def reuse(state_dir, host, port, all_projects):
        info = runfiles.get(state_dir)
        if info and any(p.pid == info["pid"] and p.alive for _, p in spawned):
            return {"ok": True, "reused": True, "host": host, "port": port,
                    "pid": info["pid"], "state_dir": state_dir}
        return None

    import puppetmaster.dashboard as pmd
    monkeypatch.setattr(pd, "_spawn_dashboard_cli", spawn)
    monkeypatch.setattr(pd, "_reuse_tracked_dashboard", reuse)
    monkeypatch.setattr(pmd, "read_dashboard_runfile", lambda sd: runfiles.get(str(sd)))
    monkeypatch.setattr(pmd, "dashboard_serves", lambda *a, **k: True)
    monkeypatch.setattr(pmd, "read_child_stderr_tail", lambda *_a: "")
    return spawned, runfiles


def test_concurrent_callers_spawn_one_board(board):
    spawned, _ = board
    out = []
    threads = [threading.Thread(target=lambda: out.append(pd.ensure_local_dashboard(state_dir="/s/a")))
               for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(spawned) == 1
    assert all(o["ok"] for o in out)


def test_switching_store_stops_the_previous_board(board):
    spawned, _ = board
    assert pd.ensure_local_dashboard(state_dir="/s/a")["ok"]
    assert pd.ensure_local_dashboard(state_dir="/s/b")["ok"]
    (_, a), (_, b) = spawned
    assert a.terminated and not b.terminated
    assert list(pd._owned) == ["/s/b"]


def test_a_board_that_never_starts_is_stopped(board, monkeypatch):
    spawned, runfiles = board
    monkeypatch.setattr(pd, "_spawn_dashboard_cli", lambda *a, popen: spawned.append(("x", FakeProc())) or spawned[-1][1])
    out = pd.ensure_local_dashboard(state_dir="/s/a", wait_s=0.1)
    assert out["error"] == "dashboard_failed_to_start"
    assert spawned[0][1].terminated
    assert pd._owned == {}


def test_exit_stops_owned_boards_and_clears_ledger(board, tmp_path):
    spawned, _ = board
    pd.ensure_local_dashboard(state_dir="/s/a")
    assert json.loads((tmp_path / "ledger.json").read_text()) == {"/s/a": spawned[0][1].pid}
    pd.stop_owned_dashboards()
    assert spawned[0][1].terminated
    assert json.loads((tmp_path / "ledger.json").read_text()) == {}


def test_boot_reaps_only_ledger_pids_the_runfile_still_names(monkeypatch, tmp_path):
    import puppetmaster.dashboard as pmd
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({"/s/a": 111, "/s/b": 222}))
    monkeypatch.setattr(pd, "_owned", {})
    monkeypatch.setattr(pmd, "pid_alive", lambda pid: True)
    monkeypatch.setattr(pmd, "pid_reused", lambda pid, identity: False)
    monkeypatch.setattr(pmd, "read_dashboard_runfile",
                        lambda sd: {"/s/a": {"pid": 111, "identity": "a"},
                                    "/s/b": {"pid": 999, "identity": "b"}}[sd])
    stopped = []
    monkeypatch.setattr(pd, "_stop_process", lambda pid, proc=None: stopped.append(pid))
    assert pd.reap_orphaned_dashboards(str(ledger)) == 1
    assert stopped == [111]
    assert json.loads(ledger.read_text()) == {}


def test_boot_reap_leaves_a_recycled_pid_alone(monkeypatch, tmp_path):
    # A crash leaves the same dead pid in the ledger and in the runfile; the OS
    # then gives it to an unrelated process.
    import puppetmaster.dashboard as pmd
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({"/s/a": 111}))
    monkeypatch.setattr(pd, "_owned", {})
    monkeypatch.setattr(pmd, "pid_alive", lambda pid: True)
    monkeypatch.setattr(pmd, "pid_reused", lambda pid, identity: True)
    monkeypatch.setattr(pmd, "read_dashboard_runfile", lambda sd: {"pid": 111, "identity": "a"})
    stopped = []
    monkeypatch.setattr(pd, "_stop_process", lambda pid, proc=None: stopped.append(pid))
    assert pd.reap_orphaned_dashboards(str(ledger)) == 0
    assert stopped == []
