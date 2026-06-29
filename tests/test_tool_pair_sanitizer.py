"""Tests for tool_use/tool_result pairing sanitizer (prevents Anthropic 400 on a
dangling tool_use after an interrupted spree) and the configurable/unlimited
pilot step ceiling."""
import os
import tempfile

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession


def _session():
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    return ConversationalSession(cfg)


def test_sanitize_inserts_stub_for_dangling_tool_call():
    s = _session()
    s._history = [
        {"role": "user", "content": "do it"},
        {"role": "assistant", "content": "acting", "tool_calls": [
            {"id": "toolu_A", "type": "function", "function": {"name": "run_command", "arguments": "{}"}},
            {"id": "toolu_B", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "toolu_A", "content": "ok"},
        # toolu_B has NO matching tool result -> dangling (the 400 trigger)
    ]
    s._sanitize_tool_pairs()
    # Every tool_call id now has a matching tool result.
    answered = {m.get("tool_call_id") for m in s._history if m.get("role") == "tool"}
    assert "toolu_A" in answered and "toolu_B" in answered
    # The stub for B is present.
    stubs = [m for m in s._history if m.get("role") == "tool" and m.get("tool_call_id") == "toolu_B"]
    assert len(stubs) == 1 and "interrupted" in stubs[0]["content"]


def test_sanitize_is_idempotent():
    s = _session()
    s._history = [
        {"role": "assistant", "content": "x", "tool_calls": [
            {"id": "toolu_A", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
        ]},
    ]
    s._sanitize_tool_pairs()
    n1 = len(s._history)
    s._sanitize_tool_pairs()
    assert len(s._history) == n1  # no duplicate stub on a second pass


def test_sanitize_noop_when_all_paired():
    s = _session()
    s._history = [
        {"role": "assistant", "content": "x", "tool_calls": [
            {"id": "toolu_A", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "toolu_A", "content": "done"},
    ]
    before = list(s._history)
    s._sanitize_tool_pairs()
    assert s._history == before


def test_max_pilot_steps_env_unlimited(monkeypatch):
    # 0 == unlimited. Verify the env parse the loop relies on.
    monkeypatch.setenv("HARNESS_MAX_PILOT_STEPS", "0")
    val = int(os.environ.get("HARNESS_MAX_PILOT_STEPS", "40"))
    assert val == 0  # loop treats <=0 as unbounded


def test_max_pilot_steps_env_custom(monkeypatch):
    monkeypatch.setenv("HARNESS_MAX_PILOT_STEPS", "100")
    val = int(os.environ.get("HARNESS_MAX_PILOT_STEPS", "40"))
    assert val == 100
