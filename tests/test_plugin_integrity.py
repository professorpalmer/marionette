"""Agent plugin integrity stamp + permission list (hermetic)."""

from __future__ import annotations

pytest_plugins = ["tests.test_agent_plugins"]

import json
from pathlib import Path

import pytest

from harness.agent_plugins import AgentPluginError, PLUGIN_SCHEMA_V1, load_agent_plugin
from harness.plugin_registry import (
    enable_plugin,
    install_from_path,
    verify_integrity_stamp,
)
from tests.test_agent_plugins import _valid_package, _write_json


def test_stamp_match_install_and_enable(plugins_home: Path, tmp_path: Path) -> None:
    source = _valid_package(tmp_path / "src")
    record = install_from_path(str(source))
    assert record.stamp_ok is True
    assert record.sha256
    assert record.permissions == []
    enabled = enable_plugin(record.id)
    assert enabled.enabled is True
    assert enabled.stamp_ok is True


def test_stamp_mismatch_rejects_enable(plugins_home: Path, tmp_path: Path) -> None:
    source = _valid_package(tmp_path / "src")
    record = install_from_path(str(source))
    installed = Path(record.path)
    (installed / "plugin.json").write_text(
        (installed / "plugin.json").read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )
    with pytest.raises(AgentPluginError, match="mismatch"):
        enable_plugin(record.id)
    with pytest.raises(AgentPluginError, match="mismatch"):
        verify_integrity_stamp(installed)


def test_manifest_permissions_surface(tmp_path: Path) -> None:
    root = _valid_package(tmp_path / "plugin")
    manifest = json.loads((root / "plugin.json").read_text(encoding="utf-8"))
    manifest["permissions"] = ["network", "filesystem"]
    _write_json(root / "plugin.json", manifest)
    package = load_agent_plugin(root, tmp_path / "data")
    assert package.permissions == ("network", "filesystem")


def test_rejects_unknown_permission(tmp_path: Path) -> None:
    root = _valid_package(tmp_path / "plugin")
    _write_json(
        root / "plugin.json",
        {
            "$schema": PLUGIN_SCHEMA_V1,
            "name": "portable.test",
            "permissions": ["marketplace"],
        },
    )
    with pytest.raises(AgentPluginError, match="permissions"):
        load_agent_plugin(root, tmp_path / "data")
