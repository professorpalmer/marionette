"""Agent plugin integrity stamp + capability consent (hermetic)."""

from __future__ import annotations

pytest_plugins = ["tests.test_agent_plugins"]

import json
from pathlib import Path

import pytest

from harness.agent_plugins import AgentPluginError, PLUGIN_SCHEMA_V1, load_agent_plugin
from harness.plugin_capabilities import (
    capability_set_hash,
    parse_requested_capabilities,
)
from harness.plugin_registry import (
    consent_plugin_capabilities,
    enable_plugin,
    install_from_path,
    verify_integrity_stamp,
)
from tests.test_agent_plugins import _valid_package, _write_json


def _package_requesting(root: Path, caps: list) -> Path:
    source = _valid_package(root)
    manifest = json.loads((source / "plugin.json").read_text(encoding="utf-8"))
    manifest["extensions"] = {"marionette": {"requested_capabilities": caps}}
    _write_json(source / "plugin.json", manifest)
    return source


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


def test_unknown_capability_id_rejected() -> None:
    with pytest.raises(AgentPluginError, match="unknown"):
        parse_requested_capabilities(["not-a-cap"])
    with pytest.raises(AgentPluginError, match="unknown"):
        parse_requested_capabilities(["fs", "camera"])


def test_unknown_capability_id_rejected_on_install(
    plugins_home: Path, tmp_path: Path
) -> None:
    source = _package_requesting(tmp_path / "src", ["camera"])
    with pytest.raises(AgentPluginError, match="unknown"):
        install_from_path(str(source))


def test_requested_capabilities_default_empty(
    plugins_home: Path, tmp_path: Path
) -> None:
    assert parse_requested_capabilities(None) == frozenset()
    assert parse_requested_capabilities([]) == frozenset()
    source = _valid_package(tmp_path / "src")
    record = install_from_path(str(source))
    assert record.requested_capabilities == []
    assert record.capability_set_hash == capability_set_hash([])
    enabled = enable_plugin(record.id)
    assert enabled.enabled is True


def test_capability_set_hash_changes_when_set_changes() -> None:
    empty = capability_set_hash([])
    fs_only = capability_set_hash(["fs"])
    fs_shell = capability_set_hash(["shell", "fs"])
    assert empty != fs_only
    assert fs_only != fs_shell
    assert fs_shell == capability_set_hash(["fs", "shell"])
    assert fs_only == capability_set_hash(["fs", "fs"])


def test_enable_without_consent_of_requested_caps_fails(
    plugins_home: Path, tmp_path: Path
) -> None:
    source = _package_requesting(tmp_path / "src", ["fs", "shell"])
    record = install_from_path(str(source))
    assert record.requested_capabilities == ["fs", "shell"]
    assert record.capability_set_hash == capability_set_hash(["fs", "shell"])
    assert record.consented_capabilities == []
    with pytest.raises(AgentPluginError, match="consent"):
        enable_plugin(record.id)
    consent_plugin_capabilities(record.id, ["fs"])
    with pytest.raises(AgentPluginError, match="consent"):
        enable_plugin(record.id)


def test_enable_after_consent_succeeds(
    plugins_home: Path, tmp_path: Path
) -> None:
    source = _package_requesting(tmp_path / "src", ["fs", "shell"])
    record = install_from_path(str(source))
    first = consent_plugin_capabilities(record.id, ["fs"])
    second = consent_plugin_capabilities(record.id, ["fs", "shell"])
    assert first.consented_hash != second.consented_hash
    assert second.consented_hash == capability_set_hash(["fs", "shell"])
    enabled = enable_plugin(record.id)
    assert enabled.enabled is True
    assert enabled.stamp_ok is True
    assert enabled.consented_capabilities == ["fs", "shell"]


def test_stamp_mismatch_rejected_independently_of_consent(
    plugins_home: Path, tmp_path: Path
) -> None:
    source = _package_requesting(tmp_path / "src", ["fs"])
    record = install_from_path(str(source))
    consent_plugin_capabilities(record.id, ["fs"])
    installed = Path(record.path)
    (installed / "plugin.json").write_text(
        (installed / "plugin.json").read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )
    with pytest.raises(AgentPluginError, match="mismatch"):
        enable_plugin(record.id)
    with pytest.raises(AgentPluginError, match="mismatch"):
        verify_integrity_stamp(installed)
