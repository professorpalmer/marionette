"""v0.9.308: memory graph over existing MemoryStore + /api/memory/graph."""
from __future__ import annotations

import json
from pathlib import Path

from harness.api.skills import SkillsServices, get_memory_graph
from harness.memory_graph import MemoryGraph, JOURNAL_FILENAME
from harness.memory_store import MemoryStore
import harness


def test_version_is_0_9_308() -> None:
    assert harness.__version__ == "0.9.308"


def test_memory_graph_empty_and_nodes_from_store(tmp_path: Path) -> None:
    store = MemoryStore(path=str(tmp_path / "memory.json"))
    g = MemoryGraph(
        memory=store,
        sqlite_path=str(tmp_path / "g.sqlite"),
        journal_path=str(tmp_path / JOURNAL_FILENAME),
    )
    try:
        empty = g.graph()
        assert empty == {"nodes": [], "edges": []}
        entry = store.add("prime 308 memory node")
        full = g.graph()
        assert len(full["nodes"]) == 1
        assert full["nodes"][0]["id"] == entry.id
        assert full["nodes"][0]["kind"] == "memory"
        assert full["edges"] == []
    finally:
        g.close()


def test_memory_graph_edge_persist_and_search(tmp_path: Path) -> None:
    store = MemoryStore(path=str(tmp_path / "memory.json"))
    a = store.add("alpha chicago fact")
    b = store.add("beta unrelated")
    journal = tmp_path / JOURNAL_FILENAME
    g = MemoryGraph(
        memory=store,
        sqlite_path=str(tmp_path / "g.sqlite"),
        journal_path=str(journal),
    )
    try:
        edge = g.add_edge(a.id, b.id, "related_to")
        assert journal.exists()
        rec = json.loads(journal.read_text(encoding="utf-8").strip().splitlines()[-1])
        assert rec["event"] == "add_edge"
        assert rec["edge"]["id"] == edge["id"]

        hit = g.search("CHICAGO")
        assert [n["id"] for n in hit["nodes"]] == [a.id]
        assert [e["id"] for e in hit["edges"]] == [edge["id"]]
    finally:
        g.close()


def test_get_memory_graph_api_shape(tmp_path: Path) -> None:
    store = MemoryStore(path=str(tmp_path / "memory.json"))
    store.add("api graph node")
    g = MemoryGraph(
        memory=store,
        sqlite_path=str(tmp_path / "g.sqlite"),
        journal_path=str(tmp_path / JOURNAL_FILENAME),
    )
    try:
        svc = SkillsServices(
            skills=None,
            rules=None,
            memory=store,
            get_pilot=lambda: None,
            memory_char_limit=4000,
            memory_graph=g,
        )
        code, payload = get_memory_graph("", svc)
        assert code == 200
        assert set(payload.keys()) >= {"nodes", "edges"}
        assert isinstance(payload["nodes"], list)
        assert isinstance(payload["edges"], list)
        assert len(payload["nodes"]) == 1
    finally:
        g.close()
