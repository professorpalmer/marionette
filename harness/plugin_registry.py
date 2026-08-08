"""Installed Agent Plugins v1 packages under ~/.pmharness/plugins.

Discover, install-from-path, and enable/disable portable packages. Default is
disabled until explicitly enabled. Skills and MCP configs are namespaced so
they never overwrite SkillStore files or collide with native mcp.json.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .agent_plugins import (
    AgentPluginDiagnostic,
    AgentPluginError,
    AgentPluginPackage,
    load_agent_plugin,
    read_agent_plugin_manifest,
)
from .secure_files import restrict_to_owner
from .diag import note as _diag

_ENABLED_FILENAME = "enabled.json"
_INSTALL_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$")
_lock = threading.RLock()


def _pmharness_root() -> Path:
    return Path(os.path.expanduser("~/.pmharness"))


def plugins_dir() -> Path:
    """Return the plugins install root (honors HARNESS_STATE_DIR for tests)."""
    explicit = (os.environ.get("HARNESS_STATE_DIR") or "").strip()
    if explicit:
        return Path(explicit) / "plugins"
    return _pmharness_root() / "plugins"


def plugin_data_root() -> Path:
    """Writable per-plugin data parent (honors HARNESS_STATE_DIR)."""
    explicit = (os.environ.get("HARNESS_STATE_DIR") or "").strip()
    if explicit:
        return Path(explicit) / "plugin-data"
    return _pmharness_root() / "plugin-data"


def portable_skill_namespace(key: str) -> str:
    """Return a readable, collision-resistant namespace for a portable plugin."""
    slug = "".join(
        ch if ch.isascii() and (ch.isalnum() or ch in "_-") else "-"
        for ch in key.lower()
    )
    slug = slug.strip("-_") or "plugin"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]
    return f"agent-plugin-{slug}-{digest}"


def _enabled_path() -> Path:
    return plugins_dir() / _ENABLED_FILENAME


def _read_enabled() -> List[str]:
    path = _enabled_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []
    if isinstance(data, dict):
        raw = data.get("enabled", [])
    elif isinstance(data, list):
        raw = data
    else:
        return []
    if not isinstance(raw, list):
        return []
    out: List[str] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out


def _write_enabled(enabled: List[str]) -> None:
    root = plugins_dir()
    root.mkdir(parents=True, exist_ok=True)
    path = _enabled_path()
    payload = {"enabled": list(enabled)}
    import tempfile

    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(root), prefix="enabled_")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        os.replace(tmp_path, str(path))
        if not restrict_to_owner(str(path)):
            _diag("secure_files.restrict_failed", msg=str(path))
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise


def _sanitize_install_id(name: str) -> str:
    slug = re.sub(r"[^a-z0-9._-]+", "-", (name or "").strip().lower()).strip("-._")
    slug = slug[:64] or "plugin"
    if _INSTALL_ID_RE.fullmatch(slug) is None:
        slug = "plugin"
    return slug


@dataclass(frozen=True)
class PluginSkillRecord:
    """Namespaced skill contributed by an enabled portable plugin."""

    name: str
    description: str
    body: str
    plugin_id: str
    namespace: str
    source_name: str


@dataclass
class PluginRecord:
    """Discovered installed package (enabled or not)."""

    id: str
    name: str
    version: str
    description: str
    path: str
    enabled: bool
    namespace: str
    skill_count: int = 0
    mcp_count: int = 0
    diagnostics: List[Dict[str, str]] = field(default_factory=list)
    error: str = ""


def _diag_dicts(
    diagnostics: Tuple[AgentPluginDiagnostic, ...]
) -> List[Dict[str, str]]:
    return [{"scope": d.scope, "message": d.message} for d in diagnostics]


def discover_plugins() -> List[PluginRecord]:
    """List installed packages under the plugins directory (best-effort)."""
    root = plugins_dir()
    if not root.exists():
        return []
    enabled = set(_read_enabled())
    records: List[PluginRecord] = []
    try:
        children = sorted(root.iterdir(), key=lambda p: p.name)
    except OSError:
        return []
    for child in children:
        if not child.is_dir():
            continue
        if child.name.startswith("."):
            continue
        plugin_json = child / "plugin.json"
        if not plugin_json.exists() and not plugin_json.is_symlink():
            continue
        plugin_id = child.name
        namespace = portable_skill_namespace(plugin_id)
        try:
            package = load_agent_plugin(child, plugin_data_root() / namespace)
            records.append(
                PluginRecord(
                    id=plugin_id,
                    name=package.name,
                    version=package.version,
                    description=package.description,
                    path=str(package.root),
                    enabled=plugin_id in enabled,
                    namespace=namespace,
                    skill_count=len(package.skills),
                    mcp_count=len(package.mcp_servers),
                    diagnostics=_diag_dicts(package.diagnostics),
                )
            )
        except AgentPluginError as exc:
            try:
                manifest, diagnostics = read_agent_plugin_manifest(child)
                records.append(
                    PluginRecord(
                        id=plugin_id,
                        name=str(manifest.get("name") or plugin_id),
                        version=str(manifest.get("version") or ""),
                        description=str(manifest.get("description") or ""),
                        path=str(child.resolve(strict=False)),
                        enabled=plugin_id in enabled,
                        namespace=namespace,
                        diagnostics=_diag_dicts(diagnostics),
                        error=str(exc),
                    )
                )
            except Exception as inner:
                records.append(
                    PluginRecord(
                        id=plugin_id,
                        name=plugin_id,
                        version="",
                        description="",
                        path=str(child),
                        enabled=plugin_id in enabled,
                        namespace=namespace,
                        error=str(inner) if inner else str(exc),
                    )
                )
        except Exception as exc:
            records.append(
                PluginRecord(
                    id=plugin_id,
                    name=plugin_id,
                    version="",
                    description="",
                    path=str(child),
                    enabled=plugin_id in enabled,
                    namespace=namespace,
                    error=str(exc),
                )
            )
    return records


def install_from_path(source: str) -> PluginRecord:
    """Copy an absolute plugin package path into the plugins dir (disabled)."""
    raw = (source or "").strip()
    if not raw:
        raise AgentPluginError("install path is required")
    src = Path(raw).expanduser()
    if not src.is_absolute():
        raise AgentPluginError("install path must be absolute")
    if not src.exists() or not src.is_dir():
        raise AgentPluginError("install path must be an existing directory")
    # Validate before copying so a bad package never lands in the registry.
    staging_ns = portable_skill_namespace("_install_probe")
    package = load_agent_plugin(src, plugin_data_root() / staging_ns)
    install_id = _sanitize_install_id(package.name)
    dest = plugins_dir() / install_id
    with _lock:
        plugins_dir().mkdir(parents=True, exist_ok=True)
        if dest.exists():
            raise AgentPluginError(f"plugin already installed: {install_id}")
        try:
            shutil.copytree(str(src), str(dest), symlinks=False)
        except OSError as exc:
            raise AgentPluginError(f"failed to install plugin: {exc}") from exc
        # Ensure default-disabled: remove from enabled if somehow present.
        enabled = [x for x in _read_enabled() if x != install_id]
        _write_enabled(enabled)
    namespace = portable_skill_namespace(install_id)
    loaded = load_agent_plugin(dest, plugin_data_root() / namespace)
    return PluginRecord(
        id=install_id,
        name=loaded.name,
        version=loaded.version,
        description=loaded.description,
        path=str(loaded.root),
        enabled=False,
        namespace=namespace,
        skill_count=len(loaded.skills),
        mcp_count=len(loaded.mcp_servers),
        diagnostics=_diag_dicts(loaded.diagnostics),
    )


def enable_plugin(plugin_id: str) -> PluginRecord:
    """Mark an installed plugin enabled."""
    plugin_id = (plugin_id or "").strip()
    if not plugin_id:
        raise AgentPluginError("plugin id is required")
    path = plugins_dir() / plugin_id
    if not path.is_dir():
        raise AgentPluginError(f"plugin not found: {plugin_id}")
    namespace = portable_skill_namespace(plugin_id)
    package = load_agent_plugin(path, plugin_data_root() / namespace)
    with _lock:
        enabled = _read_enabled()
        if plugin_id not in enabled:
            enabled.append(plugin_id)
            _write_enabled(enabled)
    return PluginRecord(
        id=plugin_id,
        name=package.name,
        version=package.version,
        description=package.description,
        path=str(package.root),
        enabled=True,
        namespace=namespace,
        skill_count=len(package.skills),
        mcp_count=len(package.mcp_servers),
        diagnostics=_diag_dicts(package.diagnostics),
    )


def disable_plugin(plugin_id: str) -> PluginRecord:
    """Mark an installed plugin disabled."""
    plugin_id = (plugin_id or "").strip()
    if not plugin_id:
        raise AgentPluginError("plugin id is required")
    path = plugins_dir() / plugin_id
    namespace = portable_skill_namespace(plugin_id)
    record_error = ""
    package: Optional[AgentPluginPackage] = None
    if path.is_dir():
        try:
            package = load_agent_plugin(path, plugin_data_root() / namespace)
        except Exception as exc:
            record_error = str(exc)
    with _lock:
        enabled = [x for x in _read_enabled() if x != plugin_id]
        _write_enabled(enabled)
    if package is not None:
        return PluginRecord(
            id=plugin_id,
            name=package.name,
            version=package.version,
            description=package.description,
            path=str(package.root),
            enabled=False,
            namespace=namespace,
            skill_count=len(package.skills),
            mcp_count=len(package.mcp_servers),
            diagnostics=_diag_dicts(package.diagnostics),
            error=record_error,
        )
    return PluginRecord(
        id=plugin_id,
        name=plugin_id,
        version="",
        description="",
        path=str(path),
        enabled=False,
        namespace=namespace,
        error=record_error or ("plugin not found" if not path.is_dir() else ""),
    )


def _load_enabled_packages() -> List[Tuple[str, str, AgentPluginPackage]]:
    """Return (plugin_id, namespace, package) for each enabled valid plugin."""
    enabled = _read_enabled()
    out: List[Tuple[str, str, AgentPluginPackage]] = []
    for plugin_id in enabled:
        path = plugins_dir() / plugin_id
        if not path.is_dir():
            continue
        namespace = portable_skill_namespace(plugin_id)
        try:
            package = load_agent_plugin(path, plugin_data_root() / namespace)
        except Exception as exc:
            _diag("agent_plugins.load_enabled", msg=f"{plugin_id}: {exc}")
            continue
        out.append((plugin_id, namespace, package))
    return out


def list_enabled_plugin_skills() -> List[PluginSkillRecord]:
    """Namespaced skills from enabled plugins (never mutates SkillStore)."""
    try:
        packages = _load_enabled_packages()
    except Exception as exc:
        _diag("agent_plugins.list_skills", exc=exc)
        return []
    skills: List[PluginSkillRecord] = []
    for plugin_id, namespace, package in packages:
        for skill in package.skills:
            skills.append(
                PluginSkillRecord(
                    name=f"{namespace}:{skill.name}",
                    description=skill.description,
                    body=skill.body,
                    plugin_id=plugin_id,
                    namespace=namespace,
                    source_name=skill.name,
                )
            )
    return skills


def list_enabled_mcp_servers() -> Dict[str, Dict[str, Any]]:
    """Namespaced stdio MCP configs from enabled plugins (best-effort)."""
    try:
        packages = _load_enabled_packages()
    except Exception as exc:
        _diag("agent_plugins.list_mcp", exc=exc)
        return {}
    servers: Dict[str, Dict[str, Any]] = {}
    for plugin_id, namespace, package in packages:
        for server_name, config in package.mcp_servers.items():
            internal_name = f"{namespace}__{server_name}"
            if internal_name in servers:
                _diag(
                    "agent_plugins.mcp_collision",
                    msg=f"{plugin_id}: {internal_name}",
                )
                continue
            servers[internal_name] = dict(config)
    return servers


def namespaced_mcp_ids_for_plugin(plugin_id: str) -> List[str]:
    """Return namespaced MCP server ids for one installed plugin (best-effort)."""
    plugin_id = (plugin_id or "").strip()
    if not plugin_id:
        return []
    path = plugins_dir() / plugin_id
    if not path.is_dir():
        return []
    namespace = portable_skill_namespace(plugin_id)
    try:
        package = load_agent_plugin(path, plugin_data_root() / namespace)
    except Exception:
        return []
    return [f"{namespace}__{name}" for name in package.mcp_servers]


def plugin_record_to_dict(record: PluginRecord) -> Dict[str, Any]:
    return {
        "id": record.id,
        "name": record.name,
        "version": record.version,
        "description": record.description,
        "path": record.path,
        "enabled": record.enabled,
        "namespace": record.namespace,
        "skill_count": record.skill_count,
        "mcp_count": record.mcp_count,
        "diagnostics": list(record.diagnostics),
        "error": record.error,
    }
