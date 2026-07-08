from __future__ import annotations

import os
import json
import tempfile
from pathlib import Path

from typing import Any

from harness.memory_store import MemoryStore, MemoryEntry, MEMORY_CHAR_LIMIT
from harness.rule_store import RuleStore
from harness.pilot import build_tools_schema, parse_tool_calls, PilotAction, PilotError
from harness.config import HarnessConfig
from harness.conversation import ConversationalSession


def test_memory_store_utf8_lf_roundtrip(tmp_path):
    """Non-ASCII memory entries round-trip; on-disk bytes use LF only (no CRLF)."""
    path = tmp_path / "memory.json"
    store = MemoryStore(path=str(path))
    text = "User prefers cafe\u00e9 and \u65e5\u672c\u8a9e"
    entry = store.add(text, category="preference", source="user")
    assert entry.text == text

    raw = path.read_bytes()
    assert b"\r\n" not in raw
    # File is valid UTF-8 (json.dumps may escape non-ASCII as \\uXXXX; either is fine).
    decoded = raw.decode("utf-8")
    assert "preference" in decoded

    store2 = MemoryStore(path=str(path))
    entries = store2.list()
    assert len(entries) == 1
    assert entries[0].text == text


def test_memory_store_crud(tmp_path):
    path = tmp_path / "memory.json"
    store = MemoryStore(path=str(path))

    # Test list empty
    assert len(store.list()) == 0
    assert store.total_chars() == 0
    assert not store.over_budget()

    # Test add
    entry1 = store.add("User prefers Python 3.9", category="preference", source="user")
    assert entry1.text == "User prefers Python 3.9"
    assert entry1.category == "preference"
    assert entry1.source == "user"
    assert len(entry1.id) > 0

    # Test list after add
    entries = store.list()
    assert len(entries) == 1
    assert entries[0].id == entry1.id

    # Test dedupe
    entry2 = store.add("  User prefers Python 3.9  ", category="preference", source="agent")
    assert entry2.id == entry1.id
    assert len(store.list()) == 1

    # Test atomic persistence (a second MemoryStore on the same path sees the entries)
    store2 = MemoryStore(path=str(path))
    assert len(store2.list()) == 1
    assert store2.list()[0].id == entry1.id

    # Test update
    ok = store.update(entry1.id, "User prefers Python 3.10")
    assert ok
    assert store.list()[0].text == "User prefers Python 3.10"

    # Test update non-existent
    ok_fake = store.update("fake_id", "New text")
    assert not ok_fake

    # Test total_chars() and over_budget()
    long_text = "a" * (MEMORY_CHAR_LIMIT + 1)
    store.add(long_text)
    assert store.total_chars() > MEMORY_CHAR_LIMIT
    assert store.over_budget()

    # Test remove
    ok_remove = store.remove(entry1.id)
    assert ok_remove
    assert len(store.list()) == 1

    # Test remove non-existent
    ok_remove_fake = store.remove("fake_id")
    assert not ok_remove_fake

    # Test clear
    count = store.clear()
    assert count == 1
    assert len(store.list()) == 0


def test_render_block(tmp_path):
    path = tmp_path / "memory.json"
    store = MemoryStore(path=str(path))
    assert store.render_block() == ""

    store.add("Fact A")
    store.add("Fact B")
    expected = "# Durable memory (persistent across sessions -- user facts and preferences)\n- Fact A\n- Fact B"
    assert store.render_block() == expected


def test_conversational_session_memory_injection(tmp_path, monkeypatch):
    temp_mem_path = tmp_path / "session_memory.json"
    monkeypatch.setattr("harness.memory_store.MEMORY_PATH", temp_mem_path)
    monkeypatch.setattr("harness.conversation.RuleStore", lambda *args, **kwargs: RuleStore(path=str(tmp_path / "rules.json")))

    # Initialize with empty memory
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    session_empty = ConversationalSession(cfg)
    assert "# Durable memory" not in session_empty._history[0]["content"]

    # Initialize with populated memory
    mem_store = MemoryStore(path=str(temp_mem_path))
    mem_store.add("User preference Z")

    session_populated = ConversationalSession(cfg)
    content = session_populated._history[0]["content"]
    assert "# Durable memory (persistent across sessions -- user facts and preferences)" in content
    assert "- User preference Z" in content


def test_build_tools_schema_memory():
    for no_deleg in (False, True):
        schemas = build_tools_schema(no_delegation=no_deleg)
        names = [s["function"]["name"] for s in schemas]
        assert "memory" in names
        memory_schema = [s for s in schemas if s["function"]["name"] == "memory"][0]
        assert memory_schema["function"]["parameters"]["properties"]["action"]["enum"] == ["add", "remove", "update", "list"]


def test_parse_tool_calls_memory():
    tc = [
        {
            "id": "tc_mem_1",
            "type": "function",
            "function": {
                "name": "memory",
                "arguments": json.dumps({
                    "action": "add",
                    "content": "Prefer Python 3.9",
                    "category": "preference"
                })
            }
        }
    ]
    actions = parse_tool_calls(tc)
    assert len(actions) == 1
    act = actions[0]
    assert act.kind == "memory"
    assert act.memory_action == "add"
    assert act.memory_content == "Prefer Python 3.9"
    assert act.memory_category == "preference"
    assert act.tool_call_id == "tc_mem_1"


class _MemoryToolPilot:
    def __init__(self, action_dict):
        self.action_dict = action_dict
        self.calls = 0

    def chat(self, messages: list, *, tools: list | None = None, system: str | None = None) -> Any:
        from pmharness.drivers.openai_compat import DriverResponse
        self.calls += 1
        if self.calls == 1:
            tool_calls = [
                {
                    "id": "tc_mem_test",
                    "type": "function",
                    "function": {
                        "name": "memory",
                        "arguments": json.dumps(self.action_dict)
                    }
                }
            ]
            return DriverResponse(
                text="",
                tokens_out=15,
                latency_ms=1.0,
                meta={
                    "tool_calls": tool_calls,
                    "reasoning": "Need to save memory.",
                    "finish_reason": "tool_calls"
                }
            )
        else:
            return DriverResponse(
                text="Saved preference successfully.",
                tokens_out=10,
                latency_ms=1.0,
                meta={
                    "tool_calls": [],
                    "reasoning": "Done.",
                    "finish_reason": "stop"
                }
            )


def test_driving_memory_tool_through_action_loop(tmp_path, monkeypatch):
    temp_mem_path = tmp_path / "action_memory.json"
    monkeypatch.setattr("harness.memory_store.MEMORY_PATH", temp_mem_path)
    monkeypatch.setattr("harness.conversation.RuleStore", lambda *args, **kwargs: RuleStore(path=str(tmp_path / "rules.json")))

    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    session = ConversationalSession(cfg)

    # Verify memory is empty at start
    assert len(session._memory.list()) == 0

    session.pilot = _MemoryToolPilot({
        "kind": "memory",
        "action": "add",
        "content": "Test durable fact X",
        "category": "fact"
    })

    events = list(session.send("Remember something"))

    # Assert memory actually has the saved entry
    entries = session._memory.list()
    assert len(entries) == 1
    assert entries[0].text == "Test durable fact X"
    assert entries[0].category == "fact"

    # Check that events have action_start and action_result
    kinds = [e.kind for e in events]
    assert "action_start" in kinds
    assert "action_result" in kinds

    action_results = [e for e in events if e.kind == "action_result"]
    assert len(action_results) == 1
    assert "Memory add succeeded" in action_results[0].data.get("artifacts", [{}])[0].get("headline", "")

    # Confirm history has the confirmation message
    found_confirmation = False
    for msg in session._history:
        content = msg.get("content") or ""
        if "Successfully saved to memory" in content and "Test durable fact X" in content:
            found_confirmation = True
            break
    assert found_confirmation, "Should have appended memory tool output back into history"
