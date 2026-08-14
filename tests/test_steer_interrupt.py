"""Regression: a mid-run user steer must INTERRUPT the action spree, not be
ignored until the model finishes. Bug: steer was passively appended to the last
user message and the loop ran its full course (the "I said stop and it ran 7 more
steps" report).
"""
import json
import threading
import time

from harness.conversation import ConversationalSession
from harness.config import HarnessConfig


class _Resp:
    def __init__(self, text, meta=None):
        self.text = text; self.error = None; self.meta = meta or {}
        self.tokens_out = 5; self.tokens_in = 5


class _SpreePilot:
    """Issues a multi-action spree. On its FIRST call it enqueues a steer (so the
    enqueue is deterministic, not timing-dependent), then keeps issuing actions
    until it sees the steer in history, then stops."""
    supports_streaming = False

    def __init__(self, session):
        self.saw_steer = False
        self.session = session
        self.calls = 0

    def chat(self, hist, tools=None, system=""):
        self.calls += 1
        if self.calls == 1:
            # user steers mid-run, right after the first spree turn is issued
            self.session.enqueue_steer("hello stop")
        for m in hist:
            content = str(m.get("content", ""))
            # First-class steer is a plain user message; legacy OUT-OF-BAND
            # markers in older history remain recognized for compatibility.
            if m.get("role") == "user" and (
                "hello stop" in content or "OUT-OF-BAND" in content
            ):
                self.saw_steer = True
        if self.saw_steer:
            return _Resp('{"say":"Stopping.","actions":[]}')
        return _Resp("", {"tool_calls": [
            {"id": "x", "type": "function",
             "function": {"name": "list_dir", "arguments": json.dumps({"path": "."})}},
            {"id": "y", "type": "function",
             "function": {"name": "list_dir", "arguments": json.dumps({"path": "."})}},
        ]})

    def export_transcript_data(self):
        return {}

    def load_history(self, h):
        pass


def test_steer_interrupts_spree(tmp_path):
    cfg = HarnessConfig(repo=str(tmp_path), state_dir=str(tmp_path / "st"))
    p = ConversationalSession(cfg)
    p.pilot = _SpreePilot(p)

    kinds = []
    for ev in p.send("do a big task"):
        kinds.append(ev.kind)
        if len([k for k in kinds if k == "action_result"]) > 30:
            break

    assert "steer" in kinds, "the steer must surface as an event"
    assert p.pilot.saw_steer, "the model must receive the steer in history"
    assert kinds[-1] == "assistant_done", "the run must end cleanly after the steer"
    assert len([k for k in kinds if k == "action_result"]) < 20
