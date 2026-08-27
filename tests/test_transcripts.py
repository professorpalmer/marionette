import os
import json
import tempfile
import pytest
from pathlib import Path

from harness.conversation import ConversationalSession
from harness.config import HarnessConfig
from harness.sessions import save_transcript, load_transcript
from pmharness.drivers.base import chat_completions_messages, stamp_assistant_phase


def test_export_load_history_roundtrip():
    cfg = HarnessConfig()
    session = ConversationalSession(cfg)
    
    # Initially we have just the system prompt at index 0
    assert len(session._history) == 1
    assert session._history[0]["role"] == "system"
    
    # Export empty history
    assert session.export_history() == []
    
    # Add some mock conversational turns
    turns = [
        {"role": "user", "content": "hello pilot"},
        {"role": "assistant", "content": "hello human"}
    ]
    session._history.extend(turns)
    
    # Export history
    exported = session.export_history()
    assert exported == turns
    
    # Now create another session, and load that history
    session2 = ConversationalSession(cfg)
    session2._history[0]["content"] = "different freshly-built system prompt"
    
    session2.load_history(exported)
    
    # Check that system prompt at index 0 is preserved
    assert len(session2._history) == 3
    assert session2._history[0]["role"] == "system"
    assert session2._history[0]["content"] == "different freshly-built system prompt"
    # and subsequent messages are loaded correctly
    assert session2._history[1:] == turns


def test_export_load_history_roundtrip_preserves_assistant_phase():
    """Issue #228: known Responses phase survives transcript export/reload."""
    cfg = HarnessConfig()
    session = ConversationalSession(cfg)
    turns = [
        {"role": "user", "content": "inspect the sql"},
        {
            "role": "assistant",
            "content": "I'll inspect the SQL first.",
            "phase": "commentary",
        },
        {
            "role": "assistant",
            "content": "The employee filter explains it.",
            "phase": "final_answer",
        },
        {"role": "assistant", "content": "legacy answer"},
    ]
    session._history.extend(turns)
    exported = session.export_history()
    assert exported[1]["phase"] == "commentary"
    assert exported[2]["phase"] == "final_answer"
    assert "phase" not in exported[3]

    session2 = ConversationalSession(cfg)
    session2.load_history(exported)
    assert session2._history[2]["phase"] == "commentary"
    assert session2._history[3]["phase"] == "final_answer"
    assert "phase" not in session2._history[4]


def test_stamp_assistant_phase_only_known_assistant_values():
    assistant = {"role": "assistant", "content": "hi"}
    stamp_assistant_phase(assistant, " commentary ")
    assert assistant["phase"] == "commentary"
    stamp_assistant_phase(assistant, "analysis")
    assert assistant["phase"] == "commentary"
    user = {"role": "user", "content": "hi"}
    stamp_assistant_phase(user, "final_answer")
    assert "phase" not in user
    legacy = {"role": "assistant", "content": "hi"}
    stamp_assistant_phase(legacy, None)
    assert "phase" not in legacy


def test_chat_completions_messages_drop_phase_without_mutating_history():
    original = {
        "role": "assistant",
        "content": "I'll inspect the SQL first.",
        "phase": "commentary",
    }
    projected = chat_completions_messages([original])
    assert "phase" not in projected[0]
    assert projected[0]["content"] == "I'll inspect the SQL first."
    assert original["phase"] == "commentary"
    assert projected[0] is not original


def test_save_load_transcript_to_disk(tmp_path):
    state_dir = str(tmp_path)
    session_id = "test-session-123"
    messages = [
        {"role": "user", "content": "ping"},
        {"role": "assistant", "content": "pong"}
    ]
    
    save_transcript(state_dir, session_id, messages)
    
    p = tmp_path / "transcripts" / f"{session_id}.json"
    assert p.exists()
    
    loaded = load_transcript(state_dir, session_id)
    assert loaded == messages


def test_save_load_transcript_corrupt_or_missing(tmp_path):
    state_dir = str(tmp_path)
    
    # Missing file
    loaded_missing = load_transcript(state_dir, "non-existent")
    assert loaded_missing == []
    
    # Corrupt file
    session_id = "corrupt-session"
    trans_dir = tmp_path / "transcripts"
    trans_dir.mkdir(parents=True, exist_ok=True)
    p = trans_dir / f"{session_id}.json"
    p.write_text("{this is corrupt json: [}")
    
    loaded_corrupt = load_transcript(state_dir, session_id)
    assert loaded_corrupt == []
