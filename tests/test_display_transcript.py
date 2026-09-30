import os
import json
import tempfile
import pytest

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession, ConvEvent, strip_turn_context_trailer
from harness.sessions import save_transcript, load_transcript

_TRAILER_SAMPLE = (
    "\n\n[context for this turn]\n"
    "CODEGRAPH HAS ALREADY BEEN QUERIED FOR THIS TASK. The relevant "
    "symbols are below.\n\n## CodeGraph\n- **Foo**\n"
)

class MockDriverResponse:
    def __init__(self, text="", error=None, tokens_out=10):
        self.text = text
        self.error = error
        self.tokens_out = tokens_out
        self.meta = {}

class MockPilot:
    name = "mock"
    def __init__(self, return_text="Sure, I can help you with that."):
        self.return_text = return_text
        self.chat_calls = []

    def chat(self, messages, tools=None, system=None):
        self.chat_calls.append((messages, system))
        # Return a valid JSON envelope representation of pilot turn if not native,
        # or a clean text response.
        return MockDriverResponse(text='{"say": "Sure, I can help you with that.", "actions": []}')

def test_display_transcript_accumulation():
    cfg = HarnessConfig()
    session = ConversationalSession(cfg)
    session.pilot = MockPilot()  # type: ignore

    # Send a user message and run the pilot loop
    events = list(session.send("how do I build a wooden table?"))
    
    # Check that display transcript has correct user message and clean assistant message
    display = session.export_display_transcript()
    assert len(display) == 2
    from harness.input_receipts import session_input_store
    input_id, = session._history[1]['input_ids']
    receipt = session_input_store(session).get(input_id)
    assert receipt['id'] == input_id
    assert receipt['original_text'] == "how do I build a wooden table?"
    assert receipt['status'] == 'injected'
    assert all(isinstance(row.pop("ts"), int) for row in display)
    assert display[0] == {"type": "message", "role": "user", "text": "how do I build a wooden table?", "input_id": input_id}
    assert display[1] == {"type": "message", "role": "assistant", "text": "Sure, I can help you with that."}

    # Verify that the raw history has the raw pilot output formatting
    history = session.export_history()
    assert len(history) == 2
    assert history[0]["role"] == "user"
    assert history[1]["role"] == "assistant"
    # It has the full clean say text or the acting fallback
    assert history[1]["content"] == "Sure, I can help you with that."

def test_save_load_display_transcript_roundtrip(tmp_path):
    state_dir = str(tmp_path)
    session_id = "test-session-display"
    
    # Mock some display transcript and raw history data
    history_data = [
        {"role": "user", "content": "raw user message with system markers"},
        {"role": "assistant", "content": "raw assistant message"}
    ]
    display_data = [
        {"role": "user", "text": "clean user message"},
        {"role": "assistant", "text": "clean assistant message"}
    ]
    job_ids_data = ["job_1234567890ab", "local-12345678"]

    data = {
        "history": history_data,
        "display": display_data,
        "job_ids": job_ids_data
    }

    save_transcript(state_dir, session_id, data)

    # Verify JSON structure on disk
    p = tmp_path / "transcripts" / f"{session_id}.json"
    assert p.exists()
    
    loaded_data = json.loads(p.read_text(encoding="utf-8"))
    assert loaded_data["history"] == history_data
    assert loaded_data["display"] == display_data
    assert loaded_data["job_ids"] == job_ids_data

    # Load back using load_transcript
    loaded = load_transcript(state_dir, session_id)
    assert isinstance(loaded, dict)
    assert loaded["history"] == history_data
    assert loaded["display"] == display_data
    assert loaded["job_ids"] == job_ids_data

def test_load_history_handles_both_formats():
    cfg = HarnessConfig()
    
    # 1. New dictionary format
    session_new = ConversationalSession(cfg)
    history_data = [
        {"role": "user", "content": "raw user"},
        {"role": "assistant", "content": "raw assistant"}
    ]
    display_data = [
        {"role": "user", "text": "clean user"},
        {"role": "assistant", "text": "clean assistant"}
    ]
    job_ids_data = ["job_123"]
    
    session_new.load_history({
        "history": history_data,
        "display": display_data,
        "job_ids": job_ids_data
    })
    
    assert session_new.export_history() == history_data
    assert session_new.export_display_transcript() == display_data
    assert session_new._session_job_ids == job_ids_data

    # 2. Legacy list format (for backward compatibility)
    session_legacy = ConversationalSession(cfg)
    session_legacy.load_history(history_data)
    
    assert session_legacy.export_history() == history_data
    assert session_legacy.export_display_transcript() == [
        {"type": "message", "role": "user", "text": "raw user"},
        {"type": "message", "role": "assistant", "text": "raw assistant"},
    ]
    assert session_legacy._session_job_ids == []


def test_strip_turn_context_trailer_cuts_at_marker():
    user = "how do I fix the leak?"
    dirty = user + _TRAILER_SAMPLE
    assert strip_turn_context_trailer(dirty) == user
    assert strip_turn_context_trailer(user) == user


def test_strip_turn_context_trailer_idempotent():
    dirty = "ship it" + _TRAILER_SAMPLE
    once = strip_turn_context_trailer(dirty)
    assert once == "ship it"
    assert strip_turn_context_trailer(once) == once
    assert strip_turn_context_trailer(strip_turn_context_trailer(dirty)) == once


def test_strip_turn_context_trailer_standalone_codegraph():
    injection = (
        "CODEGRAPH HAS ALREADY BEEN QUERIED FOR THIS TASK. The relevant "
        "symbols, definitions, and code are provided below."
    )
    assert strip_turn_context_trailer(injection) == ""
    assert strip_turn_context_trailer("  " + injection) == ""
    # User prose before CODEGRAPH is preserved (marker path only).
    kept = "please use this\n\n" + injection
    assert strip_turn_context_trailer(kept) == kept


def test_export_display_strips_trailer_history_keeps_it():
    """Display/UI scrub; pilot history must retain the append-only trailer."""
    cfg = HarnessConfig()
    session = ConversationalSession(cfg)
    dirty = "hello" + _TRAILER_SAMPLE
    session.load_history({
        "history": [
            {"role": "user", "content": dirty},
            {"role": "assistant", "content": "ok"},
        ],
        "display": [
            {"type": "message", "role": "user", "text": dirty},
            {"type": "message", "role": "assistant", "text": "ok"},
        ],
        "job_ids": [],
    })

    history = session.export_history()
    assert history[0]["content"] == dirty
    assert "[context for this turn]" in history[0]["content"]
    assert "CODEGRAPH HAS ALREADY BEEN QUERIED" in history[0]["content"]

    display = session.export_display_transcript()
    assert display[0]["text"] == "hello"
    assert "[context for this turn]" not in display[0]["text"]
    assert "CODEGRAPH" not in display[0]["text"]
    # Stored display row is not mutated — only the export copy is scrubbed.
    assert session._display_transcript[0]["text"] == dirty


def test_display_rows_are_stamped_with_append_time():
    """A turn's length must survive reload: every display row records when it
    was appended, however it got there, and in-place replacement keeps it."""
    import time as _time

    session = ConversationalSession(HarnessConfig())
    before = _time.time() * 1000
    session._display_transcript.append({"type": "message", "role": "user", "text": "hi"})
    session._display_transcript = [{"type": "message", "role": "user", "text": "loaded"}]
    assert "ts" not in session._display_transcript[0]
    session._display_transcript = []
    session._display_transcript += [{"type": "card", "id": "c1", "result": None}]
    session._display_transcript.append({"type": "message", "role": "user", "text": "hi"})
    session._display_transcript.insert(0, {"type": "message", "role": "assistant", "text": "x"})
    rows = session._display_transcript
    assert all(isinstance(r["ts"], int) and r["ts"] >= before - 1 for r in rows)

    stamped = rows[1]["ts"]
    rows[1] = {"type": "message", "role": "user", "text": "edited"}
    assert rows[1]["ts"] == stamped

    kept = {"type": "message", "role": "user", "text": "old", "ts": 42}
    rows.append(kept)
    assert rows[-1]["ts"] == 42
    assert "ts" in session.export_transcript_data()["display"][-1]

    import copy as _copy
    unstamped = type(rows)([{"type": "message", "text": "no time"}])
    assert "ts" not in _copy.deepcopy(unstamped)[0]
