"""Tests for adaptive step limit in ConversationalSession (no network)."""
import os
import json
import tempfile
import shutil
import pytest

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession, ConvEvent


class FakePilot:
    name = "fake"
    def __init__(self, turns_json_list):
        self.turns = turns_json_list
        self.current_turn = 0

    def complete(self, prompt, *, system=None):
        from pmharness.drivers.openai_compat import DriverResponse
        if self.current_turn < len(self.turns):
            txt = self.turns[self.current_turn]
            self.current_turn += 1
        else:
            txt = '{"say": "Done.", "actions": []}'
        return DriverResponse(text=txt, tokens_out=10, latency_ms=1.0)


def test_adaptive_steps_productive_runs_past_10():
    temp_dir = tempfile.mkdtemp()
    try:
        cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=temp_dir)
        cfg.repo = temp_dir
        
        session = ConversationalSession(cfg)
        
        # We will provide 12 productive turns (each has an action)
        turns = []
        for i in range(12):
            turns.append(json.dumps({
                "say": f"Step {i+1}",
                "actions": [{
                    "kind": "run_command",
                    "command": f"echo {i+1}"
                }]
            }))
        # The 13th turn has no actions (ends the loop)
        turns.append(json.dumps({
            "say": "All done!",
            "actions": []
        }))
        
        session.pilot = FakePilot(turns)
        
        events = list(session.send("Let's go!"))
        
        # Check that we received assistant_done with turns >= 13
        done_event = next(e for e in events if e.kind == "assistant_done")
        assert done_event.data["turns"] == 13
    finally:
        shutil.rmtree(temp_dir)


def test_adaptive_steps_stalls_halts_at_threshold(monkeypatch):
    from harness.pilot import PilotTurn
    monkeypatch.setattr(PilotTurn, "has_actions", True)

    temp_dir = tempfile.mkdtemp()
    try:
        cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=temp_dir)
        cfg.repo = temp_dir
        
        session = ConversationalSession(cfg)
        
        # We will provide 5 non-productive turns (empty say and no actions)
        turns = []
        for i in range(5):
            turns.append(json.dumps({
                "say": "",
                "actions": []
            }))
            
        session.pilot = FakePilot(turns)
        
        events = list(session.send("Let's go!"))
        
        # The loop should halt at consecutive_non_productive >= 3, which is exactly step 3 (0-indexed 0, 1, 2)
        # It will yield "Reached the investigation step limit for this message." and assistant_done with turns=3.
        messages = [e.data.get("text") for e in events if e.kind == "message" and e.data.get("role") == "assistant"]
        assert any("Reached the investigation step limit for this message" in m for m in messages if m)
        
        done_event = next(e for e in events if e.kind == "assistant_done")
        assert done_event.data["turns"] == 3
    finally:
        shutil.rmtree(temp_dir)
