"""Pilot action_start ids prefer PilotAction.tool_call_id over a{n}."""
from __future__ import annotations

import json
import os

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession


class _FakeResponse:
    def __init__(self, text, error="", tokens_out=10):
        self.text = text
        self.error = error
        self.tokens_out = tokens_out
        self.meta = {}


def test_action_start_uses_tool_call_id_when_present(tmp_path):
    real_tmp = os.path.realpath(str(tmp_path))
    path = os.path.join(real_tmp, "note.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("hi")
    cfg = HarnessConfig(repo=real_tmp, swarm_adapter="demo")
    session = ConversationalSession(cfg)

    class FakePilot:
        def __init__(self):
            self.calls = 0

        def complete(self, prompt, system=None):
            self.calls += 1
            if self.calls == 1:
                return _FakeResponse(text=json.dumps({
                    "say": "reading",
                    "actions": [{
                        "kind": "read_file",
                        "path": "note.txt",
                        "tool_call_id": "call_stable_99",
                    }],
                }))
            return _FakeResponse(text=json.dumps({
                "say": "done",
                "actions": [],
            }))

    session.pilot = FakePilot()
    events = list(session.send("read it"))
    starts = [e for e in events if e.kind == "action_start"]
    assert starts, "expected action_start"
    assert starts[0].data["id"] == "call_stable_99"
    assert starts[0].data.get("call_id") == "call_stable_99"
    display = session.export_display_transcript()
    cards = [d for d in display if d.get("type") == "card"]
    assert cards[0]["id"] == "call_stable_99"
    assert cards[0].get("call_id") == "call_stable_99"


def test_action_start_falls_back_to_a_seq_without_tool_call_id(tmp_path):
    real_tmp = os.path.realpath(str(tmp_path))
    path = os.path.join(real_tmp, "note.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("hi")
    cfg = HarnessConfig(repo=real_tmp, swarm_adapter="demo")
    session = ConversationalSession(cfg)

    class FakePilot:
        def __init__(self):
            self.calls = 0

        def complete(self, prompt, system=None):
            self.calls += 1
            if self.calls == 1:
                return _FakeResponse(text=json.dumps({
                    "say": "reading",
                    "actions": [{"kind": "read_file", "path": "note.txt"}],
                }))
            return _FakeResponse(text=json.dumps({
                "say": "done",
                "actions": [],
            }))

    session.pilot = FakePilot()
    events = list(session.send("read it"))
    starts = [e for e in events if e.kind == "action_start"]
    assert starts[0].data["id"] == "a1"
