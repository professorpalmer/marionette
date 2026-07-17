"""Characterization tests for codegraph POST API peel."""
from __future__ import annotations

from types import SimpleNamespace

from harness.api.codegraph import (
    CodegraphServices,
    post_codegraph_apply_excludes,
    post_codegraph_reindex,
)


def _svc(repo, *, alive=False, reason=None):
    state = {"reindex": 0, "index": 0, "reason": reason}

    return CodegraphServices(
        cfg=SimpleNamespace(repo=repo),
        index_alive=lambda: alive,
        reindex_bg=lambda r: state.__setitem__("reindex", state["reindex"] + 1),
        index_bg=lambda r: state.__setitem__("index", state["index"] + 1),
        get_status=lambda r: "ready",
        get_reason=lambda: state["reason"],
        set_reason=lambda r: state.__setitem__("reason", r),
    ), state


def test_codegraph_reindex_no_workspace():
    svc, _ = _svc(None)
    assert post_codegraph_reindex(svc)[0] == 400


def test_codegraph_reindex_already_indexing(tmp_path):
    svc, state = _svc(str(tmp_path), alive=True)
    code, payload = post_codegraph_reindex(svc)
    assert code == 200
    assert payload["note"] == "already indexing"
    assert state["reindex"] == 0


def test_codegraph_reindex_starts(tmp_path):
    svc, state = _svc(str(tmp_path), alive=False, reason="ok")
    code, payload = post_codegraph_reindex(svc)
    assert code == 200 and payload["status"] == "ready"
    assert state["reindex"] == 1


def test_codegraph_apply_excludes(monkeypatch, tmp_path):
    svc, state = _svc(str(tmp_path), alive=False)

    monkeypatch.setattr(
        "harness.codegraph_preflight.merge_codegraph_excludes",
        lambda repo, extra_excludes=None: {"exclude": list(extra_excludes or [])},
    )
    monkeypatch.setattr(
        "harness.codegraph_preflight.child_exclude_globs",
        lambda names: [f"**/{n}/**" for n in names],
    )
    monkeypatch.setattr(
        "harness.codegraph_preflight.DEFAULT_ASSET_EXCLUDES",
        ["node_modules", "dist"],
    )

    code, payload = post_codegraph_apply_excludes(
        {"excludes": ["vendor", "**/tmp/**"]}, svc
    )
    assert code == 200 and payload["ok"] is True
    assert state["index"] == 1
    assert "Asset excludes" in (state["reason"] or "")
    assert payload["exclude_count"] == 2
