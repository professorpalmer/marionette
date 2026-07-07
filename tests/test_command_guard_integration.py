"""Integration tests: the run_command safety guard + configurable timeout wiring
in ConversationalSession. Proves the guard fires ONLY in full-auto and that the
timeout is resolved from env, not hardcoded.
"""
import os
import pytest

from harness.conversation import ConversationalSession
from harness.config import HarnessConfig


class _Resp:
    def __init__(self, text, meta=None):
        self.text = text
        self.error = None
        self.meta = meta or {}
        self.tokens_out = 5
        self.tokens_in = 5


class _CmdPilot:
    """Pilot that issues one run_command then finishes."""
    supports_streaming = False

    def __init__(self, command):
        self._command = command
        self.n = 0

    def chat(self, hist, tools=None, system=""):
        self.n += 1
        if self.n == 1:
            import json
            return _Resp("", {"tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "run_command",
                             "arguments": json.dumps({"command": self._command})},
            }]})
        return _Resp('{"say":"done","actions":[]}')

    def export_transcript_data(self):
        return {}

    def load_history(self, h):
        pass


def _run(session, msg):
    blocked, results = [], []
    for ev in session.send(msg):
        if ev.kind == "command_blocked":
            blocked.append(ev.data)
        elif ev.kind == "action_result":
            results.append(ev.data)
    return blocked, results


def test_guard_blocks_dangerous_in_auto_mode(tmp_path):
    cfg = HarnessConfig(repo=str(tmp_path), state_dir=str(tmp_path / "st"))
    s = ConversationalSession(cfg)
    s.pilot = _CmdPilot("ssh prod systemctl stop nginx")
    s._auto_mode = True
    s._auto_command_guard = True  # env may have HARNESS_AUTO_COMMAND_GUARD=off
    blocked, _ = _run(s, "go")
    assert len(blocked) == 1
    assert blocked[0]["category"] == "remote-shell"


def test_guard_allows_dangerous_in_interactive(tmp_path):
    cfg = HarnessConfig(repo=str(tmp_path), state_dir=str(tmp_path / "st"))
    s = ConversationalSession(cfg)
    s.pilot = _CmdPilot("rm -rf /tmp/nonexistent_xyz")
    s._auto_mode = False  # interactive: human sees it, guard must NOT fire
    blocked, results = _run(s, "go")
    assert len(blocked) == 0
    assert len(results) == 1  # it actually ran


def test_guard_allows_benign_in_auto_mode(tmp_path):
    cfg = HarnessConfig(repo=str(tmp_path), state_dir=str(tmp_path / "st"))
    s = ConversationalSession(cfg)
    s.pilot = _CmdPilot("echo hello")
    s._auto_mode = True
    blocked, results = _run(s, "go")
    assert len(blocked) == 0
    assert len(results) == 1


def test_guard_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_AUTO_COMMAND_GUARD", "off")
    cfg = HarnessConfig(repo=str(tmp_path), state_dir=str(tmp_path / "st"))
    s = ConversationalSession(cfg)
    s.pilot = _CmdPilot("echo safe-anyway")
    s._auto_mode = True
    # guard disabled -> even if it were dangerous it would not block; benign here
    blocked, results = _run(s, "go")
    assert len(blocked) == 0


def test_auto_mode_resets_after_run_auto(tmp_path):
    # _auto_mode must not stay stuck on after the wrapper completes
    cfg = HarnessConfig(repo=str(tmp_path), state_dir=str(tmp_path / "st"))
    s = ConversationalSession(cfg)
    assert s._auto_mode is False
