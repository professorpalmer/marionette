"""Tests for preserving tool-call card steps in the display transcript."""
import json
import os
import tempfile
import pytest

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession, ConvEvent
from harness.sessions import save_transcript, load_transcript

class FakeResponse:
    def __init__(self, text, error="", tokens_out=10):
        self.text = text
        self.error = error
        self.tokens_out = tokens_out
        self.meta = {}

def test_display_transcript_typed_cards():
    with tempfile.TemporaryDirectory() as tmpdir:
        real_tmp = os.path.realpath(tmpdir)
        cfg = HarnessConfig(repo=real_tmp, swarm_adapter="demo")
        session = ConversationalSession(cfg)

        class FakePilot:
            def __init__(self):
                self.calls = 0
            def complete(self, prompt, system=None):
                self.calls += 1
                if self.calls == 1:
                    return FakeResponse(text=json.dumps({
                        "say": "Let me read a file first.",
                        "actions": [
                            {"kind": "read_file", "path": "test.txt"}
                        ]
                    }))
                else:
                    return FakeResponse(text=json.dumps({
                        "say": "All done!",
                        "actions": []
                    }))

        session.pilot = FakePilot()
        
        # Write the file first so read_file succeeds
        test_file = os.path.join(real_tmp, "test.txt")
        with open(test_file, "w") as f:
            f.write("hello from test file")

        events = list(session.send("howdy"))

        # (a) after a session runs tool calls, export_display_transcript() contains typed card entries
        # with goal/kind/result interleaved in order with messages
        display = session.export_display_transcript()
        
        # Expected display order:
        # 1. User message "howdy"
        # 2. Assistant message "Let me read a file first."
        # 3. Card entry for "read_file"
        # 4. Assistant message "All done!"
        assert len(display) == 4
        
        assert display[0]["type"] == "message"
        assert display[0]["role"] == "user"
        assert display[0]["text"] == "howdy"

        assert display[1]["type"] == "message"
        assert display[1]["role"] == "assistant"
        assert display[1]["text"] == "Let me read a file first."

        assert display[2]["type"] == "card"
        assert display[2]["id"] == "a1"
        assert display[2]["kind"] == "read_file"
        assert display[2]["goal"] == "test.txt"
        assert display[2]["result"] is not None
        assert display[2]["result"]["adapter"] == "local"
        assert isinstance(display[2]["result"]["duration_ms"], int)
        assert len(display[2]["result"]["artifacts"]) == 1
        assert "Read 20 chars" in display[2]["result"]["artifacts"][0]["headline"]

        # Card row must exist in display as soon as action_start fires (result
        # starts null, then fills in-place). Regression: mid-turn transcript
        # polls used to omit in-flight tools and wiped the Investigating UI.

        assert display[3]["type"] == "message"
        assert display[3]["role"] == "assistant"
        assert display[3]["text"] == "All done!"

        # (d) raw history still preserved for context
        history = session.export_history()
        assert len(history) >= 3  # contains user, assistant, tool result, etc.
        assert history[0]["role"] == "user"
        assert history[1]["role"] == "assistant"

def test_save_load_display_transcript_roundtrip_typed(tmp_path):
    state_dir = str(tmp_path)
    session_id = "test-session-typed"

    history_data = [
        {"role": "user", "content": "raw user msg"},
        {"role": "assistant", "content": "raw assistant msg"}
    ]
    display_data = [
        {"type": "message", "role": "user", "text": "clean user msg"},
        {"type": "card", "id": "a1", "kind": "read_file", "goal": "test.txt", "result": {"adapter": "local", "artifacts": []}},
        {"type": "message", "role": "assistant", "text": "clean assistant msg"}
    ]
    job_ids_data = ["job_abc"]

    data = {
        "history": history_data,
        "display": display_data,
        "job_ids": job_ids_data
    }

    # (b) save_transcript+load_transcript round-trips typed entries
    save_transcript(state_dir, session_id, data)
    loaded = load_transcript(state_dir, session_id)
    
    assert loaded["history"] == history_data
    assert loaded["display"] == display_data
    assert loaded["job_ids"] == job_ids_data

def test_old_format_backward_compatibility():
    cfg = HarnessConfig()
    session = ConversationalSession(cfg)

    history_data = [
        {"role": "user", "content": "raw user"},
        {"role": "assistant", "content": "raw assistant"}
    ]
    # (c) old-format display entries (role+text, no type) still load as messages
    old_display_data = [
        {"role": "user", "text": "old user"},
        {"role": "assistant", "text": "old assistant"}
    ]

    session.load_history({
        "history": history_data,
        "display": old_display_data,
        "job_ids": []
    })

    display = session.export_display_transcript()
    # The load/export itself does not necessarily mutate them to have type unless we did it, 
    # but the frontend branches correctly. Let's verify that export returns them intact.
    assert display == old_display_data
