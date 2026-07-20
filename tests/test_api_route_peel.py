"""Characterization + smoke for the post-v0.9.81 API route peel.

Locks status codes and key payload fields for skills/rules/memory, worktrees,
and terminals handlers so a future move cannot silently change shapes.
"""
from __future__ import annotations

from types import SimpleNamespace

from harness.api.skills import (
    SkillsServices,
    get_memory,
    get_rules,
    get_skills,
    post_memory_add,
    post_memory_propose_accept,
    post_memory_remove,
    post_rules_add,
    post_skills_add,
    post_skills_approve,
    post_skills_update,
)
from harness.api.terminals import (
    TerminalServices,
    post_terminal_create,
    post_terminal_kill,
    post_terminal_resize,
    post_terminal_write,
)
from harness.api.worktrees import (
    WorktreeServices,
    get_worktrees,
    post_worktrees_add,
    post_worktrees_max,
    post_worktrees_remove,
)
from harness.api import auth as auth_api


# ---------------------------------------------------------------------------
# Skills / rules / memory
# ---------------------------------------------------------------------------


class _FakeSkill:
    def __init__(self, name="n", slug="n", state="active"):
        self.name = name
        self.slug = slug
        self.description = "d"
        self.body = "b"
        self.state = state
        self.source = "manual"
        self.used_count = 0
        self.supersedes = ""


class _FakeSkills:
    def __init__(self):
        self._items = {}

    def save(self, sk):
        self._items[sk.slug] = sk

    def list(self):
        return list(self._items.values())

    def set_state(self, slug, state):
        sk = self._items.get(slug)
        if sk:
            sk.state = state
        return sk

    def update(self, slug, **kw):
        sk = self._items.get(slug)
        if not sk:
            return None
        for k, v in kw.items():
            if v is not None:
                setattr(sk, k, v)
        return sk

    def remove(self, slug):
        return self._items.pop(slug, None) is not None


class _FakeRules:
    def __init__(self):
        self._items = {}

    def add(self, rule):
        self._items[rule.slug] = rule

    def list(self):
        return list(self._items.values())

    def set_state(self, slug, state):
        r = self._items.get(slug)
        if r:
            r.state = state
        return bool(r)

    def update(self, slug, **kw):
        r = self._items.get(slug)
        if not r:
            return None
        for k, v in kw.items():
            if v is not None:
                setattr(r, k, v)
        return r

    def remove(self, slug):
        return self._items.pop(slug, None) is not None


class _FakeEntry:
    def __init__(self, text, category="general"):
        self.id = "e1"
        self.text = text
        self.category = category
        self.created_at = "t"
        self.source = "user"


class _FakeMemory:
    def __init__(self):
        self._entries = []

    def add(self, text, category="general", source="user"):
        e = _FakeEntry(text, category)
        e.source = source
        self._entries.append(e)
        return e

    def remove(self, entry_id):
        before = len(self._entries)
        self._entries = [e for e in self._entries if e.id != entry_id]
        return len(self._entries) < before

    def list(self):
        return list(self._entries)

    def total_chars(self):
        return sum(len(e.text) for e in self._entries)


def _skills_svc():
    pilot = SimpleNamespace(
        distill=lambda: {"status": "ok"},
        accept_memory_proposal=lambda pid: {"ok": True, "id": pid},
        dismiss_memory_proposal=lambda pid: {"ok": False},
    )
    return SkillsServices(
        skills=_FakeSkills(),
        rules=_FakeRules(),
        memory=_FakeMemory(),
        get_pilot=lambda: pilot,
        memory_char_limit=1000,
    )


def test_skills_add_requires_name():
    code, payload = post_skills_add({}, _skills_svc())
    assert code == 400
    assert "name" in payload["error"]


def test_skills_add_and_list():
    svc = _skills_svc()
    code, payload = post_skills_add(
        {"name": "Audit Prior", "description": "d", "body": "b"}, svc
    )
    assert code == 200
    assert payload["ok"] is True
    assert payload["slug"]
    code2, listing = get_skills(svc)
    assert code2 == 200
    assert isinstance(listing, list)
    assert listing[0]["name"] == "Audit Prior"


def test_skills_update_missing_slug():
    code, payload = post_skills_update({"name": "x"}, _skills_svc())
    assert code == 400


def test_skills_update_not_found():
    code, payload = post_skills_update({"slug": "nope", "name": "x"}, _skills_svc())
    assert code == 404


def test_skills_approve_unknown():
    code, payload = post_skills_approve({"slug": "missing"}, _skills_svc())
    assert code == 200
    assert payload["ok"] is False


def test_rules_add_and_list():
    svc = _skills_svc()
    code, payload = post_rules_add({"text": "Prefer CodeGraph"}, svc)
    assert code == 200
    assert payload["ok"] is True
    code2, listing = get_rules(svc)
    assert code2 == 200
    assert listing[0]["text"] == "Prefer CodeGraph"


def test_memory_add_remove_list():
    svc = _skills_svc()
    code, payload = post_memory_add({"text": "remember me", "category": "prefs"}, svc)
    assert code == 200
    assert payload["id"] == "e1"
    code2, listing = get_memory(svc)
    assert code2 == 200
    assert listing["total_chars"] == len("remember me")
    assert listing["limit"] == 1000
    code3, rem = post_memory_remove({"id": "e1"}, svc)
    assert code3 == 200 and rem["ok"] is True


def test_memory_propose_accept_missing_id():
    code, payload = post_memory_propose_accept({}, _skills_svc())
    assert code == 400
    assert payload["ok"] is False


def test_memory_propose_accept_ok():
    code, payload = post_memory_propose_accept({"id": "p1"}, _skills_svc())
    assert code == 200
    assert payload["ok"] is True


# ---------------------------------------------------------------------------
# Worktrees
# ---------------------------------------------------------------------------


def test_worktrees_add_rejects_bad_branch(monkeypatch):
    svc = WorktreeServices(cfg=SimpleNamespace(repo="/tmp/repo"), parse_bool=bool)
    code, payload = post_worktrees_add({"branch": "-evil"}, svc)
    assert code == 400
    assert "invalid" in payload["error"]


def test_worktrees_add_ok(monkeypatch):
    # Handlers do `from .. import worktrees` inside each fn — patch that module.
    monkeypatch.setattr(
        "harness.worktrees.add_worktree",
        lambda repo, branch, base: {"path": "/wt", "branch": branch},
    )
    monkeypatch.setattr("harness.worktrees.get_max_worktrees", lambda: 25)
    monkeypatch.setattr("harness.worktrees.cleanup_old_worktrees", lambda *a: None)
    monkeypatch.setattr(
        "harness.worktrees.list_worktrees", lambda repo: [{"path": "/wt"}]
    )

    svc = WorktreeServices(cfg=SimpleNamespace(repo="C:/proj"), parse_bool=bool)
    code, payload = post_worktrees_add({"branch": "feature/x", "base": "HEAD"}, svc)
    assert code == 200
    assert payload["branch"] == "feature/x"
    code2, listing = get_worktrees(svc)
    assert code2 == 200
    assert listing["max"] == 25
    assert listing["worktrees"][0]["path"] == "/wt"


def test_worktrees_remove_missing_path():
    svc = WorktreeServices(cfg=SimpleNamespace(repo="/r"), parse_bool=bool)
    code, payload = post_worktrees_remove({}, svc)
    assert code == 400
    assert "path" in payload["error"]


def test_worktrees_max_invalid():
    svc = WorktreeServices(cfg=SimpleNamespace(repo="/r"), parse_bool=bool)
    code, payload = post_worktrees_max({"max": "nope"}, svc)
    assert code == 400
    assert "Invalid" in payload["error"]


# ---------------------------------------------------------------------------
# Terminals
# ---------------------------------------------------------------------------


class _FakeSess:
    def __init__(self):
        self.id = "t1"
        self._cwd = "/home"
        self.writes = []
        self.resized = None

    def write(self, data):
        self.writes.append(data)

    def resize(self, rows, cols):
        self.resized = (rows, cols)


class _FakePty:
    def __init__(self):
        self.reaped = False
        self.killed = []
        self._sess = _FakeSess()

    def reap(self):
        self.reaped = True

    def create(self, cwd="", cols=80, rows=24):
        assert self.reaped
        return self._sess

    def get(self, tid):
        return self._sess if tid == self._sess.id else None

    def kill(self, tid):
        self.killed.append(tid)


def test_terminal_create_write_resize_kill():
    pty = _FakePty()
    svc = TerminalServices(cfg=SimpleNamespace(repo="/repo"), pty=pty)
    code, payload = post_terminal_create({"cols": 100, "rows": 40}, svc)
    assert code == 200
    assert payload["id"] == "t1"
    assert payload["cwd"] == "/home"
    code2, w = post_terminal_write({"id": "t1", "data": "ls\n"}, svc)
    assert code2 == 200 and w["ok"] is True
    assert pty._sess.writes == ["ls\n"]
    code_r, r = post_terminal_resize({"id": "t1", "rows": 30, "cols": 120}, svc)
    assert code_r == 200 and r["ok"] is True
    assert pty._sess.resized == (30, 120)
    code3, k = post_terminal_kill({"id": "t1"}, svc)
    assert code3 == 200 and k["ok"] is True
    assert pty.killed == ["t1"]


def test_terminal_create_clamps_zero_dims():
    pty = _FakePty()
    created = []
    orig_create = pty.create

    def _create(**kw):
        created.append(kw)
        return orig_create(**kw)

    pty.create = _create  # type: ignore[method-assign]
    svc = TerminalServices(cfg=SimpleNamespace(repo="/repo"), pty=pty)
    code, payload = post_terminal_create({"cols": 0, "rows": 0}, svc)
    assert code == 200
    assert payload["id"] == "t1"
    assert created and created[0]["cols"] == 80 and created[0]["rows"] == 24


def test_terminal_write_missing():
    pty = _FakePty()
    svc = TerminalServices(cfg=SimpleNamespace(repo=None), pty=pty)
    code, payload = post_terminal_write({"id": "nope", "data": "x"}, svc)
    assert code == 404


# ---------------------------------------------------------------------------
# Auth ownership surface
# ---------------------------------------------------------------------------


def test_auth_module_reexports_pool_handlers():
    assert callable(auth_api.post_auth_pools)
    assert callable(auth_api.get_auth_pools)
    assert callable(auth_api.post_auth_cursor_cli_status)
