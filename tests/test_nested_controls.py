from __future__ import annotations

"""Parent controls over nested sessions: archive, model, fork, read.

Design: ``docs/design/nested-sessions.md``, section "Parent controls".
"""

import os
import subprocess
from types import SimpleNamespace

import pytest

from harness import nested_sessions as ns
from test_nested_sessions import FakeRunner, Harness


def _git(cwd, *args) -> str:
    out = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True)
    return out.stdout.strip()


def git_project(tmp_path):
    """A real repository with one commit on main."""
    repo = tmp_path / "proj"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "T")
    (repo / ".gitignore").write_text(".codegraph/\n")
    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


class GitHarness(Harness):
    """The supervisor over real git worktrees and a fake archive path."""

    def __init__(self, tmp_path) -> None:
        super().__init__(tmp_path)
        self.repo = git_project(tmp_path)
        self.archived: list = []
        self.dropped: list = []
        self.sup.svc.add_worktree = self._real_worktree
        self.sup.svc.archive_row = self._archive_row
        self.sup.svc.drop_runner = self.dropped.append

    def _real_worktree(self, repo, branch):
        path = os.path.join(str(self.repo.parent), "trees", branch.replace("/", "_"))
        if _git(repo, "branch", "--list", branch):
            _git(repo, "worktree", "add", "-q", path, branch)
        else:
            _git(repo, "worktree", "add", "-q", "-b", branch, path, "HEAD")
        _git(repo, "worktree", "lock", "--reason", "test", path)
        self.trees.append((repo, branch))
        return {"path": path, "branch": branch}

    def _archive_row(self, sid, archived):
        self.archived.append((sid, archived))
        self.store.archive(sid, archived)
        return 200, {"ok": True}

    def child(self, name="kid"):
        parent = self.parent(root=str(self.repo))
        res = self.sup.start(parent, name=name, brief="b")
        assert res["ok"], res
        sid = res["session_id"]
        self.end_turn(sid, reply="Done.")
        return parent, sid, res["worktree"], res["branch"]


def _row(h, sid):
    return next(r for r in h.store.rows() if r["id"] == sid)


# -- unit 1: archive and unarchive ------------------------------------------


def test_archive_refuses_a_working_child(tmp_path):
    h = GitHarness(tmp_path)
    parent = h.parent(root=str(h.repo))
    sid = h.sup.start(parent, name="busy", brief="b")["session_id"]
    res = h.sup.archive(parent, "busy")
    assert not res["ok"] and "working" in res["error"]
    assert h.archived == [] and h.dropped == []
    assert not _row(h, sid).get("archived")


def test_archive_releases_a_clean_worktree_and_unarchive_adds_it_back(tmp_path):
    h = GitHarness(tmp_path)
    parent, sid, tree, branch = h.child()
    # Ignored files (the CodeGraph seed) do not keep the worktree.
    os.makedirs(os.path.join(tree, ".codegraph"), exist_ok=True)
    with open(os.path.join(tree, ".codegraph", "codegraph.db"), "w") as fh:
        fh.write("index")
    res = h.sup.archive(parent, "kid")
    assert res["ok"], res
    assert res["worktree"] == "released"
    assert not os.path.isdir(tree)
    assert tree not in _git(h.repo, "worktree", "list")
    # The branch stays. It has no commit that main lacks.
    assert _git(h.repo, "branch", "--list", branch)
    assert h.archived == [(sid, True)] and h.dropped == [sid]
    row = _row(h, sid)
    assert row["archived"] and row["nest"]["released"] is True
    assert [c["archived"] for c in h.sup.children(parent.harness_session_id)] == [True]
    # A message to an archived child is refused.
    msg = h.sup.message(parent, "kid", "more")
    assert not msg["ok"] and "unarchive" in msg["error"]

    back = h.sup.unarchive(parent, "kid")
    assert back["ok"], back
    assert os.path.isdir(tree)
    assert _git(tree, "rev-parse", "--abbrev-ref", "HEAD") == branch
    assert h.archived[-1] == (sid, False)
    row = _row(h, sid)
    assert not row["archived"] and row["nest"]["released"] is False
    assert ns.run_repo(row) == tree


def test_archive_keeps_a_worktree_with_commits_the_parent_lacks(tmp_path):
    h = GitHarness(tmp_path)
    parent, sid, tree, branch = h.child()
    with open(os.path.join(tree, "b.txt"), "w") as fh:
        fh.write("b\n")
    _git(tree, "add", "b.txt")
    _git(tree, "commit", "-q", "-m", "child work")
    res = h.sup.archive(parent, "kid")
    assert res["ok"], res
    assert res["worktree"] == "kept"
    assert "1 commit" in res["reason"]
    assert os.path.isdir(tree)
    row = _row(h, sid)
    assert row["archived"] and not row["nest"].get("released")
    # Unarchive of a kept worktree adds nothing.
    trees = list(h.trees)
    assert h.sup.unarchive(parent, "kid")["ok"]
    assert h.trees == trees


def test_archive_keeps_a_worktree_with_uncommitted_files(tmp_path):
    h = GitHarness(tmp_path)
    parent, sid, tree, _ = h.child()
    with open(os.path.join(tree, "draft.txt"), "w") as fh:
        fh.write("x\n")
    res = h.sup.archive(parent, "kid")
    assert res["ok"] and res["worktree"] == "kept"
    assert "uncommitted" in res["reason"]
    assert os.path.isfile(os.path.join(tree, "draft.txt"))


def test_archive_is_idempotent_and_unarchive_of_a_live_child_is_a_no_op(tmp_path):
    h = GitHarness(tmp_path)
    parent, sid, _, _ = h.child()
    assert h.sup.unarchive(parent, "kid")["ok"]
    assert h.archived == []
    assert h.sup.archive(parent, "kid")["ok"]
    assert h.sup.archive(parent, "kid")["ok"]
    assert h.archived == [(sid, True)]


def test_archive_keeps_the_runner_of_the_child_the_user_views(tmp_path):
    h = GitHarness(tmp_path)
    parent, sid, _, _ = h.child()
    h.store.switch(sid)
    assert h.sup.archive(parent, "kid")["ok"]
    assert h.dropped == []


def test_http_archive_of_a_child_uses_the_supervisor_rules(tmp_path):
    from harness.api import sessions as api
    h = GitHarness(tmp_path)
    parent = h.parent(root=str(h.repo))
    sid = h.sup.start(parent, name="busy", brief="b")["session_id"]
    svc = SimpleNamespace(parse_bool=bool, sessions=h.store, sessions_state_dir=lambda: str(tmp_path))
    ns.set_supervisor(h.sup)
    try:
        code, body = api.post_sessions_archive({"session": sid, "archived": True}, svc)
        assert code == 409 and "working" in body["error"]
        h.end_turn(sid, reply="ok")
        code, body = api.post_sessions_archive({"session": sid, "archived": True}, svc)
        assert code == 200 and body["worktree"] == "released"
        code, body = api.post_sessions_archive({"session": sid, "archived": False}, svc)
        assert code == 200 and not _row(h, sid)["archived"]
    finally:
        ns.set_supervisor(None)


# -- unit 2: a model and an effort for each child ------------------------------


PICKER = ["openrouter:deepseek/deepseek-v4.1-flash", "anthropic:claude-sonnet-5-5"]


def _with_picker(h):
    h.sup.svc.picker_models = lambda: list(PICKER)
    h.sup.svc.resolve_model = lambda spec: spec if spec in PICKER else (
        "anthropic:claude-sonnet-5-5" if spec == "claude-sonnet-5-5" else None)
    return h


def test_start_pins_a_picker_model_and_an_effort(tmp_path):
    h = _with_picker(Harness(tmp_path))
    parent = h.parent()
    h.store.pilot_preferences(parent.harness_session_id, updates={"driver": "openai:gpt-6", "reasoning_effort": "high"})
    res = h.sup.start(parent, name="cheap", brief="b", model="claude-sonnet-5-5", reasoning_effort="low")
    assert res["ok"], res
    assert res["model"] == "anthropic:claude-sonnet-5-5" and res["reasoning_effort"] == "low"
    prefs = h.store.pilot_preferences(res["session_id"])
    assert prefs["driver"] == "anthropic:claude-sonnet-5-5"
    assert prefs["reasoning_effort"] == "low"
    # The parent keeps its own choice.
    assert h.store.pilot_preferences(parent.harness_session_id)["driver"] == "openai:gpt-6"
    assert h.sup.children(parent.harness_session_id)[0]["model"] == "anthropic:claude-sonnet-5-5"


def test_start_without_a_model_keeps_the_parent_model(tmp_path):
    h = _with_picker(Harness(tmp_path))
    parent = h.parent()
    h.store.pilot_preferences(parent.harness_session_id, updates={"driver": "openai:gpt-6", "reasoning_effort": "high"})
    res = h.sup.start(parent, name="same", brief="b")
    prefs = h.store.pilot_preferences(res["session_id"])
    assert prefs["driver"] == "openai:gpt-6" and prefs["reasoning_effort"] == "high"


def test_an_unknown_model_or_effort_fails_closed(tmp_path):
    h = _with_picker(Harness(tmp_path))
    parent = h.parent()
    res = h.sup.start(parent, name="bad", brief="b", model="gpt-9-imaginary")
    assert not res["ok"]
    assert "gpt-9-imaginary" in res["error"] and PICKER[0] in res["error"]
    bad_effort = h.sup.start(parent, name="bad", brief="b", reasoning_effort="turbo")
    assert not bad_effort["ok"] and "low" in bad_effort["error"]
    # No row, no worktree, no turn for a refused start.
    assert h.store.children_of(parent.harness_session_id) == [] and h.turns == [] and h.trees == []
    # A model with no allowlist to check against is refused, not trusted.
    h.sup.svc.resolve_model = None
    assert not h.sup.start(parent, name="bad", brief="b", model=PICKER[0])["ok"]


def test_the_server_checks_a_child_model_against_the_picker_list(monkeypatch):
    from harness import model_visibility, server
    monkeypatch.setattr(model_visibility, "enabled_pilots", lambda: list(PICKER))
    assert server._nested_resolve_model(PICKER[0]) == PICKER[0]
    assert server._nested_resolve_model("deepseek/deepseek-v4.1-flash") == PICKER[0]
    assert server._nested_resolve_model("gpt-9-imaginary") is None
    assert server._nested_picker_models() == PICKER


# -- unit 3: fork context -----------------------------------------------------


def _parent_with_history(h):
    parent = h.parent()
    parent._history = [
        {"role": "user", "content": "Fix the vehicle return place."},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "r1", "type": "function",
                                                             "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "r1", "content": "warp table at 0x40"},
        {"role": "assistant", "content": "The warp table is at 0x40."},
        {"role": "user", "content": "Split the map work into a child."},
        # The turn that calls nested_session start: its tool call has no result yet.
        {"role": "assistant", "content": "", "tool_calls": [{"id": "n1", "type": "function",
                                                             "function": {"name": "nested_session", "arguments": "{}"}}]},
    ]
    return parent


def test_fork_context_starts_the_child_with_the_parent_conversation(tmp_path):
    import copy
    from harness.sessions import load_transcript
    h = Harness(tmp_path)
    h.sup.svc.state_dir = lambda: str(tmp_path)
    parent = _parent_with_history(h)
    before = copy.deepcopy(parent._history)
    res = h.sup.start(parent, name="maps", brief="Map the warps.", context="fork")
    assert res["ok"], res
    sid = res["session_id"]
    saved = load_transcript(str(tmp_path), sid)
    history = saved["history"] if isinstance(saved, dict) else saved
    # Cut at the last tool-complete boundary, as /api/session/fork does.
    assert history == before[:5]
    assert res["forked_messages"] == 5
    nest = h.store.nest_of(sid)
    assert nest["context"] == "fork" and nest["forked_messages"] == 5
    # The parent history does not change, and the copy is not shared.
    assert parent._history == before
    history[0]["content"] = "changed"
    assert parent._history[0]["content"] == "Fix the vehicle return place."
    turn = h.turns[-1]
    assert "copied from the parent session" in turn["message"]
    assert turn["message"].endswith("Task:\nMap the warps.")


def test_fresh_context_copies_nothing_and_a_bad_context_is_refused(tmp_path):
    from harness.sessions import load_transcript
    h = Harness(tmp_path)
    h.sup.svc.state_dir = lambda: str(tmp_path)
    parent = _parent_with_history(h)
    res = h.sup.start(parent, name="fresh", brief="b")
    assert load_transcript(str(tmp_path), res["session_id"]) == []
    assert "copied from the parent" not in h.turns[-1]["message"]
    bad = h.sup.start(parent, name="bad", brief="b", context="clone")
    assert not bad["ok"] and "fork" in bad["error"]
    assert len(h.store.children_of(parent.harness_session_id)) == 1
