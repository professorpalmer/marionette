"""Installed Agent Plugins v1 packages under ~/.pmharness/plugins.

Discover, install-from-path or resolved git/https/github sources, and
enable/disable portable packages. Default is disabled until explicitly
enabled. Skills and MCP configs are namespaced so they never overwrite
SkillStore files or collide with native mcp.json.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .agent_plugins import (
    STAMP_FILENAME,
    AgentPluginDiagnostic,
    AgentPluginError,
    AgentPluginPackage,
    compute_package_sha256,
    load_agent_plugin,
    read_agent_plugin_manifest,
)
from .plugin_capabilities import (
    capability_set_hash,
    parse_requested_capabilities,
    requested_capabilities_from_manifest,
)
from .plugin_source_urls import (
    ResolvedPluginSource,
    coerce_plugin_source,
    git_clone_plugin_source,
    resolve_plugin_source,
)
from .secure_files import restrict_to_owner
from .diag import note as _diag

_ENABLED_FILENAME = "enabled.json"
_CAPABILITIES_FILENAME = "capabilities.json"
_INSTALL_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$")
_lock = threading.RLock()


def _pmharness_root() -> Path:
    return Path(os.path.expanduser("~/.pmharness"))


def integrity_stamp_path(plugin_root: Path) -> Path:
    return Path(plugin_root) / STAMP_FILENAME


def write_integrity_stamp(plugin_root: Path) -> str:
    digest = compute_package_sha256(plugin_root)
    payload = {"sha256": digest}
    path = integrity_stamp_path(plugin_root)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    restrict_to_owner(str(path))
    return digest


def read_integrity_stamp(plugin_root: Path) -> Optional[str]:
    path = integrity_stamp_path(plugin_root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("sha256")
    return value if isinstance(value, str) and value else None


def verify_integrity_stamp(plugin_root: Path) -> str:
    """Return current digest. Raise if stamp missing or mismatched."""
    current = compute_package_sha256(plugin_root)
    stamped = read_integrity_stamp(plugin_root)
    if stamped is None:
        raise AgentPluginError("plugin integrity stamp is missing")
    if stamped != current:
        raise AgentPluginError("plugin integrity stamp mismatch")
    return current


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


def _capabilities_path() -> Path:
    return plugins_dir() / _CAPABILITIES_FILENAME


def _read_capability_records() -> Dict[str, Dict[str, Any]]:
    path = _capabilities_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    raw = data.get("plugins", data)
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for key, value in raw.items():
        if isinstance(key, str) and key.strip() and isinstance(value, dict):
            out[key.strip()] = value
    return out


def _write_capability_records(records: Dict[str, Dict[str, Any]]) -> None:
    root = plugins_dir()
    root.mkdir(parents=True, exist_ok=True)
    path = _capabilities_path()
    payload = {"plugins": dict(records)}
    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(root), prefix="capabilities_")
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


def _stored_capability_set(entry: Mapping[str, Any], key: str) -> frozenset:
    try:
        return parse_requested_capabilities(entry.get(key))
    except AgentPluginError:
        return frozenset()


def _capability_entry(
    requested: frozenset, consented: frozenset
) -> Dict[str, Any]:
    return {
        "requested": sorted(requested),
        "requested_hash": capability_set_hash(requested),
        "consented": sorted(consented),
        "consented_hash": capability_set_hash(consented),
    }


def _upsert_capability_entry(
    plugin_id: str,
    *,
    requested: frozenset,
    consented: Optional[frozenset] = None,
    reset_consent: bool = False,
) -> Dict[str, Any]:
    records = _read_capability_records()
    current = records.get(plugin_id) or {}
    if reset_consent:
        consented_set: frozenset = frozenset()
    elif consented is not None:
        consented_set = consented
    else:
        consented_set = _stored_capability_set(current, "consented")
    entry = _capability_entry(requested, consented_set)
    records[plugin_id] = entry
    _write_capability_records(records)
    return entry


def _require_capability_consent(plugin_id: str, requested: frozenset) -> None:
    """Fail closed when any requested id is outside the consented set."""
    if not requested:
        return
    current = _read_capability_records().get(plugin_id) or {}
    consented = _stored_capability_set(current, "consented")
    missing = requested - consented
    if missing:
        raise AgentPluginError(
            "plugin capability consent required for: " + ", ".join(sorted(missing))
        )


def _capability_snapshot(
    plugin_id: str,
    manifest: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    stored = _read_capability_records().get(plugin_id) or {}
    if manifest is not None:
        requested = requested_capabilities_from_manifest(manifest)
    else:
        requested = _stored_capability_set(stored, "requested")
    consented = _stored_capability_set(stored, "consented")
    return {
        "requested_capabilities": sorted(requested),
        "capability_set_hash": capability_set_hash(requested),
        "consented_capabilities": sorted(consented),
        "consented_hash": capability_set_hash(consented),
    }


def _write_enabled(enabled: List[str]) -> None:
    root = plugins_dir()
    root.mkdir(parents=True, exist_ok=True)
    path = _enabled_path()
    payload = {"enabled": list(enabled)}
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
    permissions: List[str] = field(default_factory=list)
    content_sha256: str = ""
    sha256: str = ""
    stamp_ok: bool = False
    requested_capabilities: List[str] = field(default_factory=list)
    capability_set_hash: str = ""
    consented_capabilities: List[str] = field(default_factory=list)
    consented_hash: str = ""
    diagnostics: List[Dict[str, str]] = field(default_factory=list)
    error: str = ""


def _diag_dicts(
    diagnostics: Tuple[AgentPluginDiagnostic, ...]
) -> List[Dict[str, str]]:
    return [{"scope": d.scope, "message": d.message} for d in diagnostics]


def _record_from_package(
    plugin_id: str,
    package: AgentPluginPackage,
    *,
    enabled: bool,
    extra_diagnostics: Tuple[AgentPluginDiagnostic, ...] = (),
    error: str = "",
    digest: str = "",
    stamp_ok: bool = False,
) -> PluginRecord:
    namespace = portable_skill_namespace(plugin_id)
    diagnostics = list(_diag_dicts(package.diagnostics))
    diagnostics.extend(_diag_dicts(extra_diagnostics))
    sha256 = digest or package.content_sha256
    caps = _capability_snapshot(plugin_id, package.manifest)
    return PluginRecord(
        id=plugin_id,
        name=package.name,
        version=package.version,
        description=package.description,
        path=str(package.root),
        enabled=enabled,
        namespace=namespace,
        skill_count=len(package.skills),
        mcp_count=len(package.mcp_servers),
        permissions=list(package.permissions),
        content_sha256=sha256,
        sha256=sha256,
        stamp_ok=stamp_ok,
        requested_capabilities=list(caps["requested_capabilities"]),
        capability_set_hash=str(caps["capability_set_hash"]),
        consented_capabilities=list(caps["consented_capabilities"]),
        consented_hash=str(caps["consented_hash"]),
        diagnostics=diagnostics,
        error=error,
    )


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
            stamp_error = ""
            stamp_ok = False
            digest = package.content_sha256
            try:
                digest = verify_integrity_stamp(package.root)
                stamp_ok = True
            except AgentPluginError as stamp_exc:
                stamp_error = str(stamp_exc)
                _diag("agent_plugins.stamp", msg=f"{plugin_id}: {stamp_exc}")
            extra: Tuple[AgentPluginDiagnostic, ...] = ()
            if stamp_error:
                extra = (AgentPluginDiagnostic("integrity", stamp_error),)
            records.append(
                _record_from_package(
                    plugin_id,
                    package,
                    enabled=plugin_id in enabled,
                    extra_diagnostics=extra,
                    error=stamp_error,
                    digest=digest,
                    stamp_ok=stamp_ok,
                )
            )
        except AgentPluginError as exc:
            try:
                manifest, diagnostics = read_agent_plugin_manifest(child)
                perms = [
                    value
                    for value in (manifest.get("permissions") or [])
                    if isinstance(value, str)
                ]
                records.append(
                    PluginRecord(
                        id=plugin_id,
                        name=str(manifest.get("name") or plugin_id),
                        version=str(manifest.get("version") or ""),
                        description=str(manifest.get("description") or ""),
                        path=str(child.resolve(strict=False)),
                        enabled=plugin_id in enabled,
                        namespace=namespace,
                        permissions=perms,
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


def _clone_resolved_source(resolved: ResolvedPluginSource, dest: Path) -> None:
    """Clone a remote plugin source into ``dest`` (git / https / github)."""
    url = (resolved.clone_url or "").strip()
    if not url:
        raise AgentPluginError("plugin source clone URL is required")
    git_clone_plugin_source(url, dest, ref=(resolved.ref or None))


def _materialize_source(source: object, *, clone_fn=None) -> Tuple[Path, Optional[Path]]:
    resolved = coerce_plugin_source(source)
    if resolved.kind == "path":
        src = Path(os.path.expanduser(resolved.path or resolved.raw)).expanduser()
        if not src.is_absolute():
            raise AgentPluginError("install path must be absolute")
        if not src.exists() or not src.is_dir():
            raise AgentPluginError("install path must be an existing directory")
        return src, None
    tmp = Path(tempfile.mkdtemp(prefix="marionette-plugin-src-"))
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        cloner = clone_fn or _clone_resolved_source
        cloner(resolved, tmp)
        root = tmp
        if resolved.subdir:
            root = tmp / resolved.subdir
            try:
                root.resolve().relative_to(tmp.resolve())
            except ValueError as exc:
                raise AgentPluginError("plugin subdir escapes source checkout") from exc
            if not root.is_dir():
                raise AgentPluginError("plugin subdir not found in source")
        return root, tmp
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def _install_materialized(src: Path, *, force: bool = False) -> PluginRecord:
    staging_ns = portable_skill_namespace("_install_probe")
    package = load_agent_plugin(src, plugin_data_root() / staging_ns)
    requested = requested_capabilities_from_manifest(package.manifest)
    install_id = _sanitize_install_id(package.name)
    dest = plugins_dir() / install_id
    with _lock:
        plugins_dir().mkdir(parents=True, exist_ok=True)
        if dest.exists():
            if not force:
                raise AgentPluginError(f"plugin already installed: {install_id}")
            shutil.rmtree(dest)
        try:
            shutil.copytree(str(src), str(dest), symlinks=False)
        except OSError as exc:
            raise AgentPluginError(f"failed to install plugin: {exc}") from exc
        digest = write_integrity_stamp(dest)
        enabled = [x for x in _read_enabled() if x != install_id]
        _write_enabled(enabled)
        _upsert_capability_entry(
            install_id, requested=requested, reset_consent=True
        )
    namespace = portable_skill_namespace(install_id)
    loaded = load_agent_plugin(dest, plugin_data_root() / namespace)
    digest = verify_integrity_stamp(dest)
    return _record_from_package(
        install_id, loaded, enabled=False, digest=digest, stamp_ok=True
    )


def install_from_path(source: str, *, clone_fn=None, force: bool = False) -> PluginRecord:
    """Copy a local dir or git / https / github source into plugins (disabled)."""
    src, cleanup = _materialize_source(source, clone_fn=clone_fn)
    try:
        return _install_materialized(src, force=force)
    finally:
        if cleanup is not None:
            shutil.rmtree(cleanup, ignore_errors=True)


def install_from_source(source: object, *, clone_fn=None, force: bool = False) -> PluginRecord:
    """Install from a path string, source URL, or resolved source mapping."""
    src, cleanup = _materialize_source(source, clone_fn=clone_fn)
    try:
        return _install_materialized(src, force=force)
    finally:
        if cleanup is not None:
            shutil.rmtree(cleanup, ignore_errors=True)


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
    digest = verify_integrity_stamp(path)
    requested = requested_capabilities_from_manifest(package.manifest)
    with _lock:
        _upsert_capability_entry(plugin_id, requested=requested)
        _require_capability_consent(plugin_id, requested)
        enabled = _read_enabled()
        if plugin_id not in enabled:
            enabled.append(plugin_id)
            _write_enabled(enabled)
    return _record_from_package(
        plugin_id, package, enabled=True, digest=digest, stamp_ok=True
    )


def consent_plugin_capabilities(plugin_id: str, ids: object) -> PluginRecord:
    """Store an explicit consented capability set (hash, not the integrity stamp)."""
    plugin_id = (plugin_id or "").strip()
    if not plugin_id:
        raise AgentPluginError("plugin id is required")
    path = plugins_dir() / plugin_id
    if not path.is_dir():
        raise AgentPluginError(f"plugin not found: {plugin_id}")
    namespace = portable_skill_namespace(plugin_id)
    package = load_agent_plugin(path, plugin_data_root() / namespace)
    requested = requested_capabilities_from_manifest(package.manifest)
    consented = parse_requested_capabilities(ids)
    with _lock:
        _upsert_capability_entry(
            plugin_id, requested=requested, consented=consented
        )
    enabled = plugin_id in set(_read_enabled())
    digest = read_integrity_stamp(path) or package.content_sha256
    stamp_ok = False
    try:
        digest = verify_integrity_stamp(path)
        stamp_ok = True
    except AgentPluginError:
        pass
    return _record_from_package(
        plugin_id, package, enabled=enabled, digest=digest, stamp_ok=stamp_ok
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
        digest = read_integrity_stamp(path) or package.content_sha256
        stamp_ok = False
        try:
            digest = verify_integrity_stamp(path)
            stamp_ok = True
        except AgentPluginError:
            pass
        return _record_from_package(
            plugin_id,
            package,
            enabled=False,
            error=record_error,
            digest=digest,
            stamp_ok=stamp_ok,
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
            verify_integrity_stamp(path)
            _require_capability_consent(
                plugin_id,
                requested_capabilities_from_manifest(package.manifest),
            )
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
        verify_integrity_stamp(path)
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
        "permissions": list(record.permissions),
        "content_sha256": record.content_sha256,
        "sha256": record.sha256,
        "stamp_ok": record.stamp_ok,
        "requested_capabilities": list(record.requested_capabilities),
        "capability_set_hash": record.capability_set_hash,
        "consented_capabilities": list(record.consented_capabilities),
        "consented_hash": record.consented_hash,
        "diagnostics": list(record.diagnostics),
        "error": record.error,
    }
