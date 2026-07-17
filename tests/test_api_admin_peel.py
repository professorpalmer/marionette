"""Characterization tests for commands/hooks/checkpoints/git API peels."""
from __future__ import annotations

from types import SimpleNamespace

from harness.api.commands import CommandsServices, get_commands, post_commands_render
from harness.api.hooks import (
    HooksServices,
    get_hooks,
    post_hooks_add,
    post_hooks_remove,
    post_hooks_update,
)
from harness.api.checkpoints import (
    CheckpointServices,
    get_checkpoints,
    get_checkpoints_diff,
    post_checkpoints_restore,
    post_checkpoints_snapshot,
)
from harness.api.git import (
    GitServices,
    get_git_branches,
    get_git_diff,
    get_git_status,
    post_git_connect,
    post_git_device_poll,
    post_git_disconnect,
)


# --- commands ----------------------------------------------------------------


class _FakeCmd:
    def __init__(self, name, description="d", scope="global"):
        self.name = name
        self.description = description
        self.scope = scope


class _FakeCommands:
    def render(self, name, args, repo=None):
        if name == "missing":
            return None
        return f"rendered:{name}:{args}:{repo}"

    def list(self, repo=None):
        return [_FakeCmd("hello")]


def test_commands_render_missing_name():
    svc = CommandsServices(commands=_FakeCommands(), cfg=SimpleNamespace(repo="/r"))
    code, payload = post_commands_render({}, svc)
    assert code == 400


def test_commands_render_unknown():
    svc = CommandsServices(commands=_FakeCommands(), cfg=SimpleNamespace(repo="/r"))
    code, payload = post_commands_render({"name": "missing"}, svc)
    assert code == 404


def test_commands_render_and_list():
    svc = CommandsServices(commands=_FakeCommands(), cfg=SimpleNamespace(repo="/r"))
    code, payload = post_commands_render({"name": "hello", "args": "x"}, svc)
    assert code == 200
    assert payload["prompt"].startswith("rendered:hello:x")
    code2, listing = get_commands(None, svc)
    assert code2 == 200
    assert listing["commands"][0]["name"] == "hello"


# --- hooks -------------------------------------------------------------------


def test_hooks_add_update_remove(monkeypatch):
    store = []

    import harness.hooks as hk

    def _save(h):
        store[:] = list(h)

    monkeypatch.setattr(hk, "ALLOWED_EVENTS", ["sessionStart", "preRun"])
    monkeypatch.setattr(hk, "get_hooks", lambda: store)
    monkeypatch.setattr(hk, "save_hooks", _save)

    code, bad = post_hooks_add({"event": "nope", "command": "echo"})
    assert code == 400
    code, empty = post_hooks_add({"event": "preRun", "command": "  "})
    assert code == 400

    code, hook = post_hooks_add({"event": "preRun", "command": "echo hi"})
    assert code == 200
    assert hook["enabled"] is True
    hid = hook["id"]

    svc = HooksServices(
        parse_bool=lambda v: (
            bool(v) if not isinstance(v, str) else v.lower() in ("1", "true", "yes")
        )
    )
    code, updated = post_hooks_update({"id": hid, "enabled": False}, svc)
    assert code == 200
    assert updated["enabled"] is False

    code, missing = post_hooks_update({"id": "nope"}, svc)
    assert code == 404

    code2, listing = get_hooks()
    assert code2 == 200
    assert listing["events"] == ["sessionStart", "preRun"]
    assert len(listing["hooks"]) == 1

    code3, rem = post_hooks_remove({"id": hid})
    assert code3 == 200 and rem["ok"] is True
    assert store == []


# --- checkpoints -------------------------------------------------------------


def test_checkpoints_no_workspace(tmp_path):
    svc = CheckpointServices(
        cfg=SimpleNamespace(repo=str(tmp_path / "missing")),
        get_active_session_id=lambda: "",
    )
    code, payload = post_checkpoints_restore({"id": "x"}, svc)
    assert code == 400
    assert "workspace" in payload["error"].lower()
    code2, empty = get_checkpoints(svc)
    assert code2 == 200 and empty == []


def test_checkpoints_snapshot_restore_diff(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    class _Store:
        def __init__(self, *a, **k):
            pass

        def snapshot(self, **k):
            return "ckpt1"

        def restore(self, cid, **k):
            return {"ok": True, "id": cid} if cid == "ckpt1" else {"ok": False, "error": "gone"}

        def list(self, **k):
            return [{"id": "ckpt1"}]

        def diff(self, cid, **k):
            return {"ok": True, "diff": "x"} if cid == "ckpt1" else {"ok": False, "error": "no"}

    monkeypatch.setattr("harness.checkpoints.CheckpointStore", _Store)

    svc = CheckpointServices(
        cfg=SimpleNamespace(repo=str(repo)),
        get_active_session_id=lambda: "sid",
    )
    code, snap = post_checkpoints_snapshot({"label": "manual"}, svc)
    assert code == 200 and snap["id"] == "ckpt1"
    code2, listing = get_checkpoints(svc)
    assert code2 == 200 and listing[0]["id"] == "ckpt1"
    code3, restored = post_checkpoints_restore({"id": "ckpt1"}, svc)
    assert code3 == 200 and restored["ok"] is True
    code4, diff = get_checkpoints_diff("ckpt1", svc)
    assert code4 == 200 and diff["ok"] is True
    code5, bad = post_checkpoints_restore({}, svc)
    assert code5 == 400


# --- git ---------------------------------------------------------------------


def test_git_connect_invalid_method():
    code, payload = post_git_connect({"method": "ftp"})
    assert code == 400


def test_git_device_poll_missing_code():
    code, payload = post_git_device_poll({})
    assert code == 400


def test_git_status_branches_diff_disconnect(monkeypatch):
    monkeypatch.setattr(
        "harness.git_provision.get_status",
        lambda: {"connected": True, "method": "gh"},
    )
    monkeypatch.setattr("harness.git_provision.delete_connection", lambda: None)
    monkeypatch.setattr(
        "harness.git_workspace.workspace_status",
        lambda cfg_repo, q: {"repo": q, "ok": True},
    )
    monkeypatch.setattr(
        "harness.git_workspace.workspace_branches",
        lambda cfg_repo, q: {"branches": ["main"]},
    )
    monkeypatch.setattr(
        "harness.git_workspace.workspace_diff",
        lambda cfg_repo, q, file, staged=False: {"diff": "d", "staged": staged},
    )

    svc = GitServices(cfg=SimpleNamespace(repo="/proj"))
    code, st = get_git_status(None, svc)
    assert code == 200 and st["connected"] is True
    code2, wst = get_git_status("other", svc)
    assert code2 == 200 and wst["repo"] == "other"
    code3, br = get_git_branches("r", svc)
    assert code3 == 200 and br["branches"] == ["main"]
    code4, df = get_git_diff("r", "a.py", True, svc)
    assert code4 == 200 and df["staged"] is True
    code5, disc = post_git_disconnect()
    assert code5 == 200 and disc["connected"] is True
