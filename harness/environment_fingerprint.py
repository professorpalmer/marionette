"""Bounded, secret-safe volatile environment fingerprint for validation reuse.

Owned by ``harness.validation_reuse`` policy. Captures hashes/metadata only —
never secret values — so PATH/tool/browser/MCP/Puppetmaster drift invalidates
reuse without spawning expensive ``--version`` probes on every gate.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Mapping, Optional, Sequence

ENVIRONMENT_FINGERPRINT_SCHEMA = 2
ENVIRONMENT_FINGERPRINT_VERSION = "marionette-env-fp-v2"

# Bound MCP config reads so a huge malformed file cannot stall the gate.
_MCP_CONFIG_MAX_BYTES = 256_000
_TOOL_NAMES = ("pyright", "pyright-langserver", "tsc")
# Per-segment sample for large binaries (Chrome); head+middle+tail, touch-stable.
_EXEC_SAMPLE_BYTES = 64 * 1024


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_path(path: str) -> str:
    text = str(path or "").strip()
    if not text:
        return ""
    try:
        from .paths import _resolve
        text = _resolve(text)
    except Exception:
        try:
            text = os.path.abspath(os.path.expanduser(text))
        except Exception:
            text = text.replace("\\", "/")
    try:
        text = os.path.normcase(text)
    except Exception:
        pass
    return text.replace("\\", "/")


def _identity_path(path: str) -> str:
    """Stable path identity: home prefix collapsed to ``~`` (cross-user safe)."""
    canon = _canonical_path(path)
    if not canon:
        return ""
    try:
        home = os.path.expanduser("~").replace("\\", "/")
        try:
            home = os.path.normcase(home)
        except Exception:
            pass
        home = home.replace("\\", "/").rstrip("/")
        if home and (canon == home or canon.startswith(home + "/")):
            return "~" + canon[len(home):]
    except Exception:
        pass
    return canon


def _bounded_content_digest(path: str, size: int) -> str:
    """Deterministic content identity from size + bounded head/middle/tail samples.

    At most three ``_EXEC_SAMPLE_BYTES`` reads. Small files digest as head (and
    residual as tail when present). Large files also sample a centered middle
    segment so mid-file byte replacement drifts identity without reading the
    whole binary. Touch-stable: only size + sampled bytes enter the digest.
    """
    h = hashlib.sha256()
    h.update(f"size:{int(size)}\n".encode("utf-8"))
    sample = int(_EXEC_SAMPLE_BYTES)
    try:
        with open(path, "rb") as fh:
            head = fh.read(sample)
            h.update(b"head:")
            h.update(head)
            if size <= sample:
                return h.hexdigest()
            if size > sample * 2:
                # Centered middle segment; for size > 3*sample it sits between
                # the head and tail windows. Total read stays ≤ 3*sample.
                mid_start = max(0, (int(size) - sample) // 2)
                fh.seek(mid_start)
                middle = fh.read(sample)
                h.update(b"middle:")
                h.update(middle)
                fh.seek(max(0, int(size) - sample))
                tail = fh.read(sample)
                h.update(b"tail:")
                h.update(tail)
            else:
                # Residual after head (≤ sample bytes); no distinct middle.
                tail = fh.read(sample)
                h.update(b"tail:")
                h.update(tail)
    except OSError:
        return ""
    return h.hexdigest()


def _executable_identity(path: str) -> dict[str, Any]:
    """Touch-stable executable identity: path + size + bounded content digest.

    Omits mtime/inode so ``touch`` alone cannot force a full swarm. Byte
    replacement in head, middle, or tail (same or different size) changes the
    content digest. Paths under the user home are stored with a ``~`` prefix.
    """
    empty = {
        "present": False,
        "path": "",
        "size": None,
        "content_digest": "",
    }
    raw = str(path or "").strip()
    if not raw:
        return dict(empty)
    canon = _canonical_path(raw)
    identity = _identity_path(raw)
    if not canon or not identity:
        return dict(empty)
    try:
        st = os.stat(canon)
        size = int(st.st_size)
        digest = _bounded_content_digest(canon, size)
        if not digest:
            return {
                "present": False,
                "path": identity,
                "size": size,
                "content_digest": "",
            }
        return {
            "present": True,
            "path": identity,
            "size": size,
            "content_digest": digest,
        }
    except OSError:
        return {
            "present": False,
            "path": identity,
            "size": None,
            "content_digest": "",
        }


def _tool_resolution_material(cwd: str) -> dict[str, Any]:
    """PATH / workspace resolution for pyright, pyright-langserver, tsc."""
    from .lsp_code_intelligence import discover_lsp_tools

    tools = discover_lsp_tools(root=cwd or None)
    resolved = {
        "pyright": tools.python_pyright or "",
        "pyright-langserver": tools.python_pyright_langserver or "",
        "tsc": tools.typescript_tsc or "",
    }
    out: dict[str, Any] = {
        "python_available": bool(tools.python_available),
        "typescript_available": bool(tools.typescript_available),
        "executables": {},
    }
    for name in _TOOL_NAMES:
        out["executables"][name] = _executable_identity(resolved.get(name) or "")
    return out


def _browser_material() -> dict[str, Any]:
    """Browser identity with a fresh probe so install/move/env drift is visible.

    Always refresh the standalone Chrome cache when stamping/matching — a stale
    process-cached miss/hit must not hide PM_BROWSER_CHROME or install changes.
    """
    configured = os.environ.get("PM_BROWSER_CHROME", "").strip()
    configured_canon = _identity_path(configured) if configured else ""
    resolved = ""
    try:
        from .browser import standalone_chrome_path
        resolved = standalone_chrome_path(refresh=True) or ""
    except Exception:
        resolved = ""
    return {
        "pm_browser_chrome": configured_canon,
        "resolved": _executable_identity(resolved),
    }


def _puppetmaster_material() -> dict[str, Any]:
    version = ""
    version_error = ""
    try:
        import importlib.metadata as metadata
        version = str(metadata.version("puppetmaster-ai") or "").strip()
    except Exception as exc:
        version_error = exc.__class__.__name__
        try:
            import puppetmaster
            version = str(getattr(puppetmaster, "__version__", "") or "").strip()
        except Exception as exc2:
            version_error = f"{version_error}+{exc2.__class__.__name__}"
    helpers = False
    try:
        from .validation_reuse import pm_validation_helpers_available
        helpers = bool(pm_validation_helpers_available())
    except Exception:
        helpers = False
    return {
        "package_version": version,
        "version_error": version_error,
        "validation_helpers": helpers,
    }


def _mcp_identity_payload(servers: Mapping[str, Any]) -> dict[str, Any]:
    """Server identity only — secrets redacted before hashing.

    Redaction/canonicalization failures raise so the fingerprint probe fails
    closed; never collapse a present config to an empty shared digest.
    """
    try:
        from .mcp_manager import redact_mcp_secrets
        scrubbed = redact_mcp_secrets(dict(servers))
    except Exception as exc:
        raise RuntimeError(
            f"mcp_redaction_failed:{exc.__class__.__name__}"
        ) from exc
    if not isinstance(scrubbed, Mapping):
        raise RuntimeError("mcp_redaction_failed:non_mapping")
    rows: list[dict[str, Any]] = []
    for name in sorted(str(k) for k in scrubbed.keys()):
        cfg = scrubbed.get(name)
        if not isinstance(cfg, Mapping):
            rows.append({"name": name, "kind": "invalid"})
            continue
        env = cfg.get("env") if isinstance(cfg.get("env"), Mapping) else {}
        headers = cfg.get("headers") if isinstance(cfg.get("headers"), Mapping) else {}
        rows.append({
            "name": name,
            "command": str(cfg.get("command") or ""),
            "args": list(cfg.get("args") or []) if isinstance(cfg.get("args"), list) else [],
            "url": str(cfg.get("url") or ""),
            "allowed_tools": (
                list(cfg.get("allowed_tools") or [])
                if isinstance(cfg.get("allowed_tools"), list) else None
            ),
            # Key names only — values are already REDACTED constants.
            "env_keys": sorted(str(k) for k in env.keys()),
            "header_keys": sorted(str(k) for k in headers.keys()),
        })
    return {"servers": rows, "server_names": [r["name"] for r in rows]}


def _bounded_mcp_file_material(path: str) -> dict[str, Any]:
    identity = _identity_path(path) if path else ""
    canon = _canonical_path(path) if path else ""
    if not canon:
        return {
            "path": identity,
            "present": False,
            "digest": "",
            "size": None,
            "server_names": [],
        }
    try:
        st = os.stat(canon)
        size = int(st.st_size)
    except OSError:
        return {
            "path": identity,
            "present": False,
            "digest": "",
            "size": None,
            "server_names": [],
        }
    if size > _MCP_CONFIG_MAX_BYTES:
        return {
            "path": identity,
            "present": True,
            "digest": "",
            "size": size,
            "server_names": [],
            "error": "mcp_config_too_large",
        }
    try:
        with open(canon, "rb") as fh:
            raw = fh.read(_MCP_CONFIG_MAX_BYTES + 1)
        if len(raw) > _MCP_CONFIG_MAX_BYTES:
            return {
                "path": identity,
                "present": True,
                "digest": "",
                "size": size,
                "server_names": [],
                "error": "mcp_config_too_large",
            }
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        return {
            "path": identity,
            "present": True,
            "digest": "",
            "size": size,
            "server_names": [],
            "error": f"mcp_config_unreadable:{exc.__class__.__name__}",
        }
    servers = {}
    if isinstance(data, Mapping):
        raw_servers = data.get("mcpServers")
        if isinstance(raw_servers, Mapping):
            servers = dict(raw_servers)
    try:
        identity_payload = _mcp_identity_payload(servers)
    except RuntimeError as exc:
        return {
            "path": identity,
            "present": True,
            "digest": "",
            "size": size,
            "server_names": [],
            "error": str(exc) or "mcp_redaction_failed",
        }
    digest = _sha256_text(
        json.dumps(identity_payload, sort_keys=True, separators=(",", ":"))
    )
    return {
        "path": identity,
        "present": True,
        "digest": digest,
        "size": size,
        "server_names": list(identity_payload.get("server_names") or []),
    }


def _mcp_config_paths(cwd: str) -> list[str]:
    paths: list[str] = []
    try:
        from .mcp_manager import CONFIG_PATH
        paths.append(str(CONFIG_PATH))
    except Exception:
        paths.append(os.path.expanduser("~/.pmharness/mcp.json"))
    root = (cwd or "").strip()
    if root:
        paths.append(os.path.join(root, ".cursor", "mcp.json"))
        paths.append(os.path.join(root, "mcp.json"))
    # Deduplicate by canonical path while preserving order.
    out: list[str] = []
    seen: set[str] = set()
    for p in paths:
        canon = _canonical_path(p) or p
        key = canon.replace("\\", "/").lower() if os.name == "nt" else canon
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def _mcp_material(cwd: str) -> dict[str, Any]:
    files = [_bounded_mcp_file_material(p) for p in _mcp_config_paths(cwd)]
    # Fail closed when a present config cannot be digested.
    for row in files:
        if row.get("present") and row.get("error"):
            raise RuntimeError(str(row.get("error") or "mcp_config_unreadable"))
    names: list[str] = []
    for row in files:
        for n in row.get("server_names") or []:
            if n not in names:
                names.append(n)
    # Fingerprint identity only: path + content digest + server names.
    # Omit size so secret-value rotation (same keys/command/url) does
    # not spuriously invalidate reuse.
    identity_files = [
        {
            "path": row.get("path") or "",
            "present": bool(row.get("present")),
            "digest": row.get("digest") or "",
            "server_names": list(row.get("server_names") or []),
            "error": row.get("error") or "",
        }
        for row in files
    ]
    return {
        "files": identity_files,
        "configured_server_names": names,
    }


def compute_environment_fingerprint(
    cwd: str,
    *,
    strict: bool = True,
) -> tuple[Optional[dict[str, Any]], str]:
    """Compute a bounded volatile-environment fingerprint.

    Returns ``(payload_or_none, reason)``. Failures are explicit: callers must
    treat ``None`` as fail-closed (no reuse). Payload never embeds secret
    values — only hashes, home-normalized identity paths, and size/digest
    metadata (never mtime/inode).
    """
    root = (cwd or "").strip()
    try:
        tools = _tool_resolution_material(root)
        browser = _browser_material()
        puppetmaster = _puppetmaster_material()
        mcp = _mcp_material(root)
    except Exception as exc:
        reason = str(exc).strip()
        if reason.startswith("mcp_"):
            return None, reason
        return None, f"environment_probe_failed:{exc.__class__.__name__}"

    material = {
        "schema": ENVIRONMENT_FINGERPRINT_SCHEMA,
        "version": ENVIRONMENT_FINGERPRINT_VERSION,
        "workspace": _identity_path(root) if root else "",
        "tools": tools,
        "browser": browser,
        "puppetmaster": puppetmaster,
        "mcp": mcp,
    }
    try:
        fingerprint = _sha256_text(
            json.dumps(material, sort_keys=True, separators=(",", ":"))
        )
    except Exception as exc:
        return None, f"environment_fingerprint_hash_failed:{exc.__class__.__name__}"

    complete = True
    if not puppetmaster.get("package_version") and puppetmaster.get("version_error"):
        # Missing PM version is still a stable signal (empty + error class),
        # not an incomplete probe — keep complete so drift still compares.
        pass
    payload = {
        "fingerprint": fingerprint,
        "schema": ENVIRONMENT_FINGERPRINT_SCHEMA,
        "version": ENVIRONMENT_FINGERPRINT_VERSION,
        "complete": complete,
        # Compact identity material for tests / diagnostics (no secrets).
        "tool_paths": {
            name: (tools.get("executables") or {}).get(name, {}).get("path") or ""
            for name in _TOOL_NAMES
        },
        "pm_browser_chrome": browser.get("pm_browser_chrome") or "",
        "browser_path": (browser.get("resolved") or {}).get("path") or "",
        "puppetmaster_version": puppetmaster.get("package_version") or "",
        "validation_helpers": bool(puppetmaster.get("validation_helpers")),
        "mcp_server_names": list(mcp.get("configured_server_names") or []),
        "mcp_digests": [
            str(row.get("digest") or "")
            for row in (mcp.get("files") or [])
            if row.get("present")
        ],
    }
    if strict and not payload.get("complete"):
        return None, "environment_fingerprint_incomplete"
    return payload, ""


def environment_fingerprint_of(job_or_block: Mapping[str, Any]) -> str:
    """Extract a stamped environment fingerprint from a job or validation block."""
    if not isinstance(job_or_block, Mapping):
        return ""
    direct = str(job_or_block.get("environment_fingerprint") or "").strip()
    if direct:
        return direct
    block = job_or_block.get("validation")
    if isinstance(block, Mapping):
        value = str(block.get("environment_fingerprint") or "").strip()
        if value:
            return value
    return ""


def environment_fingerprint_schema_of(job_or_block: Mapping[str, Any]) -> Optional[int]:
    if not isinstance(job_or_block, Mapping):
        return None
    for key in ("environment_fingerprint_schema", "environment_schema"):
        raw = job_or_block.get(key)
        if raw is None:
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
    block = job_or_block.get("validation")
    if isinstance(block, Mapping):
        raw = block.get("environment_fingerprint_schema")
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
    return None


def job_environment_fingerprint(job: Mapping[str, Any]) -> str:
    fp = environment_fingerprint_of(job)
    if fp:
        return fp
    for art in job.get("artifacts") or []:
        if not isinstance(art, Mapping):
            continue
        block = art.get("validation")
        if isinstance(block, Mapping):
            value = str(block.get("environment_fingerprint") or "").strip()
            if value:
                return value
    return ""


def job_environment_fingerprint_schema(job: Mapping[str, Any]) -> Optional[int]:
    schema = environment_fingerprint_schema_of(job)
    if schema is not None:
        return schema
    for art in job.get("artifacts") or []:
        if not isinstance(art, Mapping):
            continue
        block = art.get("validation")
        if isinstance(block, Mapping):
            raw = block.get("environment_fingerprint_schema")
            if raw is None:
                continue
            try:
                return int(raw)
            except (TypeError, ValueError):
                return None
    return None


def match_environment_fingerprint(
    candidate: Mapping[str, Any],
    cwd: str,
    *,
    current_payload: Optional[Mapping[str, Any]] = None,
) -> tuple[bool, str, Optional[dict[str, Any]]]:
    """Exact current match required. Missing/old/ambiguous → fail closed.

    Returns ``(ok, reason, current_payload_or_none)``.
    """
    prior_fp = job_environment_fingerprint(candidate)
    prior_schema = job_environment_fingerprint_schema(candidate)
    if not prior_fp:
        return False, "environment_fingerprint_missing", None
    if prior_schema is None:
        return False, "environment_fingerprint_schema_missing", None
    if int(prior_schema) != int(ENVIRONMENT_FINGERPRINT_SCHEMA):
        return False, "environment_fingerprint_schema_mismatch", None

    if current_payload is None:
        current, reason = compute_environment_fingerprint(cwd, strict=True)
    else:
        current, reason = dict(current_payload), ""
    if not isinstance(current, dict) or not current.get("fingerprint"):
        return False, reason or "environment_probe_failed", None
    if current.get("complete") is not True:
        return False, "environment_fingerprint_incomplete", dict(current)
    current_schema = current.get("schema")
    try:
        if int(current_schema) != int(ENVIRONMENT_FINGERPRINT_SCHEMA):
            return False, "environment_fingerprint_schema_mismatch", dict(current)
    except (TypeError, ValueError):
        return False, "environment_fingerprint_schema_mismatch", dict(current)

    current_fp = str(current.get("fingerprint") or "").strip()
    if not current_fp:
        return False, "environment_probe_failed", dict(current)
    if current_fp != prior_fp:
        return False, "environment_changed", dict(current)
    return True, "", dict(current)


def normalize_acceptance_criteria(
    raw: Any,
    *,
    max_items: int = 12,
    max_chars: int = 240,
) -> list[str]:
    """Bound explicit acceptance criteria. Never infer from loose prose."""
    if raw is None:
        return []
    items: Sequence[Any]
    if isinstance(raw, str):
        # A single explicit string is allowed; blank → none.
        text = raw.strip()
        return [text[:max_chars]] if text else []
    if isinstance(raw, (list, tuple)):
        items = raw
    else:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if not text:
            continue
        text = text[:max_chars]
        if text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= max_items:
            break
    return out


def format_acceptance_criteria_block(criteria: Sequence[str]) -> str:
    """Prompt/digest block for explicit criteria (empty when none supplied)."""
    clean = normalize_acceptance_criteria(list(criteria or ()))
    if not clean:
        return ""
    lines = ["Acceptance criteria (explicit; do not invent extras):"]
    for item in clean:
        lines.append(f"- {item}")
    return "\n".join(lines)


__all__ = [
    "ENVIRONMENT_FINGERPRINT_SCHEMA",
    "ENVIRONMENT_FINGERPRINT_VERSION",
    "compute_environment_fingerprint",
    "environment_fingerprint_of",
    "environment_fingerprint_schema_of",
    "format_acceptance_criteria_block",
    "job_environment_fingerprint",
    "job_environment_fingerprint_schema",
    "match_environment_fingerprint",
    "normalize_acceptance_criteria",
]
