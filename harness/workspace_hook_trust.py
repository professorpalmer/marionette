from __future__ import annotations

"""Workspace hook trust digests.

capability_set_hash covers plugin privilege ids. This module covers the
hooks.json command payload: a saved digest is trusted; a changed command
without a new save is stale_digest and must not run.
"""

import hashlib
import json
import os
from typing import Any, Dict, Iterable, Mapping, Optional

from .secure_files import restrict_to_owner
from .diag import note as _diag


TRUSTED = "trusted"
STALE_DIGEST = "stale_digest"
UNKNOWN = "unknown"


def hook_command_digest(hook: Mapping[str, Any]) -> str:
    payload = {
        "id": str(hook.get("id") or ""),
        "event": str(hook.get("event") or ""),
        "command": hook.get("command"),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def trust_key(hook_id: str) -> str:
    return str(hook_id or "").strip()


def _trust_path(hooks_json: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(hooks_json)), "hook_trust.json")


def load_trust_map(hooks_json: str) -> Dict[str, str]:
    path = _trust_path(hooks_json)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return {}
    rows = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(rows, dict):
        return {}
    out: Dict[str, str] = {}
    for key, digest in rows.items():
        if isinstance(key, str) and key and isinstance(digest, str) and digest:
            out[key] = digest
    return out


def save_trust_map(hooks_json: str, mapping: Mapping[str, str]) -> None:
    path = _trust_path(hooks_json)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {"hooks": dict(mapping)}
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, sort_keys=True)
        os.replace(tmp, path)
        if not restrict_to_owner(path):
            _diag("secure_files.restrict_failed", msg=path)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


def remember_hooks(hooks_json: str, hooks: Iterable[Mapping[str, Any]]) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    for hook in hooks:
        if not isinstance(hook, Mapping):
            continue
        key = trust_key(str(hook.get("id") or ""))
        if not key:
            continue
        mapping[key] = hook_command_digest(hook)
    save_trust_map(hooks_json, mapping)
    return mapping


def evaluate_hook_trust(
    hook: Mapping[str, Any],
    *,
    hooks_json: str,
    trust_map: Optional[Mapping[str, str]] = None,
    seed_unknown: bool = True,
) -> str:
    """Return trusted, stale_digest, or unknown.

    Missing entries are seeded on first evaluate so existing hooks.json
    records keep running after upgrade. A later command edit without
    save_hooks is stale_digest.
    """
    key = trust_key(str(hook.get("id") or ""))
    if not key:
        return UNKNOWN
    digest = hook_command_digest(hook)
    current = dict(trust_map) if trust_map is not None else load_trust_map(hooks_json)
    remembered = current.get(key)
    if remembered is None:
        if not seed_unknown:
            return UNKNOWN
        current[key] = digest
        save_trust_map(hooks_json, current)
        return TRUSTED
    if remembered == digest:
        return TRUSTED
    return STALE_DIGEST
