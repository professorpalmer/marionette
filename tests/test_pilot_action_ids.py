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


def test_reused_provider_call_ids_get_distinct_card_ids_across_turns(tmp_path):
    """Local OpenAI-compatible servers often restart call ids at call_0 each
    turn. The card id is UI identity, so it must be unique per session, while
    call_id stays the provider's (tool_prep promotion and history pairing)."""
    real_tmp = os.path.realpath(str(tmp_path))
    with open(os.path.join(real_tmp, "note.txt"), "w", encoding="utf-8") as f:
        f.write("hi")
    session = ConversationalSession(HarnessConfig(repo=real_tmp, swarm_adapter="demo"))

    class FakePilot:
        def __init__(self):
            self.calls = 0

        def complete(self, prompt, system=None):
            self.calls += 1
            if self.calls % 2 == 1:
                return _FakeResponse(text=json.dumps({
                    "say": "reading",
                    "actions": [{"kind": "read_file", "path": "note.txt", "tool_call_id": "call_0"}],
                }))
            return _FakeResponse(text=json.dumps({"say": "done", "actions": []}))

    session.pilot = FakePilot()
    first = [e.data for e in session.send("one") if e.kind == "action_start"]
    second = [e.data for e in session.send("two") if e.kind == "action_start"]
    assert first[0]["id"] == "call_0"
    assert second[0]["id"] != "call_0"
    assert first[0]["call_id"] == second[0]["call_id"] == "call_0"
    cards = [d for d in session.export_display_transcript() if d.get("type") == "card"]
    assert len({c["id"] for c in cards}) == len(cards) == 2
