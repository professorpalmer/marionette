"""Hermetic tests for CodeGraph huge-workspace preflight."""
from __future__ import annotations

import json
from pathlib import Path

import harness.codegraph_preflight as pf


def test_small_tree_verdict_ok(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text("x = 1\n", encoding="utf-8")
    result = pf.preflight_workspace(str(tmp_path))
    assert result["verdict"] == "ok"
    assert result["indexable_files"] >= 1
    assert result["files_seen"] >= 1


def test_ashita_like_scope_recommended(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(pf, "SCOPE_BYTES_THRESHOLD", 1000)
    monkeypatch.setattr(pf, "SCOPE_FILES_THRESHOLD", 50)
    monkeypatch.setattr(pf, "MIN_INDEXABLE_FOR_SCOPE", 5)

    pol = tmp_path / "polplugins"
    pol.mkdir()
    for i in range(80):
        (pol / f"blob_{i}.DAT").write_bytes(b"x" * 100)

    addons = tmp_path / "addons"
    addons.mkdir()
    for i in range(10):
        (addons / f"addon_{i}.lua").write_text(f"-- addon {i}\n", encoding="utf-8")

    result = pf.preflight_workspace(str(tmp_path))
    assert result["verdict"] == "scope_recommended"
    assert "addons" in result["suggested_roots"]
    assert "polplugins" in result["suggested_excludes"]
    assert "addons" in result["reason"]


def test_unlikely_when_no_indexable(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(pf, "SCOPE_BYTES_THRESHOLD", 100)
    monkeypatch.setattr(pf, "SCOPE_FILES_THRESHOLD", 10)
    monkeypatch.setattr(pf, "MIN_INDEXABLE_FOR_SCOPE", 5)

    dump = tmp_path / "assets"
    dump.mkdir()
    for i in range(20):
        (dump / f"a_{i}.DAT").write_bytes(b"y" * 50)

    result = pf.preflight_workspace(str(tmp_path))
    assert result["verdict"] == "unlikely"
    assert result["indexable_files"] == 0
    assert "almost no" in result["reason"].lower() or "huge" in result["reason"].lower()


def test_merge_codegraph_excludes_adds_lua_and_assets(tmp_path: Path):
    cfg = pf.merge_codegraph_excludes(str(tmp_path))
    includes = cfg.get("include") or []
    excludes = cfg.get("exclude") or []
    assert "**/*.lua" in includes
    assert "**/*.luau" in includes
    assert "**/polplugins/**" in excludes
    assert "**/*.DAT" in excludes

    path = tmp_path / ".codegraph" / "config.json"
    assert path.is_file()
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert "**/*.lua" in on_disk["include"]


def test_ensure_lua_includes_idempotent(tmp_path: Path):
    pf.merge_codegraph_excludes(str(tmp_path), extra_excludes=[])
    assert pf.ensure_lua_includes(str(tmp_path)) is False
    cfg_path = tmp_path / ".codegraph" / "config.json"
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    data["include"] = [g for g in data["include"] if "lua" not in g]
    cfg_path.write_text(json.dumps(data), encoding="utf-8")
    assert pf.ensure_lua_includes(str(tmp_path)) is True
    data2 = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert "**/*.lua" in data2["include"]


def test_child_exclude_globs():
    assert pf.child_exclude_globs(["polplugins", "logs"]) == [
        "**/polplugins/**",
        "**/logs/**",
    ]
