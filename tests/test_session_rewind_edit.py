"""Hermes-style message-edit rewind: truncate + stash + restore."""
from __future__ import annotations

from harness.conversation import ConversationalSession


def _sess(tmp_path):
    s = ConversationalSession.__new__(ConversationalSession)
    # Minimal init for rewind helpers (avoid full pilot/MCP wiring).
    s._history = [{"role": "system", "content": "sys"}]
    s._display_transcript = []
    s._session_job_ids = []
    s._rewind_stash = None
    s.state_dir = str(tmp_path)
    import threading
    s._busy = threading.Lock()
    return s


def test_rewind_to_user_ordinal_truncates_and_prefills(tmp_path):
    s = _sess(tmp_path)
    s._display_transcript = [
        {"type": "message", "role": "user", "text": "one"},
        {"type": "message", "role": "assistant", "text": "a1"},
        {"type": "message", "role": "user", "text": "two"},
        {"type": "message", "role": "assistant", "text": "a2"},
    ]
    s._history = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "two"},
        {"role": "assistant", "content": "a2"},
    ]

    res = s.rewind_to_user_ordinal(1)
    assert res["ok"] is True
    assert res["prefill"] == "two"
    assert len(s._display_transcript) == 2
    assert s._display_transcript[-1]["text"] == "a1"
    assert [m["role"] for m in s._history[1:]] == ["user", "assistant"]
    assert s._rewind_stash is not None


def test_restore_rewind_stash(tmp_path):
    s = _sess(tmp_path)
    s._display_transcript = [
        {"type": "message", "role": "user", "text": "one"},
        {"type": "message", "role": "assistant", "text": "a1"},
        {"type": "message", "role": "user", "text": "two"},
    ]
    s._history = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "two"},
    ]
    assert s.rewind_to_user_ordinal(1)["ok"]
    assert len(s._display_transcript) == 2
    restored = s.restore_rewind_stash()
    assert restored["ok"] is True
    assert len(s._display_transcript) == 3
    assert s._display_transcript[-1]["text"] == "two"
    assert s._rewind_stash is None


def test_rewind_blocked_while_busy(tmp_path):
    s = _sess(tmp_path)
    s._display_transcript = [{"type": "message", "role": "user", "text": "x"}]
    s._history = [{"role": "system", "content": "sys"}, {"role": "user", "content": "x"}]
    assert s._busy.acquire(blocking=False)
    try:
        res = s.rewind_to_user_ordinal(0)
        assert res["ok"] is False
        assert res.get("code") == "busy"
    finally:
        s._busy.release()
