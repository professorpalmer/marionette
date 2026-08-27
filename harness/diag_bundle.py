"""Redacted diagnostics / state-backup archive for bug reports.

Shared by ``harness doctor --bundle`` and ``GET /api/diagnostics/bundle``.
Reuses ``harness.api.doctor._build_checks`` — does not invent a second health
engine. Secrets are stripped via ``harness.api.redaction`` before anything is
written into the archive or manifest.
"""

from __future__ import annotations

import json
import os
import platform
import re
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, Callable, Optional

DEFAULT_SESSION_LIMIT = 20
PIN_FALLBACK = "puppetmaster-ai==1.22.37"
_PIN_RE = re.compile(r"puppetmaster-ai==[0-9]+(?:\.[0-9]+)*")
_SECRET_KEY_FRAGMENTS = (
    "api_key",
    "apikey",
    "password",
    "secret",
    "token",
    "authorization",
    "bearer",
    "credential",
)


def _state_root() -> str:
    override = (os.environ.get("HARNESS_STATE_DIR") or "").strip()
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".pmharness")


def resolve_puppetmaster_pin() -> str:
    """Installed package version, else a pin string from repo docs, else fallback."""
    try:
        import importlib.metadata as metadata

        ver = str(metadata.version("puppetmaster-ai") or "").strip()
        if ver:
            return f"puppetmaster-ai=={ver}"
    except Exception:
        pass
    try:
        import puppetmaster

        ver = str(getattr(puppetmaster, "__version__", "") or "").strip()
        if ver:
            return f"puppetmaster-ai=={ver}"
    except Exception:
        pass
    for rel in ("CONTRIBUTING.md", "README.md"):
        path = Path(__file__).resolve().parents[1] / rel
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        match = _PIN_RE.search(text)
        if match:
            return match.group(0)
    return PIN_FALLBACK


def collect_os_info() -> dict[str, str]:
    return {
        "platform": platform.platform(),
        "system": platform.system(),
        "release": platform.release(),
    }


def _is_secret_key(key: str) -> bool:
    lowered = str(key or "").strip().lower().replace("-", "_")
    if not lowered:
        return False
    if lowered in ("has_api_key", "api_key_masked", "key_env_var", "masked"):
        return False
    return any(frag in lowered for frag in _SECRET_KEY_FRAGMENTS)


def strip_secrets(value: Any) -> Any:
    """Drop secret-shaped keys and redact remaining string values."""
    from harness.api.redaction import redact_api_secrets, redact_secret_text

    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if _is_secret_key(str(key)):
                continue
            if key in ("env", "headers") and isinstance(item, dict):
                out[key] = {k: "REDACTED" for k in item}
            else:
                out[key] = strip_secrets(item)
        return redact_api_secrets(out)
    if isinstance(value, list):
        return [strip_secrets(item) for item in value]
    if isinstance(value, str):
        return redact_secret_text(value)
    return value


def collect_public_settings() -> dict[str, Any]:
    """Config/settings public shape with secrets stripped."""
    snapshot: dict[str, Any] = {}
    try:
        from harness.config import HarnessConfig

        cfg = HarnessConfig.from_env()
        snapshot = {
            "driver": cfg.driver,
            "reach": cfg.reach,
            "budget": cfg.budget,
            "state_dir": cfg.state_dir,
            "repo": cfg.repo,
            "swarm_adapter": cfg.swarm_adapter,
            "wiki_auto": cfg.wiki_auto,
            "auto_verify": cfg.auto_verify,
            "verify_command": cfg.verify_command,
            "edit_engine": os.environ.get("HARNESS_EDIT_ENGINE", ""),
            "max_pilot_steps": os.environ.get("HARNESS_MAX_PILOT_STEPS", ""),
            "command_timeout": os.environ.get("HARNESS_COMMAND_TIMEOUT", ""),
        }
    except Exception as exc:
        snapshot = {"error": f"settings unavailable: {exc.__class__.__name__}"}
    return strip_secrets(snapshot)


def collect_plugins() -> list[dict[str, Any]]:
    """Plugin names/ids/enabled only — no package paths or digests."""
    try:
        from harness.plugin_registry import discover_plugins, plugin_record_to_dict

        rows = []
        for record in discover_plugins():
            full = plugin_record_to_dict(record)
            rows.append(
                {
                    "id": full.get("id") or getattr(record, "id", ""),
                    "name": full.get("name") or getattr(record, "name", ""),
                    "enabled": bool(full.get("enabled", getattr(record, "enabled", False))),
                }
            )
        return rows
    except Exception:
        return []


def collect_session_rows(
    limit: int = DEFAULT_SESSION_LIMIT,
    *,
    state_dir: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Last N sessions: id + title/mtime only (no transcripts)."""
    root = (state_dir or "").strip() or _state_root()
    path = os.path.join(root, "harness_sessions.json")
    try:
        from harness.sessions import SessionStore

        store = SessionStore(path)
        rows = store.list(include_preview=False)
    except Exception:
        rows = []
    # Newest first when created is present.
    def _sort_key(row: dict[str, Any]) -> float:
        try:
            return float(row.get("created") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    rows = sorted(rows, key=_sort_key, reverse=True)[: max(0, int(limit))]
    out: list[dict[str, Any]] = []
    for row in rows:
        created = row.get("created")
        try:
            mtime = float(created) if created is not None else None
        except (TypeError, ValueError):
            mtime = None
        out.append(
            {
                "id": str(row.get("id") or ""),
                "title": str(row.get("title") or ""),
                "mtime": mtime,
            }
        )
    return out


def _candidate_log_paths(state_dir: Optional[str] = None) -> list[Path]:
    root = Path((state_dir or "").strip() or _state_root())
    names = (
        "diagnostics.log",
        "electron.log",
        "backend.log",
        "harness.log",
    )
    paths = [root / name for name in names]
    paths.append(root / "state" / "codegraph-index.log")
    return paths


def collect_recent_logs(
    *,
    state_dir: Optional[str] = None,
    max_bytes_per_file: int = 256_000,
) -> dict[str, str]:
    """Recent log tails, redacted. Missing files are omitted."""
    from harness.api.redaction import redact_secret_text

    out: dict[str, str] = {}
    for path in _candidate_log_paths(state_dir):
        try:
            if not path.is_file():
                continue
            raw = path.read_bytes()
            if len(raw) > max_bytes_per_file:
                raw = raw[-max_bytes_per_file:]
            text = raw.decode("utf-8", errors="replace")
            out[path.name] = redact_secret_text(text)
        except OSError:
            continue
    return out


def collect_doctor_checks(
    *,
    get_driver: Optional[Callable[[], str]] = None,
    get_reach: Optional[Callable[[], str]] = None,
    get_repo: Optional[Callable[[], str]] = None,
) -> list[dict[str, str]]:
    """Reuse the HTTP diagnostics check engine (single health source)."""
    from harness.api.doctor import DoctorServices, _build_checks

    def _driver() -> str:
        if get_driver is not None:
            return get_driver()
        return (os.environ.get("HARNESS_DRIVER") or "").strip() or "unknown"

    def _reach() -> str:
        if get_reach is not None:
            return get_reach()
        return (os.environ.get("HARNESS_REACH") or "").strip()

    def _repo() -> str:
        if get_repo is not None:
            return get_repo()
        return (os.environ.get("HARNESS_REPO") or "").strip()

    svc = DoctorServices(get_driver=_driver, get_reach=_reach, get_repo=_repo)
    return _build_checks(svc)


def build_manifest(
    *,
    session_limit: int = DEFAULT_SESSION_LIMIT,
    state_dir: Optional[str] = None,
    get_driver: Optional[Callable[[], str]] = None,
    get_reach: Optional[Callable[[], str]] = None,
    get_repo: Optional[Callable[[], str]] = None,
    checks: Optional[list[dict[str, str]]] = None,
) -> dict[str, Any]:
    from harness import __version__

    sessions = collect_session_rows(session_limit, state_dir=state_dir)
    if checks is None:
        checks = collect_doctor_checks(
            get_driver=get_driver,
            get_reach=get_reach,
            get_repo=get_repo,
        )
    plugins = collect_plugins()
    return strip_secrets(
        {
            "version": str(__version__),
            "os": collect_os_info(),
            "pin": resolve_puppetmaster_pin(),
            "plugins": plugins,
            "session_ids": [s["id"] for s in sessions if s.get("id")],
            "sessions": sessions,
            "checks": checks,
            "doctor": {"checks": checks},
            "settings": collect_public_settings(),
            "created_at": int(time.time()),
        }
    )


def _default_outdir() -> str:
    root = _state_root()
    out = os.path.join(root, "diag-bundles")
    try:
        os.makedirs(out, exist_ok=True)
        return out
    except OSError:
        return tempfile.gettempdir()


def write_diag_bundle(
    outdir: Optional[str] = None,
    *,
    session_limit: int = DEFAULT_SESSION_LIMIT,
    state_dir: Optional[str] = None,
    get_driver: Optional[Callable[[], str]] = None,
    get_reach: Optional[Callable[[], str]] = None,
    get_repo: Optional[Callable[[], str]] = None,
    checks: Optional[list[dict[str, str]]] = None,
) -> tuple[str, dict[str, Any]]:
    """Write ``*.zip`` + sibling ``*.manifest.json``. Returns (zip_path, manifest)."""
    dest = (outdir or "").strip() or _default_outdir()
    os.makedirs(dest, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    base = f"marionette-diag-{stamp}"
    zip_path = os.path.join(dest, f"{base}.zip")
    manifest_path = os.path.join(dest, f"{base}.manifest.json")

    root = (state_dir or "").strip() or _state_root()
    manifest = build_manifest(
        session_limit=session_limit,
        state_dir=root,
        get_driver=get_driver,
        get_reach=get_reach,
        get_repo=get_repo,
        checks=checks,
    )
    logs = collect_recent_logs(state_dir=root)
    settings = manifest.get("settings") or collect_public_settings()

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "manifest.json",
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        )
        zf.writestr(
            "settings.json",
            json.dumps(settings, indent=2, sort_keys=True) + "\n",
        )
        zf.writestr(
            "checks.json",
            json.dumps(manifest.get("checks") or [], indent=2) + "\n",
        )
        for name, body in logs.items():
            zf.writestr(f"logs/{name}", body if body.endswith("\n") else body + "\n")

    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)
        fh.write("\n")

    return zip_path, manifest
