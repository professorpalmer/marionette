"""Characterization tests for workspace API peel (forget/get/symbols/workspaces)."""
from __future__ import annotations

import json
from types import SimpleNamespace

from harness.api.workspace import (
    WorkspaceServices,
    get_workspace,
    get_workspace_symbols,
    get_workspaces,
    post_workspace_forget,
    post_workspaces_create,
    post_workspaces_switch,
)


def _svc(cfg, tmp_path, *, forget_fn=None):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    ws_json = tmp_path / "workspace.json"
    cleared = {"n": 0}

    class _Ws:
        @staticmethod
        def switch_workspace(repo, name, allow_dirty=False):
            return {"switched": name, "allow_dirty": allow_dirty}

        @staticmethod
        def create_workspace(repo, name, branch=None):
            return {"created": name, "branch": branch}

        @staticmethod
        def list_workspaces(repo):
            return [{"name": "default"}]

    return WorkspaceServices(
        cfg=cfg,
        parse_bool=lambda v: bool(v) if not isinstance(v, str) else v.lower() in ("1", "true"),
        ws=_Ws,
        paths_same_workspace=lambda a, b: os_path_same(a, b),
        forget_recent_workspace=forget_fn or (lambda p: ["kept"]),
        clear_active_codegraph=lambda: cleared.__setitem__("n", cleared["n"] + 1),
        get_codegraph_status=lambda r: "ready" if r else "none",
        workspace_json_path=lambda: str(ws_json),
        ensure_home_workspace=lambda: str(home),
        home_workspace_path=lambda: str(home),
        is_app_install_root=lambda p: False,
        diag=lambda *a: None,
    ), cleared, ws_json, home


def os_path_same(a, b):
    import os
    try:
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))
    except Exception:
        return a == b


def test_workspace_forget_requires_path(tmp_path):
    cfg = SimpleNamespace(repo="")
    svc, _, _, _ = _svc(cfg, tmp_path)
    assert post_workspace_forget({}, svc)[0] == 400


def test_workspace_forget_clears_active(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    cfg = SimpleNamespace(repo=str(repo))
    svc, cleared, _, _ = _svc(
        cfg, tmp_path, forget_fn=lambda p: []
    )
    code, payload = post_workspace_forget({"path": str(repo)}, svc)
    assert code == 200
    assert payload["cleared_active"] is True
    assert cfg.repo == ""
    assert cleared["n"] == 1


def test_get_workspace_includes_home(tmp_path):
    cfg = SimpleNamespace(repo="")
    svc, _, ws_json, home = _svc(cfg, tmp_path)
    ws_json.write_text(json.dumps({"recents": []}), encoding="utf-8")
    code, payload = get_workspace(svc)
    assert code == 200
    assert payload["home"] == str(home)
    assert str(home) in payload["recents"]


def test_workspaces_switch_create_list(tmp_path):
    cfg = SimpleNamespace(repo="/r")
    svc, _, _, _ = _svc(cfg, tmp_path)
    code, sw = post_workspaces_switch({"name": "a", "allow_dirty": True}, svc)
    assert code == 200 and sw["switched"] == "a" and sw["allow_dirty"] is True
    code2, cr = post_workspaces_create({"name": "b", "branch": "main"}, svc)
    assert code2 == 200 and cr["created"] == "b"
    code3, listing = get_workspaces(svc)
    assert code3 == 200 and listing[0]["name"] == "default"


def test_workspace_symbols_no_repo(tmp_path):
    cfg = SimpleNamespace(repo="")
    svc, _, _, _ = _svc(cfg, tmp_path)
    code, payload = get_workspace_symbols("Foo", svc)
    assert code == 200
    assert payload["symbols"] == []
    assert payload["status"] == "unsupported"


def test_workspace_symbols_query(monkeypatch, tmp_path):
    import sys
    import types

    repo = tmp_path / "proj"
    repo.mkdir()
    cfg = SimpleNamespace(repo=str(repo))
    svc, _, _, _ = _svc(cfg, tmp_path)

    pkg = types.ModuleType("puppetmaster")
    pkg.__path__ = []  # type: ignore[attr-defined]
    cg = types.ModuleType("puppetmaster.codegraph")

    def _query(search="", cwd=None, limit=20):
        return {
            "ok": True,
            "stdout": json.dumps([
                {
                    "node": {
                        "name": "Foo",
                        "kind": "class",
                        "filePath": "a.py",
                        "startLine": 3,
                    }
                }
            ]),
        }

    cg.codegraph_available = lambda: True  # type: ignore[attr-defined]
    cg.codegraph_ready = lambda r: True  # type: ignore[attr-defined]
    cg.codegraph_query = _query  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "puppetmaster", pkg)
    monkeypatch.setitem(sys.modules, "puppetmaster.codegraph", cg)

    code, empty = get_workspace_symbols("", svc)
    assert code == 200 and empty["status"] == "ready" and empty["symbols"] == []
    code2, hit = get_workspace_symbols("Foo", svc)
    assert code2 == 200 and hit["symbols"][0]["name"] == "Foo"
