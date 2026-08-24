"""Agent Plugins v1 portable package + registry tests (hermetic)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.agent_plugins import (
    MCP_SCHEMA_V1,
    PLUGIN_SCHEMA_V1,
    AgentPluginError,
    compute_plugin_content_sha256,
    load_agent_plugin,
)
from harness.mcp_manager import McpManager
from harness.plugin_registry import (
    disable_plugin,
    discover_plugins,
    enable_plugin,
    install_from_path,
    list_enabled_mcp_servers,
    list_enabled_plugin_skills,
    plugin_record_to_dict,
    portable_skill_namespace,
)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _manifest(**overrides: object) -> dict:
    value = {"$schema": PLUGIN_SCHEMA_V1, "name": "portable.test"}
    value.update(overrides)
    return value


def _write_skill(root: Path, directory: str = "summarize", **fields: object) -> Path:
    skill_dir = root / "skills" / directory
    skill_dir.mkdir(parents=True)
    meta = {"name": directory, "description": "Summarizes reports."}
    meta.update(fields)
    lines = ["---"]
    for key, value in meta.items():
        if isinstance(value, dict):
            lines.append(f"{key}:")
            for nested_key, nested_value in value.items():
                lines.append(f"  {nested_key}: {nested_value}")
        elif isinstance(value, list):
            lines.append(f"{key}: {json.dumps(value)}")
        else:
            lines.append(f"{key}: {value}")
    lines.extend(["---", "Instructions.", ""])
    (skill_dir / "SKILL.md").write_text("\n".join(lines), encoding="utf-8")
    return skill_dir


def _valid_package(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _write_json(root / "plugin.json", _manifest(version="1.2.3", description="Demo"))
    _write_skill(root)
    _write_json(
        root / "mcp.json",
        {
            "$schema": MCP_SCHEMA_V1,
            "mcpServers": {
                "worker": {
                    "type": "stdio",
                    "command": "python",
                    "args": ["${PLUGIN_ROOT}/server.py"],
                    "env": {"CACHE": "${PLUGIN_DATA}/cache"},
                },
                "remote": {
                    "type": "streamable-http",
                    "url": "https://example.test/mcp",
                },
                "sse-remote": {
                    "type": "sse",
                    "url": "https://example.test/sse",
                },
            },
        },
    )
    return root


@pytest.fixture
def plugins_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
    return state


def test_loads_manifest_skill_and_stdio_server(tmp_path: Path) -> None:
    root = _valid_package(tmp_path / "plugin")
    package = load_agent_plugin(root, tmp_path / "data")

    assert package.name == "portable.test"
    assert package.version == "1.2.3"
    assert len(package.skills) == 1
    assert package.skills[0].name == "summarize"
    assert package.skills[0].body == "Instructions."
    assert "worker" in package.mcp_servers
    server = package.mcp_servers["worker"]
    assert server["command"] == "python"
    assert server["args"] == [str(root.resolve() / "server.py")]
    assert server["env"]["PLUGIN_ROOT"] == str(root.resolve())
    assert server["env"]["PLUGIN_DATA"] == str((tmp_path / "data").resolve())
    assert server["env"]["CACHE"] == str((tmp_path / "data").resolve() / "cache")
    # Remote transports are skipped with diagnostics
    assert "remote" not in package.mcp_servers
    assert "sse-remote" not in package.mcp_servers
    scopes = {d.scope for d in package.diagnostics}
    assert "mcp:remote" in scopes
    assert "mcp:sse-remote" in scopes


@pytest.mark.parametrize(
    "manifest",
    [
        [],
        {"name": "valid-name"},
        {"$schema": "https://example.test/schema.json", "name": "valid-name"},
        {"$schema": PLUGIN_SCHEMA_V1},
        {"$schema": PLUGIN_SCHEMA_V1, "name": "Bad_Name"},
        {"$schema": PLUGIN_SCHEMA_V1, "name": "a--b"},
        {"$schema": PLUGIN_SCHEMA_V1, "name": "a", "keywords": [1]},
        {"$schema": PLUGIN_SCHEMA_V1, "name": "a", "author": {"handle": "x"}},
    ],
)
def test_rejects_invalid_manifests(tmp_path: Path, manifest: object) -> None:
    _write_json(tmp_path / "plugin.json", manifest)
    with pytest.raises(AgentPluginError):
        load_agent_plugin(tmp_path, tmp_path / "data")


def test_symlink_escape_is_isolated_to_component(tmp_path: Path) -> None:
    root = tmp_path / "plugin"
    root.mkdir()
    _write_json(root / "plugin.json", _manifest())
    _write_skill(root)
    outside = tmp_path / "outside.json"
    _write_json(outside, {"$schema": MCP_SCHEMA_V1, "mcpServers": {}})
    (root / "mcp.json").symlink_to(outside)

    package = load_agent_plugin(root, tmp_path / "data")

    assert len(package.skills) == 1
    assert package.mcp_servers == {}
    assert any(d.scope == "mcp" for d in package.diagnostics)


def test_default_disabled_registers_nothing(
    plugins_home: Path, tmp_path: Path
) -> None:
    source = _valid_package(tmp_path / "src-plugin")
    record = install_from_path(str(source))
    assert record.enabled is False
    assert list_enabled_plugin_skills() == []
    assert list_enabled_mcp_servers() == {}

    mcp = McpManager(config_path=str(tmp_path / "native-mcp.json"))
    assert mcp.effective_config() == {}


def test_enable_mounts_namespaced_skills_and_mcp(
    plugins_home: Path, tmp_path: Path
) -> None:
    source = _valid_package(tmp_path / "src-plugin")
    record = install_from_path(str(source))
    enabled = enable_plugin(record.id)
    assert enabled.enabled is True

    skills = list_enabled_plugin_skills()
    assert len(skills) == 1
    ns = portable_skill_namespace(record.id)
    assert skills[0].name == f"{ns}:summarize"
    assert skills[0].description == "Summarizes reports."
    assert skills[0].body == "Instructions."

    servers = list_enabled_mcp_servers()
    assert set(servers) == {f"{ns}__worker"}
    assert servers[f"{ns}__worker"]["command"] == "python"
    assert "PLUGIN_ROOT" in servers[f"{ns}__worker"]["env"]
    assert "PLUGIN_DATA" in servers[f"{ns}__worker"]["env"]

    native_path = tmp_path / "native-mcp.json"
    _write_json(
        native_path,
        {
            "mcpServers": {
                "github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"]},
            }
        },
    )
    mcp = McpManager(config_path=str(native_path))
    effective = mcp.effective_config()
    assert "github" in effective
    assert f"{ns}__worker" in effective
    # Native wins on collision
    colliding = f"{ns}__worker"
    _write_json(
        native_path,
        {"mcpServers": {colliding: {"command": "native-wins"}}},
    )
    mcp2 = McpManager(config_path=str(native_path))
    assert mcp2.effective_config()[colliding]["command"] == "native-wins"

    disable_plugin(record.id)
    assert list_enabled_plugin_skills() == []
    assert list_enabled_mcp_servers() == {}


def test_install_requires_absolute_path(plugins_home: Path, tmp_path: Path) -> None:
    _valid_package(tmp_path / "src-plugin")
    with pytest.raises(AgentPluginError, match="absolute"):
        install_from_path("src-plugin")


def test_remote_only_plugin_enable_contributes_no_mcp(
    plugins_home: Path, tmp_path: Path
) -> None:
    root = tmp_path / "remote-only"
    root.mkdir()
    _write_json(root / "plugin.json", _manifest(name="remote.only"))
    _write_json(
        root / "mcp.json",
        {
            "$schema": MCP_SCHEMA_V1,
            "mcpServers": {
                "remote": {
                    "type": "streamable-http",
                    "url": "https://example.test/mcp",
                }
            },
        },
    )
    record = install_from_path(str(root))
    enable_plugin(record.id)
    assert list_enabled_mcp_servers() == {}
    package = load_agent_plugin(
        Path(record.path), plugins_home / "plugin-data" / record.namespace
    )
    assert any("not supported" in d.message for d in package.diagnostics)


def test_extensions_marionette_capabilities_survives_load(tmp_path: Path) -> None:
    root = tmp_path / "plugin"
    root.mkdir()
    _write_json(
        root / "plugin.json",
        _manifest(
            name="portable.depth",
            version="0.1.0",
            description="depth plugin",
            extensions={
                "marionette": {
                    "capabilities": ["adaptive-depth"],
                    "task_types": ["micro", "standard"],
                }
            },
            # Unknown top-level field must still be stripped.
            capabilities=["should-be-stripped"],
        ),
    )
    _write_skill(root)
    package = load_agent_plugin(root, tmp_path / "data")
    assert "capabilities" not in package.manifest
    marionette = package.manifest["extensions"]["marionette"]
    assert marionette["capabilities"] == ["adaptive-depth"]
    assert marionette["task_types"] == ["micro", "standard"]


def test_manifest_permissions_surface_on_package(tmp_path: Path) -> None:
    root = _valid_package(tmp_path / "plugin")
    _write_json(
        root / "plugin.json",
        _manifest(permissions=["network", "filesystem"]),
    )
    package = load_agent_plugin(root, tmp_path / "data")
    assert package.permissions == ("network", "filesystem")


@pytest.mark.parametrize(
    "permissions",
    [
        ["sudo"],
        ["network", "sudo"],
        ["network", "network"],
    ],
)
def test_rejects_invalid_permissions(tmp_path: Path, permissions: list[str]) -> None:
    root = _valid_package(tmp_path / "plugin")
    _write_json(root / "plugin.json", _manifest(permissions=permissions))
    with pytest.raises(AgentPluginError, match="permissions"):
        load_agent_plugin(root, tmp_path / "data")


def test_install_writes_content_stamp_and_enable_loads(
    plugins_home: Path, tmp_path: Path
) -> None:
    source = _valid_package(tmp_path / "src-plugin")
    record = install_from_path(str(source))
    assert record.content_sha256
    assert record.content_sha256 == compute_plugin_content_sha256(Path(record.path))

    payload = plugin_record_to_dict(record)
    assert payload["content_sha256"] == record.content_sha256
    assert payload["permissions"] == []

    enabled = enable_plugin(record.id)
    assert enabled.enabled is True
    assert list_enabled_plugin_skills()


def test_stamp_mismatch_rejects_enable_and_warns_on_discover(
    plugins_home: Path, tmp_path: Path
) -> None:
    source = _valid_package(tmp_path / "src-plugin")
    record = install_from_path(str(source))
    plugin_root = Path(record.path)
    (plugin_root / "skills" / "summarize" / "SKILL.md").write_text(
        (plugin_root / "skills" / "summarize" / "SKILL.md").read_text(encoding="utf-8")
        + "\n# tampered\n",
        encoding="utf-8",
    )

    with pytest.raises(AgentPluginError, match="integrity"):
        enable_plugin(record.id)

    assert list_enabled_plugin_skills() == []

    discovered = discover_plugins()
    assert len(discovered) == 1
    row = discovered[0]
    assert any(d["scope"] == "integrity" for d in row.diagnostics)
    assert "integrity" in row.error or any(
        "integrity" in d["message"] for d in row.diagnostics
    )

