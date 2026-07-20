from __future__ import annotations

"""Marionette-owned model registry isolation + router ladder.

Cursor MCP and the Puppetmaster CLI default to ``~/.puppetmaster/models.json``.
Marionette must not rewrite that file while the user also runs Cursor — copy
into ``~/.pmharness/marionette-models.json`` and point
``PUPPETMASTER_MODELS_PATH`` at it for this process (and Electron children).
"""

import json
import os
import shutil
from pathlib import Path
from typing import Any, Optional

from .diag import note as _diag

MARIONETTE_MODELS_FILENAME = "marionette-models.json"

# Preferred Marionette labor ladder (capability_score). Higher = preferred under
# balanced/quality auto_route. Vision tags required so analysis peels never
# reject these for missing vision.
_LADDER: tuple[tuple[str, int, tuple[str, ...]], ...] = (
    ("agentic/moonshotai/kimi-k3", 98, ("vision", "detailed-vision")),
    ("agentic/cursor-grok-4.5-high-fast", 92, ("vision",)),
    ("cursor/grok-4-5", 91, ("vision",)),
    ("agentic/deepseek/deepseek-v4-pro", 85, ("vision",)),
    ("agentic/composer-2.5-fast", 76, ("vision",)),
    ("cursor/composer-2-5", 75, ("vision",)),
    ("agentic/composer-2.5", 74, ("vision",)),
)

# Keep strong-but-not-ladder models below DeepSeek so they do not steal Autopilot.
_DEMOTE: dict[str, int] = {
    "agentic/minimax/minimax-m3": 68,
    "agentic/z-ai/glm-5.2": 80,
    "agentic/deepseek/deepseek-v4-flash": 64,
}


def marionette_models_path() -> Path:
    return Path.home() / ".pmharness" / MARIONETTE_MODELS_FILENAME


def shared_puppetmaster_models_path() -> Path:
    return Path.home() / ".puppetmaster" / "models.json"


def ensure_marionette_models_env() -> str:
    """Ensure ``PUPPETMASTER_MODELS_PATH`` points at the Marionette-only registry.

    If the env is already set (tests / explicit override), leave it alone.
    Otherwise copy the shared PM registry once into ``~/.pmharness/`` and export
    the path for this process.
    """
    existing = (os.environ.get("PUPPETMASTER_MODELS_PATH") or "").strip()
    if existing:
        return existing
    dest = marionette_models_path()
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.is_file():
            src = shared_puppetmaster_models_path()
            if src.is_file():
                shutil.copy2(src, dest)
            else:
                dest.write_text(
                    json.dumps({"version": 1, "models": []}, indent=2) + "\n",
                    encoding="utf-8",
                )
        os.environ["PUPPETMASTER_MODELS_PATH"] = str(dest)
    except Exception as exc:
        _diag("marionette_registry.ensure", exc)
        return existing
    return str(dest)


def apply_marionette_router_ladder(path: Optional[str] = None) -> dict[str, Any]:
    """Apply the Kimi > Grok > DeepSeek > Composer score ladder in-place.

    Idempotent. Never touches ``~/.puppetmaster/models.json`` unless that path
    is explicitly passed (tests). Returns a small report for diagnostics.
    """
    report: dict[str, Any] = {"updated": [], "missing": [], "path": ""}
    raw = (path or os.environ.get("PUPPETMASTER_MODELS_PATH") or "").strip()
    if not raw:
        raw = str(marionette_models_path())
    report["path"] = raw
    p = Path(raw)
    if not p.is_file():
        report["error"] = "missing registry file"
        return report
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        _diag("marionette_registry.read", exc)
        report["error"] = str(exc)
        return report
    models = data.get("models")
    if not isinstance(models, list):
        report["error"] = "invalid models list"
        return report
    by_id = {
        str(m.get("id") or ""): m
        for m in models
        if isinstance(m, dict) and m.get("id")
    }
    changed = False
    for mid, score, tags in _LADDER:
        row = by_id.get(mid)
        if row is None:
            report["missing"].append(mid)
            continue
        if int(row.get("capability_score") or 0) != int(score):
            row["capability_score"] = int(score)
            changed = True
            report["updated"].append(mid)
        existing_tags = row.get("tags")
        tag_list = [str(t) for t in existing_tags] if isinstance(existing_tags, list) else []
        for tag in tags:
            if tag not in tag_list:
                tag_list.append(tag)
                changed = True
        row["tags"] = tag_list
    for mid, score in _DEMOTE.items():
        row = by_id.get(mid)
        if row is None:
            continue
        if int(row.get("capability_score") or 0) != int(score):
            row["capability_score"] = int(score)
            changed = True
            report["updated"].append(mid)
    if changed:
        try:
            p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        except Exception as exc:
            _diag("marionette_registry.write", exc)
            report["error"] = str(exc)
    return report


def boot_marionette_registry() -> None:
    """Boot hook: isolate env, then apply the Marionette ladder (best-effort)."""
    try:
        ensure_marionette_models_env()
        apply_marionette_router_ladder()
    except Exception as exc:
        _diag("marionette_registry.boot", exc)
