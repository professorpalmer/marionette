"""Session-partitioned store behind ``{state_dir}/swarm_local_jobs.json``.

The on-disk format is unchanged -- ``{"jobs": [row, ...]}`` in one file -- so
``internal_uri`` and every other reader of the legacy file keep working.

Ownership: a row belongs to the session named by its ``session_id`` (``""`` for
legacy rows without one). Each runner owns exactly one partition, its harness
session. ``write(session_id, rows)`` replaces only that partition and carries
every other partition over from this store, so a runner can never persist a
stale snapshot of another session's rows (no last-writer-wins across runners).

One store instance exists per file path per process, and its lock serializes
the read-merge-write. Before merging, the file signature (inode, mtime, size)
is compared with the one this store last wrote or read; any external write is
re-parsed first. A second backend process on the same state dir is still not
supported, but its writes are merged rather than blindly overwritten.

Cost model: the file is parsed once per external change. Each partition is
cached as serialized JSON, so a persist re-serializes only the writer's own
rows, and a new runner (cold session switch) parses only its own partition.
Other partitions are parsed lazily, once per change, for the all-scope
metadata index. Parsing a legacy file bounds oversized worker provenance
(``bound_live_dirty_provenance``) and marks the store dirty so the next write
shrinks the file.
"""
from __future__ import annotations

import json
import os
import threading
from typing import Dict, List, Optional, Tuple

from .provenance_sanitize import artifact_worker_provenance, bound_live_dirty_provenance

_COMMAND_KINDS = ("run_command", "run_command_batch")
_COMMAND_ROLES = ("command", "command_batch")
_COMMAND_TERMINAL = frozenset({"completed", "failed", "cancelled", "timeout", "truncated"})
TERMINAL_STATUSES = _COMMAND_TERMINAL

# Bound provider history per session; command identities must survive action
# replay, so unresolved command rows are never pruned. Terminal command
# receipts (a batch with its children counts once) keep the newest N.
HISTORY_CAP = 200
COMMAND_RECEIPT_CAP = 500


def _is_command_row(row: dict) -> bool:
    return row.get("job_kind") in _COMMAND_KINDS or row.get("role") in _COMMAND_ROLES


def _row_time(row: dict) -> float:
    for key in ("updated_at", "created_at"):
        value = row.get(key)
        if type(value) in (int, float):
            return float(value)
    return 0.0


def cap_session_rows(rows: List[dict]) -> List[dict]:
    """Apply the provider-history and terminal-command caps to one session."""
    commands = [r for r in rows if _is_command_row(r)]
    history = [r for r in rows if not _is_command_row(r)]
    history.sort(key=lambda r: r.get("created_at") or 0.0)
    history = history[-HISTORY_CAP:]
    units: Dict[str, List[dict]] = {}
    for row in commands:
        key = str(row.get("batch_id") or row.get("id") or "")
        units.setdefault(key, []).append(row)
    terminal = [
        key for key, members in units.items()
        if all(str(m.get("status") or "") in _COMMAND_TERMINAL for m in members)
    ]
    if len(terminal) > COMMAND_RECEIPT_CAP:
        terminal.sort(key=lambda key: max(_row_time(m) for m in units[key]))
        dropped = set(terminal[:-COMMAND_RECEIPT_CAP])
        commands = [r for r in commands
                    if str(r.get("batch_id") or r.get("id") or "") not in dropped]
    return commands + history


def migrate_row(row: dict) -> bool:
    """Bound legacy worker provenance in place. Returns True if it changed."""
    changed = False
    prov = row.get("worker_provenance")
    if isinstance(prov, dict):
        bounded = bound_live_dirty_provenance(prov)
        if bounded != prov:
            row["worker_provenance"] = bounded
            changed = True
    for art in row.get("artifacts") or []:
        if not isinstance(art, dict):
            continue
        prov = art.get("worker_provenance")
        if isinstance(prov, dict):
            slim = artifact_worker_provenance(prov)
            if slim != prov:
                art["worker_provenance"] = slim
                changed = True
    return changed


def _serialize(rows: List[dict]) -> str:
    # Rows joined without the surrounding brackets so partitions concatenate.
    return json.dumps(rows)[1:-1]


class _Partition:
    __slots__ = ("text", "rows", "shared", "version")

    def __init__(self, text: str, rows: Optional[List[dict]], version: int):
        self.text = text
        self.rows = rows  # Parsed cache; None once handed to an owner.
        self.shared = False  # Rows exposed to foreign readers stay read-only.
        self.version = version


class LocalJobsStore:
    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()
        self._signature: Optional[Tuple[int, int, int]] = None
        self._parts: Dict[str, _Partition] = {}
        self._version = 0
        self._dirty = False
        self.status = "missing"  # missing | ok | corrupt
        self.malformed = False

    def _stat(self) -> Optional[Tuple[int, int, int]]:
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            return None
        return (st.st_ino, st.st_mtime_ns, st.st_size)

    def _next_version(self) -> int:
        self._version += 1
        return self._version

    def _sync_locked(self) -> None:
        signature = self._stat()
        if signature is not None and signature == self._signature:
            return
        self._signature = signature
        self._parts = {}
        self._dirty = False
        self.malformed = False
        if signature is None:
            self.status = "missing"
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            self.status = "corrupt"
            return
        jobs = data.get("jobs") if isinstance(data, dict) else None
        if not isinstance(jobs, list):
            self.status = "corrupt"
            return
        self.status = "ok"
        grouped: Dict[str, List[dict]] = {}
        for row in jobs:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
                self.malformed = True
                self._dirty = True
                continue
            if migrate_row(row):
                self._dirty = True
            grouped.setdefault(str(row.get("session_id") or ""), []).append(row)
        for sid, rows in grouped.items():
            self._parts[sid] = _Partition(_serialize(rows), rows, self._next_version())

    def load(self, session_id: str) -> Tuple[List[dict], str, bool]:
        """Rows owned by ``session_id`` (fresh objects the caller may mutate),
        plus the file status and whether malformed rows were dropped."""
        with self.lock:
            self._sync_locked()
            part = self._parts.get(session_id)
            if part is None:
                return [], self.status, self.malformed
            if part.rows is not None and not part.shared:
                rows, part.rows = part.rows, None
            else:
                rows = json.loads("[" + part.text + "]")
            return rows, self.status, self.malformed

    def foreign(self, session_id: str) -> Dict[str, Tuple[int, List[dict]]]:
        """``{session: (version, rows)}`` for every other partition.

        Rows are shared, read-only snapshots; callers must never mutate them.
        """
        with self.lock:
            self._sync_locked()
            out = {}
            for sid, part in self._parts.items():
                if sid == session_id:
                    continue
                if part.rows is None:
                    part.rows = json.loads("[" + part.text + "]")
                part.shared = True
                out[sid] = (part.version, part.rows)
            return out

    def write(self, session_id: str, rows: List[dict]) -> None:
        """Replace ``session_id``'s partition and atomically rewrite the file.

        Rows stamped with another session (which a runner should not hold) are
        upserted by id into their own partition rather than dropped. Skips the
        disk write when nothing changed. Raises on I/O failure.
        """
        own: List[dict] = []
        strays: Dict[str, List[dict]] = {}
        for row in rows:
            sid = str(row.get("session_id") or "")
            if sid == session_id:
                own.append(row)
            else:
                strays.setdefault(sid, []).append(row)
        own_text = _serialize(cap_session_rows(own))
        with self.lock:
            self._sync_locked()
            # Stage on a copy: the cache must never claim what the disk refused
            # (a failed terminal barrier would otherwise replay as completed).
            parts = dict(self._parts)
            # A missing file with nothing to hold stays missing.
            changed = self._dirty or self.status == "corrupt"
            current = parts.get(session_id)
            if (current.text if current is not None else "") != own_text:
                changed = True
                if own_text:
                    parts[session_id] = _Partition(own_text, None, self._next_version())
                else:
                    parts.pop(session_id, None)
            for sid, extra in strays.items():
                part = parts.get(sid)
                merged = list(part.rows if part is not None and part.rows is not None
                              else json.loads("[" + part.text + "]") if part is not None else [])
                ids = {str(r.get("id")) for r in extra}
                merged = [r for r in merged if str(r.get("id")) not in ids] + extra
                parts[sid] = _Partition(_serialize(cap_session_rows(merged)), None,
                                        self._next_version())
                changed = True
            if not changed:
                return
            body = ", ".join(p.text for p in parts.values() if p.text)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                f.write('{"jobs": [' + body + "]}")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
            self._parts = parts
            self._signature = self._stat()
            self._dirty = False
            self.status = "ok"
            self.malformed = False


_stores: Dict[str, LocalJobsStore] = {}
_stores_lock = threading.Lock()


def local_jobs_store(path: str) -> LocalJobsStore:
    """The process-wide store for ``path`` (shared by every session runner)."""
    key = os.path.normcase(os.path.realpath(path))
    with _stores_lock:
        store = _stores.get(key)
        if store is None:
            store = _stores[key] = LocalJobsStore(path)
        return store


def forget_local_jobs_store(path: str) -> None:
    """Drop the cached store for ``path`` (tests and state-dir teardown)."""
    key = os.path.normcase(os.path.realpath(path))
    with _stores_lock:
        _stores.pop(key, None)
