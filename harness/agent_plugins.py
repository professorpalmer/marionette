"""Agent Plugins v1 portable directory packages (skills + stdio MCP).

Validates the versioned portable format locally and translates supported
components into records for Marionette's skill prompt and MCP runtimes.
No remote schema fetch and no Python import from packages.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

PLUGIN_SCHEMA_V1 = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
MCP_SCHEMA_V1 = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"

_PLUGIN_FIELDS = {
    "$schema",
    "name",
    "version",
    "description",
    "author",
    "homepage",
    "repository",
    "license",
    "keywords",
    "extensions",
}
_AUTHOR_FIELDS = {"name", "email", "url"}
_STDIO_FIELDS = {"type", "command", "args", "env", "cwd"}
_REMOTE_FIELDS = {"type", "url", "headers"}
_PLUGIN_NAME_RE = re.compile(
    r"^(?!.*(?:--|\.\.))[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$"
)
_SKILL_NAME_RE = re.compile(r"^(?!.*--)[a-z0-9]+(?:-[a-z0-9]+)*$")
_PLACEHOLDER_RE = re.compile(r"\$\{(PLUGIN_ROOT|PLUGIN_DATA)\}")
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


class AgentPluginError(ValueError):
    """Fatal portable manifest validation failure."""


@dataclass(frozen=True)
class AgentPluginDiagnostic:
    scope: str
    message: str


@dataclass(frozen=True)
class AgentPluginSkill:
    name: str
    description: str
    root: Path
    skill_md: Path
    body: str
    frontmatter: Mapping[str, Any]


@dataclass(frozen=True)
class AgentPluginPackage:
    name: str
    version: str
    description: str
    root: Path
    data_root: Path
    manifest: Mapping[str, Any]
    skills: Tuple[AgentPluginSkill, ...]
    mcp_servers: Mapping[str, Dict[str, Any]]
    diagnostics: Tuple[AgentPluginDiagnostic, ...]


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=True))
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def _read_json_object(path: Path, *, label: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AgentPluginError(f"{label} is not valid readable JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise AgentPluginError(f"{label} must contain a JSON object")
    return value


def _parse_flow_sequence(text: str) -> list:
    inner = text[1:-1].strip()
    if not inner:
        return []
    items: List[Any] = []
    buf: List[str] = []
    depth = 0
    in_quote: Optional[str] = None
    i = 0
    while i < len(inner):
        ch = inner[i]
        if in_quote:
            buf.append(ch)
            if ch == in_quote and (i == 0 or inner[i - 1] != "\\"):
                in_quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            in_quote = ch
            buf.append(ch)
        elif ch in "[{":
            depth += 1
            buf.append(ch)
        elif ch in "]}":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            items.append(_parse_yaml_scalar("".join(buf).strip()))
            buf = []
        else:
            buf.append(ch)
        i += 1
    if buf or not items:
        items.append(_parse_yaml_scalar("".join(buf).strip()))
    return items


def _parse_yaml_scalar(raw: str) -> Any:
    s = raw.strip()
    if not s:
        return ""
    if (s.count("[") != s.count("]")) or (s.count("{") != s.count("}")):
        raise ValueError("invalid YAML frontmatter")
    if (s.startswith("'") and s.endswith("'") and len(s) >= 2) or (
        s.startswith('"') and s.endswith('"') and len(s) >= 2
    ):
        return s[1:-1]
    if s.startswith("[") and s.endswith("]"):
        return _parse_flow_sequence(s)
    if s in ("true", "True"):
        return True
    if s in ("false", "False"):
        return False
    if s in ("null", "Null", "~"):
        return None
    return s


def _parse_simple_yaml(text: str) -> dict:
    """Minimal YAML mapping subset for Agent Skills frontmatter (stdlib only)."""
    root: Dict[str, Any] = {}
    # stack entries: (indent_of_keys_in_this_map, map)
    stack: List[Tuple[int, Dict[str, Any]]] = [(0, root)]
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if "\t" in raw:
            raise ValueError("tabs are not allowed in YAML frontmatter")
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        if ":" not in line:
            raise ValueError("invalid YAML frontmatter")
        key, _, rest = line.partition(":")
        key = key.strip()
        if not key:
            raise ValueError("invalid YAML frontmatter")
        while len(stack) > 1 and indent < stack[-1][0]:
            stack.pop()
        parent_indent, parent = stack[-1]
        if indent != parent_indent:
            raise ValueError("invalid YAML frontmatter indentation")
        value_text = rest.strip()
        if value_text == "":
            nested: Dict[str, Any] = {}
            parent[key] = nested
            stack.append((indent + 2, nested))
        else:
            parent[key] = _parse_yaml_scalar(value_text)
    return root


def _parse_skill_markdown(content: str) -> Tuple[Dict[str, Any], str]:
    content = content.lstrip("\ufeff")
    if not content.startswith("---"):
        raise ValueError("missing YAML frontmatter")
    end_match = re.search(r"\n---\s*\n", content[3:])
    if end_match is None:
        # allow EOF terminator ---
        end_match = re.search(r"\n---\s*$", content[3:])
        if end_match is None:
            raise ValueError("unterminated YAML frontmatter")
        body = ""
        fm_text = content[3 : end_match.start() + 3]
    else:
        fm_text = content[3 : end_match.start() + 3]
        body = content[3 + end_match.end() :]
    parsed = _parse_simple_yaml(fm_text)
    if not isinstance(parsed, dict):
        raise ValueError("YAML frontmatter must be an object")
    return parsed, body.strip()


def _validate_manifest(root: Path) -> Tuple[dict, List[AgentPluginDiagnostic]]:
    manifest_path = root / "plugin.json"
    if not _inside(manifest_path, root) or not manifest_path.is_file():
        raise AgentPluginError("plugin.json must be a regular file within the plugin root")
    manifest = _read_json_object(manifest_path, label="plugin.json")
    diagnostics: List[AgentPluginDiagnostic] = []

    for field in sorted(set(manifest) - _PLUGIN_FIELDS):
        diagnostics.append(
            AgentPluginDiagnostic("manifest", f"ignored unknown top-level field: {field}")
        )
        manifest.pop(field)

    if manifest.get("$schema") != PLUGIN_SCHEMA_V1:
        raise AgentPluginError(
            "plugin.json declares an unsupported or missing Agent Plugins schema"
        )
    name = manifest.get("name")
    if (
        not isinstance(name, str)
        or not 1 <= len(name) <= 64
        or _PLUGIN_NAME_RE.fullmatch(name) is None
    ):
        raise AgentPluginError("plugin.json name does not satisfy v1 constraints")

    for field in ("version", "description", "homepage", "repository", "license"):
        if field in manifest and not isinstance(manifest[field], str):
            raise AgentPluginError(f"plugin.json {field} must be a string")

    if "keywords" in manifest:
        keywords = manifest["keywords"]
        if not isinstance(keywords, list) or any(
            not isinstance(value, str) for value in keywords
        ):
            raise AgentPluginError("plugin.json keywords must be an array of strings")

    if "author" in manifest:
        author = manifest["author"]
        if not isinstance(author, dict):
            raise AgentPluginError("plugin.json author must be an object")
        unknown = set(author) - _AUTHOR_FIELDS
        if unknown or any(not isinstance(value, str) for value in author.values()):
            raise AgentPluginError(
                "plugin.json author may contain only string name, email, and url fields"
            )

    if "extensions" in manifest:
        extensions = manifest["extensions"]
        if not isinstance(extensions, dict):
            diagnostics.append(
                AgentPluginDiagnostic(
                    "manifest", "ignored non-object extensions field"
                )
            )
            manifest.pop("extensions")
        elif any(not isinstance(value, dict) for value in extensions.values()):
            raise AgentPluginError("plugin.json extension namespace values must be objects")

    return manifest, diagnostics


def _valid_skill_frontmatter(
    frontmatter: Mapping[str, Any], directory_name: str
) -> Optional[str]:
    name = frontmatter.get("name")
    if (
        not isinstance(name, str)
        or name != directory_name
        or not 1 <= len(name) <= 64
        or _SKILL_NAME_RE.fullmatch(name) is None
    ):
        return "name must match the directory and satisfy Agent Skills constraints"
    description = frontmatter.get("description")
    if not isinstance(description, str) or not 1 <= len(description) <= 1024:
        return "description must be a non-empty string of at most 1024 characters"
    if "license" in frontmatter and not isinstance(frontmatter["license"], str):
        return "license must be a string"
    if "compatibility" in frontmatter:
        compatibility = frontmatter["compatibility"]
        if not isinstance(compatibility, str) or not 1 <= len(compatibility) <= 500:
            return "compatibility must be a string of 1 to 500 characters"
    if "metadata" in frontmatter:
        metadata = frontmatter["metadata"]
        if not isinstance(metadata, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in metadata.items()
        ):
            return "metadata must map string keys to string values"
    if "allowed-tools" in frontmatter and not isinstance(
        frontmatter["allowed-tools"], str
    ):
        return "allowed-tools must be a string"
    return None


def _discover_skills(
    root: Path, diagnostics: List[AgentPluginDiagnostic]
) -> Tuple[AgentPluginSkill, ...]:
    skills_root = root / "skills"
    if not skills_root.exists() and not skills_root.is_symlink():
        return ()
    if not _inside(skills_root, root) or not skills_root.is_dir():
        diagnostics.append(
            AgentPluginDiagnostic("skills", "skills must be an in-root directory")
        )
        return ()

    skills: List[AgentPluginSkill] = []
    try:
        children = sorted(skills_root.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        diagnostics.append(AgentPluginDiagnostic("skills", f"cannot list skills: {exc}"))
        return ()

    for child in children:
        skill_md = child / "SKILL.md"
        if not child.is_dir() or not skill_md.exists():
            continue
        scope = f"skill:{child.name}"
        if not _inside(skill_md, root) or not skill_md.is_file():
            diagnostics.append(
                AgentPluginDiagnostic(scope, "SKILL.md must be a regular in-root file")
            )
            continue
        try:
            content = skill_md.read_text(encoding="utf-8")
            frontmatter, body = _parse_skill_markdown(content)
        except (OSError, UnicodeError, ValueError) as exc:
            diagnostics.append(AgentPluginDiagnostic(scope, f"invalid SKILL.md: {exc}"))
            continue
        error = _valid_skill_frontmatter(frontmatter, child.name)
        if error:
            diagnostics.append(AgentPluginDiagnostic(scope, error))
            continue
        skills.append(
            AgentPluginSkill(
                name=child.name,
                description=str(frontmatter["description"]),
                root=child.resolve(strict=True),
                skill_md=skill_md.resolve(strict=True),
                body=body,
                frontmatter=dict(frontmatter),
            )
        )
    return tuple(skills)


def _expand(value: str, plugin_root: Path, data_root: Path) -> str:
    """Expand PLUGIN_ROOT / PLUGIN_DATA placeholders to OS-native paths.

    Path-shaped values (exact root or ``${PLUGIN_*}/rel``) join via ``Path`` so
    Windows does not keep a mixed ``C:\\plugin/server.py`` separator. Other
    strings still get literal placeholder substitution.
    """
    if value == "${PLUGIN_ROOT}":
        return str(plugin_root)
    if value == "${PLUGIN_DATA}":
        return str(data_root)
    if value.startswith("${PLUGIN_ROOT}/"):
        return str(plugin_root / value[len("${PLUGIN_ROOT}/") :])
    if value.startswith("${PLUGIN_DATA}/"):
        return str(data_root / value[len("${PLUGIN_DATA}/") :])
    replacements = {
        "PLUGIN_ROOT": str(plugin_root),
        "PLUGIN_DATA": str(data_root),
    }
    return _PLACEHOLDER_RE.sub(lambda match: replacements[match.group(1)], value)


def _resolve_scoped_path(
    value: str,
    plugin_root: Path,
    data_root: Path,
    *,
    expand_placeholders: bool = True,
) -> Path:
    if value.startswith("./"):
        base = plugin_root
        # Keep package-relative ``./`` paths even when expand is disabled
        # (stdio command tokens); placeholders are never valid after ``./``.
        candidate = base / value[2:]
    elif value == "${PLUGIN_ROOT}" or value.startswith("${PLUGIN_ROOT}/"):
        base = plugin_root
        if not expand_placeholders:
            raise ValueError("path must start with ./, ${PLUGIN_ROOT}, or ${PLUGIN_DATA}")
        candidate = (
            base
            if value == "${PLUGIN_ROOT}"
            else base / value[len("${PLUGIN_ROOT}/") :]
        )
    elif value == "${PLUGIN_DATA}" or value.startswith("${PLUGIN_DATA}/"):
        base = data_root
        if not expand_placeholders:
            raise ValueError("path must start with ./, ${PLUGIN_ROOT}, or ${PLUGIN_DATA}")
        candidate = (
            base
            if value == "${PLUGIN_DATA}"
            else base / value[len("${PLUGIN_DATA}/") :]
        )
    else:
        raise ValueError("path must start with ./, ${PLUGIN_ROOT}, or ${PLUGIN_DATA}")
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(base.resolve(strict=False))
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("path escapes its resolved root") from exc
    return resolved


def _validate_headers(headers: object) -> bool:
    if headers is None:
        return True
    if not isinstance(headers, dict):
        return False
    seen: set = set()
    for name, value in headers.items():
        if (
            not isinstance(name, str)
            or _HEADER_NAME_RE.fullmatch(name) is None
            or not isinstance(value, str)
            or "\r" in value
            or "\n" in value
            or name.lower() in seen
        ):
            return False
        seen.add(name.lower())
    return True


def _translate_stdio(
    config: Mapping[str, Any], plugin_root: Path, data_root: Path
) -> Dict[str, Any]:
    if set(config) - _STDIO_FIELDS:
        raise ValueError("unknown stdio field")
    command = config.get("command")
    if not isinstance(command, str) or not command or "\x00" in command:
        raise ValueError("command must be a non-empty executable token")
    if command.startswith("./"):
        command_value = str(
            _resolve_scoped_path(
                command,
                plugin_root,
                data_root,
                expand_placeholders=False,
            )
        )
    elif any(character.isspace() for character in command):
        raise ValueError("command must contain one executable token")
    elif "/" in command or "\\" in command or command in {".", ".."}:
        raise ValueError("command must be a bare executable or begin with ./")
    else:
        command_value = command

    args = config.get("args", [])
    if not isinstance(args, list) or any(not isinstance(value, str) for value in args):
        raise ValueError("args must be an array of strings")
    env = config.get("env", {})
    if not isinstance(env, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in env.items()
    ):
        raise ValueError("env must map string keys to string values")
    env_keys = {key.upper() if os.name == "nt" else key for key in env}
    if "PLUGIN_ROOT" in env_keys or "PLUGIN_DATA" in env_keys:
        raise ValueError("PLUGIN_ROOT and PLUGIN_DATA are reserved")

    cwd = config.get("cwd")
    if cwd is None:
        cwd_value = plugin_root
    elif not isinstance(cwd, str):
        raise ValueError("cwd must be a string")
    else:
        cwd_value = _resolve_scoped_path(cwd, plugin_root, data_root)

    translated_env = {
        key: _expand(value, plugin_root, data_root) for key, value in env.items()
    }
    translated_env["PLUGIN_ROOT"] = str(plugin_root)
    translated_env["PLUGIN_DATA"] = str(data_root)
    return {
        "command": command_value,
        "args": [_expand(value, plugin_root, data_root) for value in args],
        "env": translated_env,
        "cwd": str(cwd_value),
    }


def _discover_mcp(
    root: Path,
    data_root: Path,
    diagnostics: List[AgentPluginDiagnostic],
    *,
    create_data: bool = True,
) -> Dict[str, Dict[str, Any]]:
    mcp_path = root / "mcp.json"
    if not mcp_path.exists() and not mcp_path.is_symlink():
        return {}
    if not _inside(mcp_path, root) or not mcp_path.is_file():
        diagnostics.append(
            AgentPluginDiagnostic("mcp", "mcp.json must be a regular in-root file")
        )
        return {}
    try:
        config = _read_json_object(mcp_path, label="mcp.json")
    except AgentPluginError as exc:
        diagnostics.append(AgentPluginDiagnostic("mcp", str(exc)))
        return {}
    if set(config) != {"$schema", "mcpServers"}:
        diagnostics.append(
            AgentPluginDiagnostic("mcp", "mcp.json has an invalid top-level shape")
        )
        return {}
    if config.get("$schema") != MCP_SCHEMA_V1:
        diagnostics.append(
            AgentPluginDiagnostic("mcp", "mcp.json declares an unsupported schema")
        )
        return {}
    servers = config.get("mcpServers")
    if not isinstance(servers, dict):
        diagnostics.append(
            AgentPluginDiagnostic("mcp", "mcpServers must be an object")
        )
        return {}

    translated: Dict[str, Dict[str, Any]] = {}
    for name, server in servers.items():
        scope = f"mcp:{name}"
        if not isinstance(name, str) or not name or not isinstance(server, dict):
            diagnostics.append(AgentPluginDiagnostic(scope, "invalid server entry"))
            continue
        server_type = server.get("type")
        if server_type == "stdio":
            try:
                translated_server = _translate_stdio(server, root, data_root)
                if create_data:
                    data_root.mkdir(parents=True, exist_ok=True)
                    cwd_path = Path(translated_server["cwd"])
                    try:
                        cwd_path.relative_to(data_root)
                    except ValueError:
                        pass
                    else:
                        # Create only data-root descendants; plugin-root paths
                        # remain package-owned.
                        cwd_path.mkdir(parents=True, exist_ok=True)
                translated[name] = translated_server
            except (OSError, ValueError) as exc:
                diagnostics.append(AgentPluginDiagnostic(scope, str(exc)))
        elif server_type in {"streamable-http", "sse"}:
            if (
                set(server) - _REMOTE_FIELDS
                or not isinstance(server.get("url"), str)
                or not server.get("url")
                or not _validate_headers(server.get("headers"))
            ):
                diagnostics.append(AgentPluginDiagnostic(scope, "invalid remote entry"))
            else:
                diagnostics.append(
                    AgentPluginDiagnostic(
                        scope,
                        f"portable {server_type} transport is not supported",
                    )
                )
        else:
            diagnostics.append(AgentPluginDiagnostic(scope, "unknown MCP server type"))
    return translated


def load_agent_plugin(plugin_root: Path, data_root: Path) -> AgentPluginPackage:
    """Validate and translate one installed Agent Plugins v1 package.

    Fatal manifest errors raise :class:`AgentPluginError`. Component and entry
    failures are returned as diagnostics and isolated to their owning scope.
    """
    root = Path(plugin_root).resolve(strict=True)
    if not root.is_dir():
        raise AgentPluginError("plugin root must be a directory")
    manifest, diagnostics = _validate_manifest(root)
    resolved_data = Path(data_root).resolve(strict=False)
    skills = _discover_skills(root, diagnostics)
    mcp_servers = _discover_mcp(root, resolved_data, diagnostics)
    return AgentPluginPackage(
        name=manifest["name"],
        version=manifest.get("version", "") or "",
        description=manifest.get("description", "") or "",
        root=root,
        data_root=resolved_data,
        manifest=dict(manifest),
        skills=skills,
        mcp_servers=mcp_servers,
        diagnostics=tuple(diagnostics),
    )


def read_agent_plugin_manifest(
    plugin_root: Path,
) -> Tuple[dict, Tuple[AgentPluginDiagnostic, ...]]:
    """Validate only root ``plugin.json`` without discovering components."""
    root = Path(plugin_root).resolve(strict=True)
    if not root.is_dir():
        raise AgentPluginError("plugin root must be a directory")
    manifest, diagnostics = _validate_manifest(root)
    return manifest, tuple(diagnostics)
