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
        self.tokens: dict = {}
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

    def _run_turn(self, sid, message, *, resume=False, driver="", token=0):
        self.turns.append({"sid": sid, "message": message, "resume": resume, "driver": driver})
        self.tokens[sid] = token
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


def test_a_turn_with_no_reply_has_no_report():
    """Live 2026-11-14: a child stopped before it replied reported its previous turn."""
    history = [
        {"role": "user", "content": "task one"},
        {"role": "assistant", "content": "All three steps ran."},
        {"role": "user", "content": "Run sleep 60."},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
        {"role": "tool", "content": "interrupted"},
    ]
    assert ns.report_from_history(history) == ""
    # A tool result in user role (Anthropic shape) is not a new user message.
    history.append({"role": "user", "content": [{"type": "tool_result", "content": "ok"}]})
    history.insert(3, {"role": "assistant", "content": "Starting the sleep."})
    assert ns.report_from_history(history) == "Starting the sleep."


def test_live_status_prefers_a_running_runner():
    assert ns.live_status("done", "running") == "working"
    assert ns.live_status("stopped", "idle") == "stopped"
    assert ns.live_status("", "missing") == "done"
    # A started turn counts before its runner exists.
    assert ns.live_status("working", "missing", in_flight=True) == "working"
    # A stored "working" with no turn running is dead, never "Working".
    assert ns.live_status("working", "idle") == "stopped"
    assert ns.live_status("working", "missing") == "stopped"


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


def test_no_worktree_means_no_child(tmp_path):
    """Each child writes only in its own worktree, so two children never
    write the same files. A child never falls back to the project folder."""
    h = Harness(tmp_path)

    def broken(repo, branch):
        raise RuntimeError("not a git repository")

    h.sup.svc.add_worktree = broken
    parent = h.parent()
    res = h.sup.start(parent, name="no-git", brief="b")
    assert not res["ok"] and "not a git repository" in res["error"]
    assert h.store.children_of(parent.harness_session_id) == []
    assert h.turns == []


def test_two_children_get_two_worktrees(tmp_path):
    h = Harness(tmp_path)
    parent = h.parent()
    a = h.sup.start(parent, name="same", brief="a")
    b = h.sup.start(parent, name="same", brief="b")
    assert a["worktree"] != b["worktree"] and a["branch"] != b["branch"]
    assert "/work/proj" not in (a["worktree"], b["worktree"])


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
    notes = again.peek_nest_notes(parent.harness_session_id)
    assert notes[0]["child_id"] == child_id
    again.drop_nest_notes(parent.harness_session_id, [ns.note_id(n) for n in notes])
    assert again.peek_nest_notes(parent.harness_session_id) == []


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
    assert row["ts"] > 10**12  # milliseconds, as for every display row
    assert h.persisted == [pid]
    wake = h.turns[-1]
    assert wake == {"sid": pid, "message": "", "resume": True, "driver": "wake"}
    assert h.store.nest_parent_state(pid)["auto_wakes"] == 1
    assert h.store.peek_nest_notes(pid) == []


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
    h.end_turn(child_id, reply="first part done")
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


# -- robustness --------------------------------------------------------------


def test_a_child_whose_turn_died_never_shows_working(tmp_path):
    h = Harness(tmp_path)
    parent = h.parent()
    pid = parent.harness_session_id
    child_id = h.sup.start(parent, name="kid", brief="b")["session_id"]
    # Started, runner not built yet: Working.
    h.status[child_id] = "missing"
    assert h.sup.children(pid)[0]["status"] == "working"
    # The turn thread ends with no finalize_turn (it died). The stored row
    # still says working, but the turn is gone.
    h.sup.turn_exited(child_id, h.tokens[child_id], "failed")
    assert h.sup.children(pid)[0]["status"] == "failed"
    assert parent._display_transcript[-1]["status"] == "failed"
    # A row left "working" by a crash shows stopped in the list.
    h.store.update_nest(child_id, status="working")
    rows = [{"id": child_id, "nest": h.store.nest_of(child_id)}]
    ns.set_supervisor(h.sup)
    try:
        assert ns.with_live_status(rows)[0]["nest"]["status"] == "stopped"
    finally:
        ns.set_supervisor(None)
    assert h.store.nest_of(child_id)["status"] == "working"  # the list copy changed, not the store


def test_turn_exit_after_a_normal_end_reports_nothing_more(tmp_path):
    h = Harness(tmp_path)
    parent = h.parent()
    child_id = h.sup.start(parent, name="kid", brief="b")["session_id"]
    token = h.tokens[child_id]
    h.end_turn(child_id, reply="done")
    notes = [r for r in parent._display_transcript if r["type"] == "session_note"]
    h.sup.turn_exited(child_id, token, "done")
    assert [r for r in parent._display_transcript if r["type"] == "session_note"] == notes
    assert len(notes) == 1


def test_an_old_turn_exit_does_not_touch_the_next_turn(tmp_path):
    h = Harness(tmp_path)
    parent = h.parent()
    pid = parent.harness_session_id
    child_id = h.sup.start(parent, name="kid", brief="b")["session_id"]
    old = h.tokens[child_id]
    h.end_turn(child_id, reply="one")
    h.end_turn(pid, reply="wake answer")
    h.sup.message(parent, child_id, "two")  # a new turn, new token
    assert h.tokens[child_id] != old
    h.status[child_id] = "missing"
    h.sup.turn_exited(child_id, old, "done")  # the first thread ends late
    assert h.sup.children(pid)[0]["status"] == "working"


def test_message_to_a_child_that_is_starting_is_refused(tmp_path):
    h = Harness(tmp_path)
    parent = h.parent()
    child_id = h.sup.start(parent, name="kid", brief="b")["session_id"]
    h.status[child_id] = "missing"
    res = h.sup.message(parent, "kid", "more")
    assert not res["ok"] and "starting" in res["error"]
    assert [t["sid"] for t in h.turns].count(child_id) == 1


def test_a_report_arrives_once_when_the_save_fails(tmp_path):
    h = Harness(tmp_path)
    parent = h.parent()
    pid = parent.harness_session_id
    child_id = h.sup.start(parent, name="kid", brief="b")["session_id"]
    calls = {"n": 0}

    def flaky(sid, runner):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk full")
        h.persisted.append(sid)

    h.sup.svc.persist = flaky
    h.end_turn(child_id, reply="the report")
    # The save failed: the note stays in the inbox, the row is in memory once.
    assert len(h.store.peek_nest_notes(pid)) == 1
    assert sum(1 for r in parent._display_transcript if r["type"] == "session_note") == 1
    h.sup.deliver(pid)
    assert h.store.peek_nest_notes(pid) == []
    assert sum(1 for r in parent._display_transcript if r["type"] == "session_note") == 1
    assert parent._history[-1]["content"].count("<nested-session-event>") == 1


def test_a_report_arrives_once_after_a_crash_before_the_inbox_drop(tmp_path):
    h = Harness(tmp_path)
    parent = h.parent()
    pid = parent.harness_session_id
    child_id = h.sup.start(parent, name="kid", brief="b")["session_id"]
    real_drop = h.store.drop_nest_notes
    h.store.drop_nest_notes = lambda *a, **k: None  # the app dies after the save
    h.end_turn(child_id, reply="the report")
    assert len(h.store.peek_nest_notes(pid)) == 1
    h.store.drop_nest_notes = real_drop
    h.status[pid] = "idle"  # after the restart nothing runs
    h.sup.deliver(pid)  # boot or switch delivers again
    assert h.store.peek_nest_notes(pid) == []
    assert sum(1 for r in parent._display_transcript if r["type"] == "session_note") == 1


def test_a_busy_parent_gets_the_report_when_it_frees(tmp_path, monkeypatch):
    monkeypatch.setattr(ns, "RETRY_SECONDS", 0.02)
    h = Harness(tmp_path)
    parent = h.parent()
    pid = parent.harness_session_id
    child_id = h.sup.start(parent, name="kid", brief="b")["session_id"]
    parent._busy.acquire()  # the parent's turn is closing and still holds its lock
    h.end_turn(child_id, reply="report")
    assert not parent._display_transcript
    parent._busy.release()
    for _ in range(1000):  # up to 10 s on a loaded machine; ends at the first row
        # The row lands first. The inbox drop follows on the same thread.
        if parent._display_transcript and not h.store.peek_nest_notes(pid):
            break
        threading.Event().wait(0.01)
    assert sum(1 for r in parent._display_transcript if r["type"] == "session_note") == 1
    assert h.store.peek_nest_notes(pid) == []


def test_a_child_worktree_starts_from_the_project_codegraph_index(tmp_path):
    project = tmp_path / "proj"
    (project / ".codegraph").mkdir(parents=True)
    (project / ".codegraph" / "codegraph.db").write_bytes(b"index")
    (project / ".codegraph" / "codegraph.lock").write_text("123")
    tree = tmp_path / "tree"
    tree.mkdir()
    assert ns.seed_codegraph(str(project), str(tree))
    assert (tree / ".codegraph" / "codegraph.db").read_bytes() == b"index"
    assert not (tree / ".codegraph" / "codegraph.lock").exists()
    # The first staleness check sees the copy as old and runs one incremental pass.
    assert os.path.getmtime(tree / ".codegraph") == 0
    assert not ns.seed_codegraph(str(tmp_path / "no-index"), str(tree))


def test_the_workspace_read_finds_the_open_child_worktree(tmp_path):
    h = Harness(tmp_path)
    tree = tmp_path / "tree"
    tree.mkdir()
    h.sup.svc.add_worktree = lambda repo, branch: {"path": str(tree), "branch": branch}
    parent = h.parent()
    child_id = h.sup.start(parent, name="kid", brief="b")["session_id"]
    ns.set_supervisor(h.sup)
    try:
        h.store.switch(parent.harness_session_id)
        assert ns.active_child_tree() == ""
        h.store.switch(child_id)
        assert ns.active_child_tree() == str(tree)
    finally:
        ns.set_supervisor(None)
