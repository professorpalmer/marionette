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

    If the env is already set (tests / explicit override), preserve that exact
    path, but materialize it when Electron supplied a path before first boot.
    Otherwise copy the shared PM registry once into ``~/.pmharness/`` and
    export the path for this process.
    """
    existing = (os.environ.get("PUPPETMASTER_MODELS_PATH") or "").strip()
    if existing:
        target = Path(existing)
        if target.is_file():
            return existing
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            # An explicit Electron/test path is an isolated target.  Do not
            # import Cursor's catalog into it; an empty valid catalog is the
            # deterministic seed and normal sync will add keyed providers.
            target.write_text(
                json.dumps({"version": 1, "models": []}, indent=2) + "\n",
                encoding="utf-8",
            )
        except Exception as exc:
            _diag("marionette_registry.ensure_explicit", exc)
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


def reconcile_shared_models(path: Optional[str] = None) -> dict[str, Any]:
    """Restore non-agentic rows from the shared PM registry if they vanished.

    ``sync_agentic_registry`` preserves non-agentic entries that are already
    present, but a fresh ``marionette-models.json`` that only ever received
    agentic syncs (or a wipe) leaves the router with no plan/codex/cursor
    ladder peers. Merge shared non-agentic models back in without touching
    ``~/.puppetmaster/models.json``.
    """
    report: dict[str, Any] = {"merged": 0, "path": "", "skipped": False}
    raw = (path or os.environ.get("PUPPETMASTER_MODELS_PATH") or "").strip()
    if not raw:
        raw = str(marionette_models_path())
    report["path"] = raw
    dest = Path(raw)
    # Filename gate (not resolve()==home path): Windows Path.home() ignores
    # $HOME, and tests / alternate state roots still use marionette-models.json.
    if dest.name != MARIONETTE_MODELS_FILENAME:
        report["skipped"] = True
        report["reason"] = "not marionette registry"
        return report
    if not dest.is_file():
        report["error"] = "missing registry file"
        return report
    shared = shared_puppetmaster_models_path()
    # When the marionette registry lives under a non-default home (tests, or a
    # relocated state root), prefer the sibling ~/.puppetmaster next to
    # .pmharness so reconcile still finds the shared plan/cursor peers.
    sibling_shared = dest.parent.parent / ".puppetmaster" / "models.json"
    if sibling_shared.is_file():
        shared = sibling_shared
    if not shared.is_file():
        report["skipped"] = True
        report["reason"] = "no shared registry"
        return report
    try:
        data = json.loads(dest.read_text(encoding="utf-8"))
        shared_data = json.loads(shared.read_text(encoding="utf-8"))
    except Exception as exc:
        _diag("marionette_registry.reconcile_read", exc)
        report["error"] = str(exc)
        return report
    models = data.get("models")
    shared_models = shared_data.get("models")
    if not isinstance(models, list) or not isinstance(shared_models, list):
        report["error"] = "invalid models list"
        return report
    non_agentic = [
        m for m in models
        if isinstance(m, dict) and m.get("adapter") != "agentic"
    ]
    if non_agentic:
        report["skipped"] = True
        report["reason"] = "non-agentic already present"
        return report
    shared_non_agentic = [
        m for m in shared_models
        if isinstance(m, dict) and m.get("adapter") != "agentic" and m.get("id")
    ]
    if not shared_non_agentic:
        report["skipped"] = True
        report["reason"] = "shared has no non-agentic rows"
        return report
    agentic = [
        m for m in models
        if isinstance(m, dict) and m.get("adapter") == "agentic"
    ]
    data["models"] = shared_non_agentic + agentic
    try:
        dest.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except Exception as exc:
        _diag("marionette_registry.reconcile_write", exc)
        report["error"] = str(exc)
        return report
    report["merged"] = len(shared_non_agentic)
    return report


def boot_marionette_registry() -> None:
    """Boot hook: isolate env, reconcile catalog, apply ladder (best-effort)."""
    try:
        ensure_marionette_models_env()
        reconcile_shared_models()
        apply_marionette_router_ladder()
    except Exception as exc:
        _diag("marionette_registry.boot", exc)
