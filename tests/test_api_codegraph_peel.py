"""Characterization tests for codegraph API peel (GET status + POST admin)."""
from __future__ import annotations

from types import SimpleNamespace

from harness.api.codegraph import (
    CodegraphServices,
    get_codegraph,
    post_codegraph_apply_excludes,
    post_codegraph_reindex,
)


def _svc(repo, *, alive=False, reason=None, live="ready", indexed=True):
    state = {
        "reindex": 0,
        "index": 0,
        "reason": reason,
        "live": live,
        "cache": {},
    }

    return CodegraphServices(
        cfg=SimpleNamespace(repo=repo),
        index_alive=lambda: alive,
        reindex_bg=lambda r: state.__setitem__("reindex", state["reindex"] + 1),
        index_bg=lambda r: state.__setitem__("index", state["index"] + 1),
        get_status=lambda r: "ready",
        get_reason=lambda: state["reason"],
        set_reason=lambda r: state.__setitem__("reason", r),
        get_live_status=lambda: state["live"],
        set_live_status=lambda s: state.__setitem__("live", s),
        get_preflight=lambda: None,
        get_suggested_action=lambda: None,
        puppetmaster_available=lambda: True,
        codegraph_indexed=lambda r: indexed,
        status_cache_get=lambda r: state["cache"].get(r),
        status_cache_put=lambda r, p: state["cache"].__setitem__(
            r, (1e18, p)
        ),
        status_cache_pop=lambda r: state["cache"].pop(r, None),
        fail_until_for=lambda r: 0.0,
        puppetmaster_cmd=lambda *a: ["echo"],
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


def test_get_codegraph_no_repo():
    svc, _ = _svc(None)
    code, payload = get_codegraph(svc)
    assert code == 200
    assert payload["status"] == "none"
    assert payload["repo"] == ""
    assert "reason" not in payload


def test_get_codegraph_needs_scope(tmp_path):
    svc, state = _svc(str(tmp_path), live="needs_scope", reason="too big")
    code, payload = get_codegraph(svc)
    assert code == 200
    assert payload["status"] == "needs_scope"
    assert payload["reason"] == "too big"
    assert "preflight" in payload
