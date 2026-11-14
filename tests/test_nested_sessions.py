from __future__ import annotations

import os
import threading

import pytest

from harness import nested_sessions as ns
from harness.headless_turn import HeadlessHandler
from harness.sessions import SessionStore
from harness.todo import (
    TodoItem,
    TodoPhase,
    apply_todo_op,
    link_task_session,
    phases_from_raw,
    phases_to_dicts,
)


class FakeRunner:
    def __init__(self, sid: str) -> None:
        self.harness_session_id = sid
        self._history: list = []
        self._display_transcript: list = []
        self._busy = threading.Lock()
        self._stop_holds_idle = False
        self.links: list = []
        self.steers: list = []

    def link_todo_session(self, task, sid, name):
        self.links.append((task, sid, name))
        return {"phases": [{"name": "Plan", "tasks": [
            {"content": task or "step", "status": "in_progress", "session": sid, "session_name": name},
        ]}]}

    def enqueue_steer(self, text):
        self.steers.append(text)


class Harness:
    """A supervisor over a real SessionStore with fake runners and turns."""

    def __init__(self, tmp_path) -> None:
        self.store = SessionStore(os.path.join(str(tmp_path), "sessions.json"))
        self.runners: dict = {}
        self.status: dict = {}
        self.turns: list = []
        self.persisted: list = []
        self.trees: list = []
        self.sup = ns.NestedSupervisor(ns.NestServices(
            sessions=self.store,
            get_runner=self.runners.get,
            runner_status=lambda sid: self.status.get(sid, "idle" if sid in self.runners else "missing"),
            run_turn=self._run_turn,
            persist=lambda sid, runner: self.persisted.append(sid),
            diag=lambda *a, **k: None,
            add_worktree=self._add_worktree,
            ensure_runner=self._ensure_runner,
        ))

    def _run_turn(self, sid, message, *, resume=False, driver=""):
        self.turns.append({"sid": sid, "message": message, "resume": resume, "driver": driver})
        self.status[sid] = "running"

    def _add_worktree(self, repo, branch):
        path = os.path.join(repo, ".trees", branch.replace("/", "_"))
        self.trees.append((repo, branch))
        return {"path": path, "branch": branch}

    def _ensure_runner(self, sid):
        return self.runners.setdefault(sid, FakeRunner(sid))

    def parent(self, root="/work/proj") -> FakeRunner:
        row = self.store.create("Robotrek", repo=root, workspace_root=root)
        runner = FakeRunner(row["id"])
        self.runners[row["id"]] = runner
        return runner

    def end_turn(self, sid, *, reply="", outcome=""):
        runner = self.runners.setdefault(sid, FakeRunner(sid))
        if reply:
            runner._history.append({"role": "user", "content": "go"})
            runner._history.append({"role": "assistant", "content": reply})
        self.status[sid] = "idle"
        self.sup.on_turn_end(sid, runner, outcome=outcome)


# -- pure helpers ----------------------------------------------------------


def test_names_are_slugs_and_unique():
    assert ns.normalize_name("Vehicle Return!") == "vehicle-return"
    assert ns.valid_name("vehicle-return")
    assert not ns.valid_name("x")
    assert not ns.valid_name("-bad")
    assert ns.unique_name("a1", ["a1", "a1-2"]) == "a1-3"
    assert ns.branch_name("full-bag", "abcdef123456") == "nest/full-bag-abcdef"


def test_report_is_the_last_reply_cut_to_the_limit():
    history = [
        {"role": "assistant", "content": "old"},
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": [{"type": "text", "text": "x" * 500}]},
        {"role": "assistant", "content": ""},
    ]
    report = ns.report_from_history(history)
    assert len(report) == ns.REPORT_CHARS
    assert report.endswith("...")
    assert ns.report_from_history([]) == ""


def test_live_status_prefers_a_running_runner():
    assert ns.live_status("done", "running") == "working"
    assert ns.live_status("stopped", "idle") == "stopped"
    assert ns.live_status("", "missing") == "done"


def test_wake_rule():
    base = dict(pending_wake=True, siblings_working=False, parent_idle=True, auto_wakes=0, stop_held=False)
    assert ns.should_wake(**base)
    assert not ns.should_wake(**{**base, "siblings_working": True})
    assert not ns.should_wake(**{**base, "stop_held": True})
    assert not ns.should_wake(**{**base, "auto_wakes": ns.MAX_AUTO_WAKES})


def test_note_text_marks_report_as_unverified():
    text = ns.note_text({"child_id": "c1", "name": "full-bag", "status": "done", "report": "Fixed it."})
    assert text.startswith("<nested-session-event>")
    assert 'Child session "full-bag"' in text and "Report: Fixed it." in text
    assert "not proof" in text


# -- data model ------------------------------------------------------------


def test_start_creates_a_child_without_moving_the_active_session(tmp_path):
    h = Harness(tmp_path)
    parent = h.parent()
    res = h.sup.start(parent, name="Vehicle Return", brief="Map story warps.", todo="Map warps")
    assert res["ok"], res
    child_id = res["session_id"]
    assert h.store.active == parent.harness_session_id
    row = next(r for r in h.store.rows() if r["id"] == child_id)
    assert row["workspace_root"] == "/work/proj"
    assert row["nest"]["parent_id"] == parent.harness_session_id
    assert row["nest"]["name"] == "vehicle-return"
    assert row["nest"]["status"] == "working"
    assert row["nest"]["branch"] == f"nest/vehicle-return-{child_id[:6]}"
    assert row["repo"] == row["nest"]["worktree"]
    assert h.trees == [("/work/proj", row["nest"]["branch"])]
    assert parent.links == [("Map warps", child_id, "vehicle-return")]
    assert res["todos"]["phases"][0]["tasks"][0]["session"] == child_id
    turn = h.turns[-1]
    assert turn["sid"] == child_id and turn["driver"] == "parent" and not turn["resume"]
    assert turn["message"].endswith("Task:\nMap story warps.")
    # The rail reads children through the same store rows.
    assert [c["name"] for c in h.sup.children(parent.harness_session_id)] == ["vehicle-return"]


def test_start_refuses_bad_input_and_grandchildren(tmp_path):
    h = Harness(tmp_path)
    parent = h.parent()
    assert not h.sup.start(parent, name="x", brief="b")["ok"]
    assert not h.sup.start(parent, name="good-name", brief="  ")["ok"]
    child_id = h.sup.start(parent, name="kid", brief="b")["session_id"]
    again = h.sup.start(parent, name="kid", brief="b")
    assert again["name"] == "kid-2"
    grandchild = h.sup.start(FakeRunner(child_id), name="deep", brief="b")
    assert not grandchild["ok"] and "cannot start" in grandchild["error"]


def test_worktree_failure_keeps_the_project_folder(tmp_path):
    h = Harness(tmp_path)

    def broken(repo, branch):
        raise RuntimeError("not a git repository")

    h.sup.svc.add_worktree = broken
    res = h.sup.start(h.parent(), name="no-git", brief="b")
    assert res["ok"] and res["worktree"] == "/work/proj"
    assert "not a git repository" in res["worktree_error"]


def test_create_child_refuses_an_encrypted_parent(tmp_path, monkeypatch):
    h = Harness(tmp_path)
    parent = h.parent()
    monkeypatch.setattr("harness.sessions.seal.is_sealed", lambda sid: sid == parent.harness_session_id)
    res = h.sup.start(parent, name="kid", brief="b")
    assert not res["ok"] and "encrypted" in res["error"]


def test_store_persists_links_and_inbox_across_reload(tmp_path):
    h = Harness(tmp_path)
    parent = h.parent()
    child_id = h.sup.start(parent, name="kid", brief="b")["session_id"]
    h.store.push_nest_note(parent.harness_session_id, {"child_id": child_id, "wake": True})
    h.store.flush()
    again = SessionStore(h.store.path)
    assert again.nest_of(child_id)["parent_id"] == parent.harness_session_id
    assert [r["id"] for r in again.children_of(parent.harness_session_id)] == [child_id]
    assert again.take_nest_notes(parent.harness_session_id)[0]["child_id"] == child_id
    assert again.take_nest_notes(parent.harness_session_id) == []


# -- report-back -----------------------------------------------------------


def test_child_report_lands_in_parent_and_wakes_it(tmp_path):
    h = Harness(tmp_path)
    parent = h.parent()
    pid = parent.harness_session_id
    child_id = h.sup.start(parent, name="full-bag", brief="b")["session_id"]
    h.end_turn(child_id, reply="Gifts drop when the bag is full. Commit abc123.")
    nest = h.store.nest_of(child_id)
    assert nest["status"] == "done"
    assert nest["report"].startswith("Gifts drop")
    assert parent._history[-1]["role"] == "user"
    assert "<nested-session-event>" in parent._history[-1]["content"]
    row = parent._display_transcript[-1]
    assert row["type"] == "session_note" and row["name"] == "full-bag" and row["status"] == "done"
    assert h.persisted == [pid]
    wake = h.turns[-1]
    assert wake == {"sid": pid, "message": "", "resume": True, "driver": "wake"}
    assert h.store.nest_parent_state(pid)["auto_wakes"] == 1
    assert h.store.take_nest_notes(pid) == []


def test_parent_wakes_once_after_the_last_working_child(tmp_path):
    h = Harness(tmp_path)
    parent = h.parent()
    pid = parent.harness_session_id
    a = h.sup.start(parent, name="alpha", brief="a")["session_id"]
    b = h.sup.start(parent, name="beta", brief="b")["session_id"]
    h.end_turn(a, reply="A done.")
    assert not [t for t in h.turns if t["sid"] == pid]
    assert "alpha" in parent._history[-1]["content"]
    h.end_turn(b, reply="B done.")
    wakes = [t for t in h.turns if t["sid"] == pid]
    assert len(wakes) == 1 and wakes[0]["resume"]
    # Both notes sit in the one user message the wake turn answers.
    assert "alpha" in parent._history[-1]["content"] and "beta" in parent._history[-1]["content"]


def test_user_driven_child_turn_reports_but_does_not_wake(tmp_path):
    h = Harness(tmp_path)
    parent = h.parent()
    pid = parent.harness_session_id
    child_id = h.sup.start(parent, name="kid", brief="b")["session_id"]
    h.end_turn(child_id, reply="first")       # parent-driven: wakes
    h.end_turn(pid, reply="parent answer")    # the wake turn ends
    before = len(h.turns)
    h.status[child_id] = "running"
    h.end_turn(child_id, reply="user asked me something")
    assert len(h.turns) == before
    assert parent._display_transcript[-1]["driver"] == "user"
    assert "The user drove this turn" in parent._history[-1]["content"]


def test_busy_parent_keeps_notes_until_its_turn_ends(tmp_path):
    h = Harness(tmp_path)
    parent = h.parent()
    pid = parent.harness_session_id
    child_id = h.sup.start(parent, name="kid", brief="b")["session_id"]
    h.status[pid] = "running"
    h.end_turn(child_id, reply="done while parent worked")
    assert not parent._display_transcript
    assert h.store.nested_rows() and any(r.get("nest_inbox") for r in h.store.nested_rows())
    parent._history.append({"role": "assistant", "content": "parent turn text"})
    h.end_turn(pid)
    assert parent._display_transcript[-1]["type"] == "session_note"
    assert h.turns[-1]["sid"] == pid and h.turns[-1]["resume"]


def test_stopped_child_reports_without_a_wake(tmp_path):
    h = Harness(tmp_path)
    parent = h.parent()
    pid = parent.harness_session_id
    child_id = h.sup.start(parent, name="kid", brief="b")["session_id"]
    h.runners[child_id] = FakeRunner(child_id)
    h.runners[child_id]._stop_holds_idle = True
    h.end_turn(child_id, reply="partial")
    assert h.store.nest_of(child_id)["status"] == "stopped"
    assert parent._display_transcript[-1]["status"] == "stopped"
    assert not [t for t in h.turns if t["sid"] == pid]


def test_failed_turn_and_refused_start_report_failed(tmp_path):
    h = Harness(tmp_path)
    parent = h.parent()
    pid = parent.harness_session_id
    child_id = h.sup.start(parent, name="kid", brief="b")["session_id"]
    h.status[child_id] = "missing"
    h.sup.refused(child_id, "No free session slot")
    assert h.store.nest_of(child_id) == {**h.store.nest_of(child_id), "status": "failed",
                                         "report": "No free session slot"}
    assert h.turns[-1]["sid"] == pid
    other = h.sup.start(parent, name="other", brief="b")["session_id"]
    h.end_turn(other, reply="tried", outcome="failed")
    assert h.store.nest_of(other)["status"] == "failed"


def test_auto_wakes_stop_at_the_cap_and_reset_on_a_user_turn(tmp_path):
    h = Harness(tmp_path)
    parent = h.parent()
    pid = parent.harness_session_id
    h.store.nest_parent_state(pid, auto_wakes=ns.MAX_AUTO_WAKES)
    child_id = h.sup.start(parent, name="kid", brief="b")["session_id"]
    h.end_turn(child_id, reply="r")
    assert not [t for t in h.turns if t["sid"] == pid]
    h.end_turn(pid)  # a turn the user drove
    assert h.store.nest_parent_state(pid)["auto_wakes"] == 0


def test_message_steers_a_busy_child_and_starts_a_turn_on_an_idle_one(tmp_path):
    h = Harness(tmp_path)
    parent = h.parent()
    child_id = h.sup.start(parent, name="kid", brief="b")["session_id"]
    runner = h.runners.setdefault(child_id, FakeRunner(child_id))
    res = h.sup.message(parent, "kid", "also check the shop")
    assert res["delivered"] == "steer" and runner.steers == ["also check the shop"]
    h.status[child_id] = "idle"
    res = h.sup.message(parent, child_id, "next step")
    assert res["delivered"] == "turn" and h.turns[-1]["message"] == "next step"
    assert not h.sup.message(parent, "nobody", "x")["ok"]


def test_boot_reconcile_resumes_children_a_restart_cut(tmp_path):
    h = Harness(tmp_path)
    parent = h.parent()
    child_id = h.sup.start(parent, name="kid", brief="b")["session_id"]
    done_id = h.sup.start(parent, name="done-kid", brief="b")["session_id"]
    h.store.update_nest(done_id, status="done")
    h.turns.clear()
    resumed = h.sup.reconcile_on_boot()
    assert resumed == [child_id]
    assert h.turns[0]["sid"] == child_id and "restarted" in h.turns[0]["message"]
    assert h.store.nest_of(child_id)["status"] == "working"


# -- plan-step links ---------------------------------------------------------


def test_todo_link_round_trips_and_survives_init():
    phases = [TodoPhase("Plan", [TodoItem("Map story warps", "in_progress"), TodoItem("Test bag")])]
    assert link_task_session(phases, "", "c1", "vehicle-return").content == "Map story warps"
    assert link_task_session(phases, "test bag", "c2", "full-bag").content == "Test bag"
    assert link_task_session(phases, "nothing like it", "c3", "x") is None
    raw = phases_to_dicts(phases)
    assert raw[0]["tasks"][0]["session"] == "c1"
    again = phases_from_raw(raw)
    assert again[0]["tasks"][1].session_name == "full-bag" if isinstance(again[0], dict) else again[0].tasks[1].session_name == "full-bag"
    nxt, errors, _op = apply_todo_op(again, {"op": "init", "list": [
        {"phase": "Plan", "items": ["Map story warps", "New step"]},
    ]})
    assert not errors
    assert nxt[0].tasks[0].session == "c1"
    assert nxt[0].tasks[1].session is None


# -- headless driver ---------------------------------------------------------


def test_headless_handler_reads_the_turn_outcome():
    ok = HeadlessHandler()
    ok.wfile.write(b'data: {"kind": "assistant_done", "data": {}}\n\ndata: {"kind": "do')
    ok.wfile.write(b'ne"}\n\n')
    assert ok.outcome == "done"
    bad = HeadlessHandler()
    bad.wfile.write(b'data: {"kind": "error", "data": {"error": "provider 500"}}\n\n')
    assert bad.outcome == "failed" and bad.wfile.error == "provider 500"
    refused = HeadlessHandler()
    refused._send(409, '{"code": "lease_exhausted"}')
    assert refused.outcome == "failed"
