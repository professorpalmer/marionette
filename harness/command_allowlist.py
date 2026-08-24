"""Persistent command allowlist under HARNESS_STATE_DIR.

After an operator approves a danger command once, matching hashes or exact
command strings skip the pending approval card on later full-auto turns.
Hermetic: path resolves from ``HARNESS_STATE_DIR`` (tests set it to tmp).

Writes record the current ``turn_id`` ContextVar when present. An allowlist
hit authorizes re-execution only — it never invents a safer rewrite.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from typing import Any, Optional

ALLOWLIST_FILENAME = "command_allowlist.json"

_LOCK = threading.Lock()


def _state_dir(explicit: str | None = None) -> str:
    if explicit and str(explicit).strip():
        return os.path.abspath(str(explicit).strip())
    env = (os.environ.get("HARNESS_STATE_DIR") or "").strip()
    if env:
        return os.path.abspath(env)
    return os.path.abspath(os.path.expanduser("~/.pmharness/state"))


def allowlist_path(state_dir: str | None = None) -> str:
    return os.path.join(_state_dir(state_dir), ALLOWLIST_FILENAME)


def _workspace_key(workspace_root: str) -> str:
    raw = (workspace_root or "").strip()
    if not raw:
        return ""
    try:
        return os.path.normcase(os.path.realpath(raw))
    except OSError:
        return os.path.normcase(os.path.abspath(raw))


def command_hash(command: str) -> str:
    return hashlib.sha256((command or "").encode("utf-8")).hexdigest()


def _empty_store() -> dict[str, Any]:
    return {"version": 1, "entries": []}


def _load_unlocked(state_dir: str | None = None) -> dict[str, Any]:
    path = allowlist_path(state_dir)
    if not os.path.isfile(path):
        return _empty_store()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError, TypeError):
        return _empty_store()
    if not isinstance(data, dict):
        return _empty_store()
    entries = data.get("entries")
    if not isinstance(entries, list):
        return _empty_store()
    return {
        "version": 1,
        "entries": [row for row in entries if isinstance(row, dict)],
    }


def _atomic_write(data: dict[str, Any], state_dir: str | None = None) -> None:
    path = allowlist_path(state_dir)
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".cmd_allow_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise


def _entry_matches(
    row: dict[str, Any],
    *,
    command: str,
    digest: str,
    workspace_key: str,
) -> bool:
    row_ws = _workspace_key(str(row.get("workspace_root") or ""))
    # When the caller scopes by workspace, require the same key. Entries with
    # an empty workspace are treated as profile-global and still match.
    if workspace_key and row_ws and row_ws != workspace_key:
        return False
    row_hash = str(row.get("command_hash") or "").strip().lower()
    if digest and row_hash and row_hash == digest:
        return True
    row_cmd = row.get("command")
    if isinstance(row_cmd, str) and command and row_cmd == command:
        return True
    return False


def is_allowlisted(
    command: str,
    *,
    digest: str = "",
    command_hash: str = "",
    workspace_root: str = "",
    state_dir: str | None = None,
) -> bool:
    """True when hash or exact command is on the allowlist."""
    return allowlist_contains(
        command or "",
        state_dir=state_dir,
        workspace_root=workspace_root,
        command_hash=digest or command_hash,
    )


def allowlist_contains(
    command: str,
    state_dir: str | None = None,
    workspace_root: str = "",
    command_hash: str = "",
) -> bool:
    """True when hash or exact command is recorded under the state dir."""
    cmd = command or ""
    digest = (command_hash or "").strip().lower()
    if not digest and cmd:
        digest = hashlib.sha256(cmd.encode("utf-8")).hexdigest()
    if not digest and not cmd.strip():
        return False
    ws = _workspace_key(workspace_root)
    with _LOCK:
        store = _load_unlocked(state_dir)
        for row in store["entries"]:
            if _entry_matches(
                row, command=cmd, digest=digest, workspace_key=ws
            ):
                return True
    return False


def add_allowlisted(
    command: str,
    *,
    command_hash: str = "",
    workspace_root: str = "",
    turn_id: Optional[str] = None,
    state_dir: str | None = None,
) -> dict[str, Any]:
    """Record an approved command. Idempotent for the same hash/command."""
    from .turn_identity import get_turn_id

    cmd = command or ""
    digest = (command_hash or "").strip().lower() or (
        hashlib.sha256(cmd.encode("utf-8")).hexdigest() if cmd else ""
    )
    if not digest:
        return {}
    ws = _workspace_key(workspace_root)
    attributed = str(turn_id or "").strip() or get_turn_id()
    entry = {
        "command_hash": digest,
        "command": cmd,
        "workspace_root": ws,
        "turn_id": attributed,
        "approved_at": time.time(),
    }
    with _LOCK:
        store = _load_unlocked(state_dir)
        entries: list = store["entries"]
        for i, row in enumerate(entries):
            if _entry_matches(
                row, command=cmd, digest=digest, workspace_key=ws
            ):
                entries[i] = entry
                _atomic_write(store, state_dir)
                return dict(entry)
        entries.append(entry)
        _atomic_write(store, state_dir)
        return dict(entry)


def allowlist_add(
    command: str,
    state_dir: str | None = None,
    workspace_root: str = "",
) -> bool:
    """Record ``command`` under the state-dir allowlist. Returns True on write."""
    entry = add_allowlisted(
        command,
        workspace_root=workspace_root,
        state_dir=state_dir,
    )
    return bool(entry)


def load_allowlist(state_dir: str | None = None) -> list[str]:
    """Return exact command strings currently on the allowlist."""
    with _LOCK:
        store = _load_unlocked(state_dir)
    out: list[str] = []
    for row in store["entries"]:
        cmd = row.get("command")
        if isinstance(cmd, str) and cmd.strip():
            out.append(cmd.strip())
    return out


def clear_allowlist_for_tests(state_dir: str | None = None) -> None:
    """Test helper: delete the allowlist file under the current state dir."""
    path = allowlist_path(state_dir)
    with _LOCK:
        if os.path.isfile(path):
            try:
                os.unlink(path)
            except OSError:
                pass
