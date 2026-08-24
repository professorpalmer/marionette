"""Hermetic MemoryGraph tests (v0.9.308)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from harness.api.skills import SkillsServices, get_memory_graph, post_memory_graph_edge
from harness.memory_graph import MemoryGraph
from harness.memory_store import MemoryStore


def _graph(tmp_path: Path, store: Optional[MemoryStore] = None) -> MemoryGraph:
    memory = store or MemoryStore(path=str(tmp_path / "memory.json"))
    return MemoryGraph(
        memory=memory,
        sqlite_path=str(tmp_path / "memory_graph.sqlite"),
        journal_path=str(tmp_path / "memory_graph.jsonl"),
    )


def test_empty_graph(tmp_path: Path) -> None:
    g = _graph(tmp_path)
    try:
        payload = g.graph()
        assert payload["nodes"] == []
        assert payload["edges"] == []
    finally:
        g.close()


def test_nodes_from_existing_memory_store(tmp_path: Path) -> None:
    store = MemoryStore(path=str(tmp_path / "memory.json"))
    a = store.add("User prefers Python 3.9", category="preference", source="user")
    b = store.add("Repo root is marionette", category="fact", source="agent")
    g = _graph(tmp_path, store)
    try:
        payload = g.graph()
        ids = {n["id"] for n in payload["nodes"]}
        assert ids == {a.id, b.id}
        texts = {n["text"] for n in payload["nodes"]}
        assert "User prefers Python 3.9" in texts
        assert payload["edges"] == []
    finally:
        g.close()


def test_add_edge_persists_sqlite_and_jsonl(tmp_path: Path) -> None:
    store = MemoryStore(path=str(tmp_path / "memory.json"))
    a = store.add("alpha fact", category="fact")
    b = store.add("beta fact", category="fact")
    sqlite_path = tmp_path / "memory_graph.sqlite"
    journal_path = tmp_path / "memory_graph.jsonl"
    g = MemoryGraph(
        memory=store,
        sqlite_path=str(sqlite_path),
        journal_path=str(journal_path),
    )
    try:
        edge = g.add_edge(a.id, b.id, "related_to")
        assert edge["source"] == a.id
        assert edge["target"] == b.id
        assert edge["rel"] == "related_to"
        assert isinstance(edge["created_at"], float)
        assert sqlite_path.exists()
        assert journal_path.exists()
        raw = journal_path.read_bytes()
        assert b"\r\n" not in raw
        rec = json.loads(journal_path.read_text(encoding="utf-8").strip().splitlines()[-1])
        assert rec["event"] == "add_edge"
        assert rec["edge"]["id"] == edge["id"]

        g2 = MemoryGraph(
            memory=store,
            sqlite_path=str(sqlite_path),
            journal_path=str(journal_path),
        )
        try:
            again = g2.graph()
            assert len(again["edges"]) == 1
            assert again["edges"][0]["id"] == edge["id"]
            assert again["edges"][0]["rel"] == "related_to"
        finally:
            g2.close()
    finally:
        g.close()


def test_search_existing_first_includes_related_edges(tmp_path: Path) -> None:
    store = MemoryStore(path=str(tmp_path / "memory.json"))
    hit = store.add("Chicago timezone preference", category="preference")
    other = store.add("Unrelated shell note", category="environment")
    neighbor = store.add("America/Chicago office hours", category="fact")
    g = _graph(tmp_path, store)
    try:
        edge = g.add_edge(hit.id, neighbor.id, "same_topic")
        g.add_edge(other.id, neighbor.id, "mentions")
        found = g.search("chicago")
        found_ids = {n["id"] for n in found["nodes"]}
        assert hit.id in found_ids
        assert neighbor.id in found_ids
        assert other.id not in found_ids
        edge_ids = {e["id"] for e in found["edges"]}
        assert edge["id"] in edge_ids
        # Edge from non-matching other is excluded when neither endpoint matched
        # wait - other is not matched, neighbor IS matched (chicago in text),
        # so the other->neighbor edge IS related because target is matched.
        assert len(found["edges"]) == 2
    finally:
        g.close()


def test_get_memory_graph_handler_no_server(tmp_path: Path) -> None:
    store = MemoryStore(path=str(tmp_path / "memory.json"))
    a = store.add("durable latch fact")
    b = store.add("other memory")
    graph = _graph(tmp_path, store)
    try:
        graph.add_edge(a.id, b.id, "links")
        svc = SkillsServices(
            skills=None,
            rules=None,
            memory=store,
            get_pilot=lambda: None,
            memory_char_limit=4000,
            memory_graph=graph,
        )
        code, payload = get_memory_graph("", svc)
        assert code == 200
        assert "nodes" in payload and "edges" in payload
        assert len(payload["nodes"]) == 2
        assert len(payload["edges"]) == 1

        code2, filtered = get_memory_graph("latch", svc)
        assert code2 == 200
        assert len(filtered["nodes"]) == 1
        assert filtered["nodes"][0]["id"] == a.id
        assert len(filtered["edges"]) == 1

        code3, edge = post_memory_graph_edge(
            {"source": b.id, "target": a.id, "rel": "inverse"}, svc
        )
        assert code3 == 200
        assert edge["rel"] == "inverse"
    finally:
        graph.close()
