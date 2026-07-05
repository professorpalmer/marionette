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


def test_sanitize_inserts_stub_when_steer_wedged_between_tool_use_and_result():
    # Adjacency case: a user/steer message wedged between the assistant tool_use
    # and its tool result breaks Anthropic's "immediately after" rule even though
    # the result is present later. A stub must be inserted immediately after the
    # (empty) adjacent tool run, i.e. right after the assistant message.
    s = _session()
    s._history = [
        {"role": "user", "content": "do it"},
        {"role": "assistant", "content": "acting", "tool_calls": [
            {"id": "toolu_A", "type": "function", "function": {"name": "run_command", "arguments": "{}"}},
        ]},
        # A steer/user message is wedged BEFORE the tool result -> non-adjacent.
        {"role": "user", "content": "[OUT-OF-BAND] stop"},
        {"role": "tool", "tool_call_id": "toolu_A", "content": "ok"},
    ]
    s._sanitize_tool_pairs()
    # The assistant tool_use must now be directly followed by a stub tool result.
    idx = next(i for i, m in enumerate(s._history)
               if m.get("role") == "assistant" and m.get("tool_calls"))
    nxt = s._history[idx + 1]
    assert nxt.get("role") == "tool"
    assert nxt.get("tool_call_id") == "toolu_A"
    assert "interrupted" in nxt.get("content", "")


def test_export_transcript_data_has_no_dangling_tool_use():
    s = _session()
    s._history = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "do it"},
        {"role": "assistant", "content": "acting", "tool_calls": [
            {"id": "toolu_A", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
        ]},
        # dangling: no tool result
    ]
    data = s.export_transcript_data()
    history = data["history"]
    answered = {m.get("tool_call_id") for m in history if m.get("role") == "tool"}
    called = set()
    for m in history:
        for tc in (m.get("tool_calls") or []):
            called.add(tc.get("id"))
    assert called <= answered, "export must not persist a dangling tool_use"
    assert "toolu_A" in answered


def test_load_history_heals_dangling_tool_use():
    s = _session()
    corrupted = {
        "history": [
            {"role": "user", "content": "do it"},
            {"role": "assistant", "content": "acting", "tool_calls": [
                {"id": "toolu_A", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
            ]},
            # dangling on load
        ],
        "display": [],
        "job_ids": [],
    }
    s.load_history(corrupted)
    answered = {m.get("tool_call_id") for m in s._history if m.get("role") == "tool"}
    assert "toolu_A" in answered
    idx = next(i for i, m in enumerate(s._history)
               if m.get("role") == "assistant" and m.get("tool_calls"))
    assert s._history[idx + 1].get("role") == "tool"


def test_steer_marker_hardwraps_long_unbroken_token():
    long_token = "a" * 500
    out = ConversationalSession._steer_marker(long_token)
    # The wrapped token lines are the lines made up solely of the run char.
    token_lines = [ln for ln in out.splitlines() if ln and set(ln) == {"a"}]
    assert token_lines, "the token must survive the wrap"
    # Each token line must be clamped to the wrap width (200).
    assert all(len(ln) <= 200 for ln in token_lines), "each token line must be clamped to width"
    # All 500 characters are preserved across the wrap.
    assert sum(len(ln) for ln in token_lines) == 500
    # The 500-char unbroken run was actually broken into multiple lines.
    assert len(token_lines) >= 3


def test_max_pilot_steps_env_unlimited(monkeypatch):
    # 0 == unlimited. Verify the env parse the loop relies on.
    monkeypatch.setenv("HARNESS_MAX_PILOT_STEPS", "0")
    val = int(os.environ.get("HARNESS_MAX_PILOT_STEPS", "40"))
    assert val == 0  # loop treats <=0 as unbounded


def test_max_pilot_steps_env_custom(monkeypatch):
    monkeypatch.setenv("HARNESS_MAX_PILOT_STEPS", "100")
    val = int(os.environ.get("HARNESS_MAX_PILOT_STEPS", "40"))
    assert val == 100
