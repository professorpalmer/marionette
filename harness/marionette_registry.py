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
# reject these for missing vision — except DeepSeek V4 Pro (text-only).
_LADDER: tuple[tuple[str, int, tuple[str, ...]], ...] = (
    ("agentic/moonshotai/kimi-k3", 98, ("vision", "detailed-vision")),
    ("agentic/cursor-grok-4.6-high-fast", 94, ("vision",)),
    ("cursor/grok-4-6", 93, ("vision",)),
    ("agentic/cursor-grok-4.5-high-fast", 92, ("vision",)),
    ("cursor/grok-4-5", 91, ("vision",)),
    ("agentic/deepseek/deepseek-v4-pro", 85, ()),
    ("agentic/composer-2.5-fast", 76, ("vision",)),
    ("cursor/composer-2-5", 75, ("vision",)),
    ("agentic/composer-2.5", 74, ("vision",)),
)

# Live OpenCode Go / legacy catalogs flatten provider namespaces
# (``agentic/kimi-k3`` vs ``agentic/moonshotai/kimi-k3``). Match those plus
# ``adapter_model_name`` / ``payload_defaults.model`` aliases.
_LADDER_ALIASES: dict[str, tuple[str, ...]] = {
    "agentic/moonshotai/kimi-k3": (
        "agentic/kimi-k3",
        "kimi-k3",
        "moonshotai/kimi-k3",
        "opencode-go/kimi-k3",
    ),
    "agentic/cursor-grok-4.6-high-fast": (
        "agentic/grok-4.6",
        "agentic/grok-4-6",
        "agentic/grok-4.6-high-fast",
        "agentic/x-ai/grok-4.6",
        "grok-4.6",
        "x-ai/grok-4.6",
        "cursor-grok-4.6-high-fast",
    ),
    "cursor/grok-4-6": (
        "grok-4.6",
        "grok-4-6",
    ),
    "agentic/cursor-grok-4.5-high-fast": (
        "agentic/grok-4.5",
        "agentic/grok-4-5",
        "agentic/grok-4.5-high-fast",
        "grok-4.5",
        "cursor-grok-4.5-high-fast",
    ),
    "cursor/grok-4-5": (
        "grok-4.5",
        "grok-4-5",
    ),
    "agentic/deepseek/deepseek-v4-pro": (
        "agentic/deepseek-v4-pro",
        "deepseek-v4-pro",
        "deepseek/deepseek-v4-pro",
        "opencode-go/deepseek-v4-pro",
        "agentic/deepseek/deepseek-v4-pro-0813",
        "deepseek/deepseek-v4-pro-0813",
        "deepseek-v4-pro-0813",
    ),
    "agentic/composer-2.5-fast": (
        "agentic/composer-2-5-fast",
        "composer-2.5-fast",
        "composer-2-5-fast",
    ),
    "cursor/composer-2-5": (
        "composer-2.5",
        "composer-2-5",
    ),
    "agentic/composer-2.5": (
        "agentic/composer-2-5",
        "composer-2.5",
        "composer-2-5",
    ),
}

# DeepSeek V4 Pro is text-only: never stamp vision / detailed-vision; strip if
# a stale catalog row still carries them.
_TEXT_ONLY_LADDER_IDS = frozenset({
    "agentic/deepseek/deepseek-v4-pro",
    "agentic/deepseek-v4-pro",
    "deepseek-v4-pro",
    "deepseek/deepseek-v4-pro",
    "opencode-go/deepseek-v4-pro",
    "agentic/deepseek/deepseek-v4-pro-0813",
    "deepseek/deepseek-v4-pro-0813",
    "deepseek-v4-pro-0813",
})
_VISION_TAG_NAMES = frozenset({"vision", "detailed-vision"})

# Keep strong-but-not-ladder models below DeepSeek so they do not steal Autopilot.
_DEMOTE: dict[str, int] = {
    "agentic/minimax/minimax-m3": 68,
    "agentic/z-ai/glm-5.2": 80,
    "agentic/deepseek/deepseek-v4-flash": 64,
}

_DEMOTE_ALIASES: dict[str, tuple[str, ...]] = {
    "agentic/minimax/minimax-m3": (
        "agentic/minimax-m3",
        "minimax-m3",
        "minimax/minimax-m3",
    ),
    "agentic/z-ai/glm-5.2": (
        "agentic/glm-5-2",
        "agentic/glm-5.2",
        "glm-5.2",
        "glm-5-2",
        "z-ai/glm-5.2",
    ),
    "agentic/deepseek/deepseek-v4-flash": (
        "agentic/deepseek-v4-flash",
        "deepseek-v4-flash",
        "deepseek/deepseek-v4-flash",
    ),
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


def _row_match_keys(row: dict[str, Any]) -> set[str]:
    """Ids + alias keys a registry row can answer to."""
    keys: set[str] = set()
    mid = str(row.get("id") or "").strip()
    if mid:
        keys.add(mid)
    amn = str(row.get("adapter_model_name") or "").strip()
    if amn:
        keys.add(amn)
        keys.add(f"agentic/{amn}")
    payload = row.get("payload_defaults")
    if isinstance(payload, dict):
        model = str(payload.get("model") or "").strip()
        if model:
            keys.add(model)
            keys.add(f"agentic/{model}")
    return {k for k in keys if k}


def _row_matches_ladder_family(canonical_id: str, row: dict[str, Any]) -> bool:
    """Keep agentic ladder entries from rewriting cursor peers (and vice versa)."""
    family = canonical_id.split("/", 1)[0] if "/" in canonical_id else ""
    row_id = str(row.get("id") or "")
    row_adapter = str(row.get("adapter") or "")
    if family == "agentic":
        return row_id.startswith("agentic/") or row_adapter == "agentic"
    if family == "cursor":
        return row_id.startswith("cursor/") or row_adapter == "cursor"
    return True


def _find_registry_rows(
    models: list[Any],
    by_id: dict[str, dict[str, Any]],
    canonical_id: str,
    aliases: dict[str, tuple[str, ...]],
) -> list[dict[str, Any]]:
    """Resolve all ladder/demote sibling rows (canonical + flattened aliases)."""
    candidates = {canonical_id, *aliases.get(canonical_id, ())}
    found: list[dict[str, Any]] = []
    seen: set[int] = set()
    for key in candidates:
        row = by_id.get(key)
        if row is None or not _row_matches_ladder_family(canonical_id, row):
            continue
        marker = id(row)
        if marker in seen:
            continue
        seen.add(marker)
        found.append(row)
    for model in models:
        if not isinstance(model, dict):
            continue
        if not (_row_match_keys(model) & candidates):
            continue
        if not _row_matches_ladder_family(canonical_id, model):
            continue
        marker = id(model)
        if marker in seen:
            continue
        seen.add(marker)
        found.append(model)
    return found


def _find_registry_row(
    models: list[Any],
    by_id: dict[str, dict[str, Any]],
    canonical_id: str,
    aliases: dict[str, tuple[str, ...]],
) -> Optional[dict[str, Any]]:
    """Resolve a ladder/demote id against canonical, flattened, or alias keys."""
    rows = _find_registry_rows(models, by_id, canonical_id, aliases)
    return rows[0] if rows else None


def _is_text_only_row(canonical_id: str, row: dict[str, Any]) -> bool:
    if canonical_id in _TEXT_ONLY_LADDER_IDS:
        return True
    return bool(_row_match_keys(row) & _TEXT_ONLY_LADDER_IDS)


def apply_marionette_router_ladder(path: Optional[str] = None) -> dict[str, Any]:
    """Apply the Kimi > Grok 4.6 > Grok 4.5 > DeepSeek > Composer score ladder in-place.

    Idempotent. Filename-gated like ``reconcile_shared_models``: never writes a
    non-``marionette-models.json`` catalog (including shared
    ``~/.puppetmaster/models.json``). Returns a small report for diagnostics.
    """
    report: dict[str, Any] = {
        "updated": [], "missing": [], "path": "", "skipped": False,
    }
    raw = (path or os.environ.get("PUPPETMASTER_MODELS_PATH") or "").strip()
    if not raw:
        raw = str(marionette_models_path())
    report["path"] = raw
    p = Path(raw)
    # Filename gate (not resolve()==home path): Windows Path.home() ignores
    # $HOME, and tests / alternate state roots still use marionette-models.json.
    if p.name != MARIONETTE_MODELS_FILENAME:
        report["skipped"] = True
        report["reason"] = "not marionette registry"
        return report
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
        rows = _find_registry_rows(models, by_id, mid, _LADDER_ALIASES)
        if not rows:
            report["missing"].append(mid)
            continue
        for row in rows:
            row_id = str(row.get("id") or mid)
            if int(row.get("capability_score") or 0) != int(score):
                row["capability_score"] = int(score)
                changed = True
                if row_id not in report["updated"]:
                    report["updated"].append(row_id)
            existing_tags = row.get("tags")
            tag_list = (
                [str(t) for t in existing_tags]
                if isinstance(existing_tags, list)
                else []
            )
            text_only = _is_text_only_row(mid, row)
            if text_only:
                stripped = [t for t in tag_list if t not in _VISION_TAG_NAMES]
                if stripped != tag_list:
                    tag_list = stripped
                    changed = True
                    if row_id not in report["updated"]:
                        report["updated"].append(row_id)
            else:
                for tag in tags:
                    if tag not in tag_list:
                        tag_list.append(tag)
                        changed = True
                        if row_id not in report["updated"]:
                            report["updated"].append(row_id)
            row["tags"] = tag_list
    for mid, score in _DEMOTE.items():
        rows = _find_registry_rows(models, by_id, mid, _DEMOTE_ALIASES)
        for row in rows:
            row_id = str(row.get("id") or mid)
            if int(row.get("capability_score") or 0) != int(score):
                row["capability_score"] = int(score)
                changed = True
                if row_id not in report["updated"]:
                    report["updated"].append(row_id)
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
