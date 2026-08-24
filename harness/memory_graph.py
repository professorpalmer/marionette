from __future__ import annotations

"""Memory graph: relations over durable MemoryStore entries.

Nodes are sourced from the existing MemoryStore (one node per memory entry).
Edges are persisted in sqlite (queryable) and an append-only jsonl journal
(audit/replay). Shape matches wiki graph payloads: ``{nodes, edges}``.
"""

import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .memory_store import MemoryEntry, MemoryStore
from .sqlite_journal import configure_sqlite_connection, host_scoped_state_path

DEFAULT_SQLITE_PATH = Path(os.path.expanduser("~/.pmharness/memory_graph.sqlite"))
DEFAULT_JOURNAL_PATH = Path(os.path.expanduser("~/.pmharness/memory_graph.jsonl"))
JOURNAL_FILENAME = "memory_graph.jsonl"


def _pytest_graph_paths(pytest_current_test: str) -> Tuple[Path, Path]:
    """Deterministic per-test graph paths under the OS temp directory."""
    test_id = pytest_current_test.strip().rsplit(" (", 1)[0]
    digest = hashlib.sha256(test_id.encode("utf-8")).hexdigest()[:16]
    root = Path(tempfile.gettempdir()) / "pmharness-memory-graph-tests" / digest
    return root / "memory_graph.sqlite", root / JOURNAL_FILENAME


def default_graph_paths() -> Tuple[Path, Path]:
    """Resolve durable graph paths (never pollutes Home under pytest)."""
    explicit = (os.environ.get("HARNESS_STATE_DIR") or "").strip()
    if explicit:
        base = Path(explicit)
        return base / "memory_graph.sqlite", base / JOURNAL_FILENAME

    pytest_test = (os.environ.get("PYTEST_CURRENT_TEST") or "").strip()
    if pytest_test:
        return _pytest_graph_paths(pytest_test)

    sqlite_path = host_scoped_state_path(DEFAULT_SQLITE_PATH)
    journal_path = host_scoped_state_path(DEFAULT_JOURNAL_PATH)
    return sqlite_path, journal_path


def _entry_to_node(entry: MemoryEntry) -> Dict[str, Any]:
    return {
        "id": entry.id,
        "text": entry.text,
        "category": entry.category,
        "created_at": entry.created_at,
        "source": entry.source,
        "kind": "memory",
    }


def _edge_row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "source": row["source"],
        "target": row["target"],
        "rel": row["rel"],
        "created_at": float(row["created_at"]),
    }


class MemoryGraph:
    """Relation graph layered on an existing MemoryStore.

    Pass explicit ``sqlite_path`` / ``journal_path`` in tests (tmp_path only).
    Default paths live under ``~/.pmharness`` (or HARNESS_STATE_DIR); under
    pytest with no explicit paths, OS-temp per-test paths are used instead.
    """

    def __init__(
        self,
        memory: MemoryStore,
        sqlite_path: Optional[str] = None,
        journal_path: Optional[str] = None,
    ) -> None:
        self.memory = memory
        if sqlite_path is not None or journal_path is not None:
            self.sqlite_path = (
                Path(sqlite_path) if sqlite_path else DEFAULT_SQLITE_PATH
            )
            self.journal_path = (
                Path(journal_path) if journal_path else DEFAULT_JOURNAL_PATH
            )
        else:
            self.sqlite_path, self.journal_path = default_graph_paths()
        self._lock = threading.Lock()
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.sqlite_path), timeout=30.0, check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            configure_sqlite_connection(self._conn, self.sqlite_path)
            self._init_db()

    def _init_db(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS nodes (
                id TEXT PRIMARY KEY,
                text TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT 'general',
                created_at REAL NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL DEFAULT 'memory'
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS edges (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                target TEXT NOT NULL,
                rel TEXT NOT NULL,
                created_at REAL NOT NULL DEFAULT 0
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target)"
        )
        self._conn.commit()

    def _append_journal(self, record: Dict[str, Any]) -> None:
        """Append one UTF-8 LF jsonl record (caller holds lock)."""
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with open(self.journal_path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())

    def _list_edges_locked(self) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT id, source, target, rel, created_at FROM edges "
            "ORDER BY created_at ASC, id ASC"
        ).fetchall()
        return [_edge_row_to_dict(r) for r in rows]

    def _cache_nodes_locked(self, nodes: List[Dict[str, Any]]) -> None:
        """Optional sqlite cache of memory-derived nodes (best-effort)."""
        self._conn.execute("DELETE FROM nodes")
        self._conn.executemany(
            """
            INSERT INTO nodes (id, text, category, created_at, source, kind)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    n["id"],
                    n.get("text") or "",
                    n.get("category") or "general",
                    float(n.get("created_at") or 0),
                    n.get("source") or "",
                    n.get("kind") or "memory",
                )
                for n in nodes
            ],
        )

    def _memory_nodes(self) -> List[Dict[str, Any]]:
        return [_entry_to_node(e) for e in self.memory.list()]

    def graph(self) -> Dict[str, List[Dict[str, Any]]]:
        """Return full graph: memory nodes + persisted edges."""
        nodes = self._memory_nodes()
        with self._lock:
            edges = self._list_edges_locked()
            try:
                self._cache_nodes_locked(nodes)
                self._conn.commit()
            except sqlite3.Error:
                try:
                    self._conn.rollback()
                except sqlite3.Error:
                    pass
        return {"nodes": nodes, "edges": edges}

    def add_edge(
        self,
        source: str,
        target: str,
        rel: str,
    ) -> Dict[str, Any]:
        """Persist a relation between two memory ids (sqlite + jsonl)."""
        src = (source or "").strip()
        tgt = (target or "").strip()
        relation = (rel or "").strip()
        if not src or not tgt or not relation:
            raise ValueError("source, target, and rel are required")
        edge_id = uuid.uuid4().hex
        created_at = time.time()
        edge = {
            "id": edge_id,
            "source": src,
            "target": tgt,
            "rel": relation,
            "created_at": created_at,
        }
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO edges (id, source, target, rel, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (edge_id, src, tgt, relation, created_at),
            )
            self._conn.commit()
            self._append_journal({"event": "add_edge", "edge": edge})
        return edge

    def remove_edge(self, edge_id: str) -> bool:
        """Remove an edge by id. Returns True when a row was deleted."""
        eid = (edge_id or "").strip()
        if not eid:
            return False
        with self._lock:
            cur = self._conn.execute("DELETE FROM edges WHERE id = ?", (eid,))
            self._conn.commit()
            removed = cur.rowcount > 0
            if removed:
                self._append_journal(
                    {"event": "remove_edge", "id": eid, "created_at": time.time()}
                )
            return removed

    def search(self, query: str) -> Dict[str, List[Dict[str, Any]]]:
        """Filter memory nodes by casefold substring, then related edges.

        Matching is against existing MemoryStore text first. Edges whose
        source or target is among matched node ids are included.
        """
        q = (query or "").strip()
        nodes = self._memory_nodes()
        if not q:
            with self._lock:
                edges = self._list_edges_locked()
            return {"nodes": nodes, "edges": edges}

        needle = q.casefold()
        matched = [n for n in nodes if needle in (n.get("text") or "").casefold()]
        matched_ids = {n["id"] for n in matched}
        with self._lock:
            all_edges = self._list_edges_locked()
        related = [
            e for e in all_edges
            if e["source"] in matched_ids or e["target"] in matched_ids
        ]
        return {"nodes": matched, "edges": related}

    def close(self) -> None:
        with self._lock:
            self._conn.close()
