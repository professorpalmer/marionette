"""Regression: workspace_drivers / workspace.json must resolve under the stable
state dir (~/.pmharness/state), with a legacy fallback to ~/.pmharness/.

Root cause (Windows + others): boot restored drivers BEFORE HARNESS_STATE_DIR
was anchored to ~/.pmharness/state. Saves wrote state/workspace_drivers.json;
boot read ~/.pmharness/workspace_drivers.json (missing) and fell through to
enabled_pilots()[0] (often openrouter:z-ai/glm-5.2).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


@pytest.fixture
def srv(monkeypatch, tmp_path):
    """Import harness.server against an isolated state dir (never real home)."""
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    import importlib
    import harness.server as server
    importlib.reload(server)
    return server


def test_resolve_prefers_state_home_file(srv, tmp_path):
    name = "workspace_drivers.json"
    primary = tmp_path / name
    primary.write_text(json.dumps({"__last__": "openrouter:deepseek/deepseek-v4-flash"}), encoding="utf-8")
    assert Path(srv._resolve_existing_state_file(name)) == primary


def test_resolve_legacy_fallback_when_state_anchor_empty(srv, monkeypatch, tmp_path):
    """When HARNESS_STATE_DIR is the stable ~/.pmharness/state equivalent and
    the file is missing there, adopt the legacy root copy."""
    legacy_root = tmp_path / "pmharness"
    state_dir = legacy_root / "state"
    state_dir.mkdir(parents=True)
    legacy_file = legacy_root / "workspace_drivers.json"
    legacy_file.write_text(
        json.dumps({"__last__": "openrouter:deepseek/deepseek-v4-flash"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state_dir))
    monkeypatch.setattr(srv, "_pmharness_root", lambda: str(legacy_root))

    resolved = srv._resolve_existing_state_file("workspace_drivers.json")
    assert Path(resolved) == legacy_file
    assert srv._get_workspace_driver("") == "openrouter:deepseek/deepseek-v4-flash"


def test_resolve_no_legacy_leak_from_isolated_temp(srv, monkeypatch, tmp_path):
    """Test / ephemeral HARNESS_STATE_DIR must NOT read the developer's real
    ~/.pmharness — only the stable-anchor layout may fall back."""
    # Point "legacy" at a decoy that would wrongly win if isolation broke.
    decoy = tmp_path / "decoy-home"
    decoy.mkdir()
    (decoy / "workspace_drivers.json").write_text(
        json.dumps({"__last__": "should-not-leak"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(srv, "_pmharness_root", lambda: str(decoy))
    # tmp_path is HARNESS_STATE_DIR but is NOT decoy/state — no fallback.
    resolved = srv._resolve_existing_state_file("workspace_drivers.json")
    assert Path(resolved) == tmp_path / "workspace_drivers.json"
    assert not os.path.exists(resolved)
    assert srv._get_workspace_driver("") is None


def test_save_writes_state_home_and_get_reads_it(srv, tmp_path):
    repo = str(tmp_path / "repoA")
    os.makedirs(repo, exist_ok=True)
    # Use a non-temp-looking path so _save_workspace_driver does not skip it.
    # On Windows tempfile is under Users/.../AppData/Local/Temp; our tmp_path
    # from pytest is also under Temp, so saves are skipped. Exercise the path
    # helpers + JSON round-trip directly instead.
    drivers = {
        os.path.realpath(repo): "openrouter:deepseek/deepseek-v4-flash",
        "__last__": "openrouter:deepseek/deepseek-v4-flash",
    }
    path = tmp_path / "workspace_drivers.json"
    path.write_text(json.dumps(drivers), encoding="utf-8")
    assert srv._workspace_drivers_path() == str(path)
    assert srv._get_workspace_driver(repo) == "openrouter:deepseek/deepseek-v4-flash"
    assert srv._get_workspace_driver("") == "openrouter:deepseek/deepseek-v4-flash"


def test_state_home_follows_anchored_env(monkeypatch, tmp_path):
    """After setdefault(HARNESS_STATE_DIR, ~/.pmharness/state), _state_home
    must return that path so boot restore and saves share one directory."""
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    import importlib
    import harness.server as server
    importlib.reload(server)
    assert server._state_home() == str(tmp_path / "state")
    assert server._workspace_drivers_path() == str(tmp_path / "state" / "workspace_drivers.json")
