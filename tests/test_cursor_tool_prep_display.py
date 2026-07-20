"""Cursor-native tool_prep must persist into display_transcript by call_id."""

from __future__ import annotations

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession, ConvEvent


def test_persist_cursor_tool_prep_keeps_call_id_slot_before_final_prose():
    cfg = HarnessConfig()
    session = ConversationalSession(cfg)
    session._display_transcript = [
        {"type": "message", "role": "user", "text": "investigate"},
    ]

    session._persist_cursor_tool_prep({
        "name": "Read",
        "goal": "handler.ts",
        "id": "call-disp-1",
        "status": "in_progress",
    })
    assert len(session._display_transcript) == 2
    card = session._display_transcript[1]
    assert card["type"] == "card"
    assert card["id"] == "call-disp-1"
    assert card["call_id"] == "call-disp-1"
    assert card["kind"] == "Read"
    assert card["goal"] == "handler.ts"
    assert card["result"] is None

    # Final prose lands after tools.
    session._display_transcript.append({
        "type": "message", "role": "assistant", "text": "Root cause found.",
    })

    # Late completed status patches in place (does not append after prose).
    session._persist_cursor_tool_prep({
        "name": "Read",
        "goal": "handler.ts",
        "id": "call-disp-1",
        "status": "completed",
    })
    kinds = [r.get("type") for r in session._display_transcript]
    assert kinds == ["message", "card", "message"]
    assert session._display_transcript[1]["result"]["status"] == "complete"
    assert session._display_transcript[1]["call_id"] == "call-disp-1"


def test_persist_cursor_tool_prep_inserts_before_trailing_assistant():
    """Single final assistant: late card slots immediately before it."""
    cfg = HarnessConfig()
    session = ConversationalSession(cfg)
    session._display_transcript = [
        {"type": "message", "role": "user", "text": "go"},
        {"type": "message", "role": "assistant", "text": "Done."},
    ]
    session._persist_cursor_tool_prep({
        "name": "Grep",
        "goal": "TODO",
        "id": "call-late",
        "status": "completed",
    })
    assert [r.get("type") for r in session._display_transcript] == [
        "message", "card", "message",
    ]
    assert session._display_transcript[1]["call_id"] == "call-late"
    assert session._display_transcript[2]["text"] == "Done."


def test_persist_cursor_tool_prep_preserves_pre_tool_narration():
    """Pre-tool narration stays above a late card; only the final prose sits below."""
    cfg = HarnessConfig()
    session = ConversationalSession(cfg)
    session._display_transcript = [
        {"type": "message", "role": "user", "text": "go"},
        {"type": "message", "role": "assistant", "text": "Checking next."},
        {"type": "message", "role": "assistant", "text": "Done."},
    ]
    session._persist_cursor_tool_prep({
        "name": "Read",
        "goal": "handler.ts",
        "id": "call-late-narration",
        "status": "completed",
    })
    rows = session._display_transcript
    assert [r.get("type") for r in rows] == ["message", "message", "card", "message"]
    assert rows[1]["text"] == "Checking next."
    assert rows[2]["call_id"] == "call-late-narration"
    assert rows[3]["text"] == "Done."


def test_persist_cursor_tool_prep_multiple_late_cards_before_final():
    """Multiple late call_ids preserve arrival order before the final assistant."""
    cfg = HarnessConfig()
    session = ConversationalSession(cfg)
    session._display_transcript = [
        {"type": "message", "role": "user", "text": "go"},
        {"type": "message", "role": "assistant", "text": "Checking next."},
        {"type": "message", "role": "assistant", "text": "Done."},
    ]
    session._persist_cursor_tool_prep({
        "name": "Read",
        "goal": "a.ts",
        "id": "call-a",
        "status": "completed",
    })
    session._persist_cursor_tool_prep({
        "name": "Grep",
        "goal": "TODO",
        "id": "call-b",
        "status": "completed",
    })
    rows = session._display_transcript
    assert [r.get("type") for r in rows] == [
        "message", "message", "card", "card", "message",
    ]
    assert rows[1]["text"] == "Checking next."
    assert rows[2]["call_id"] == "call-a"
    assert rows[3]["call_id"] == "call-b"
    assert rows[4]["text"] == "Done."


def test_persist_cursor_tool_prep_skips_anonymous_kind_only():
    cfg = HarnessConfig()
    session = ConversationalSession(cfg)
    session._display_transcript = [
        {"type": "message", "role": "user", "text": "go"},
    ]
    session._persist_cursor_tool_prep({"name": "Read", "status": "in_progress"})
    assert session._display_transcript == [
        {"type": "message", "role": "user", "text": "go"},
    ]


def test_assistant_done_settles_only_current_turn_native_prep(monkeypatch, tmp_path):
    """assistant_done completes this turn's null native cards; older ones stay."""
    sess = ConversationalSession(HarnessConfig(state_dir=str(tmp_path)))
    older = {
        "type": "card",
        "id": "call-older",
        "kind": "Read",
        "goal": "old.ts",
        "call_id": "call-older",
        "result": None,
    }
    sess._display_transcript = [
        {"type": "message", "role": "user", "text": "prior"},
        older,
        {"type": "message", "role": "assistant", "text": "prior done"},
        {"type": "message", "role": "user", "text": "now"},
    ]

    def _fake_send_locked(*_a, **_k):
        yield ConvEvent("tool_prep", {
            "name": "Grep",
            "goal": "TODO",
            "id": "call-now",
            "status": "in_progress",
        })
        yield ConvEvent("assistant_done", {"text": "done"})

    monkeypatch.setattr(sess, "_send_locked", _fake_send_locked)
    list(sess.send("now"))

    by_id = {
        str(c.get("call_id") or ""): c
        for c in sess._display_transcript
        if isinstance(c, dict) and c.get("type") == "card"
    }
    assert by_id["call-now"]["result"] == {"status": "complete"}
    assert by_id["call-older"]["result"] is None


def test_send_cancel_settles_current_turn_native_prep_as_interrupted(monkeypatch, tmp_path):
    """Cancel/error finally settles this turn's still-null native prep cards."""
    sess = ConversationalSession(HarnessConfig(state_dir=str(tmp_path)))
    older = {
        "type": "card",
        "id": "call-bg",
        "kind": "Read",
        "goal": "bg.ts",
        "call_id": "call-bg",
        "result": None,
    }
    sess._display_transcript = [
        {"type": "message", "role": "user", "text": "prior"},
        older,
    ]

    def _fake_send_locked(*_a, **_k):
        yield ConvEvent("tool_prep", {
            "name": "Read",
            "goal": "cur.ts",
            "id": "call-cur",
            "status": "in_progress",
        })
        raise RuntimeError("boom mid-turn")

    monkeypatch.setattr(sess, "_send_locked", _fake_send_locked)
    try:
        list(sess.send("hi"))
    except RuntimeError as exc:
        assert "boom" in str(exc)
    else:
        raise AssertionError("expected RuntimeError from fake send")

    by_id = {
        str(c.get("call_id") or ""): c
        for c in sess._display_transcript
        if isinstance(c, dict) and c.get("type") == "card"
    }
    assert by_id["call-cur"]["result"]["status"] == "interrupted"
    assert by_id["call-cur"]["result"]["error"] == "cancelled"
    assert by_id["call-bg"]["result"] is None


def test_promoted_native_prep_defers_to_action_result(monkeypatch, tmp_path):
    """Prep promoted into action_start is settled by Marionette action lifecycle."""
    sess = ConversationalSession(HarnessConfig(state_dir=str(tmp_path)))

    def _fake_send_locked(*_a, **_k):
        yield ConvEvent("tool_prep", {
            "name": "read_file",
            "goal": "a.py",
            "id": "call-promo",
            "status": "in_progress",
        })
        yield ConvEvent("action_start", {
            "id": "a1",
            "kind": "read_file",
            "goal": "a.py",
            "call_id": "call-promo",
        })
        yield ConvEvent("action_result", {
            "id": "a1",
            "status": "ok",
            "duration_ms": 12,
        })
        yield ConvEvent("assistant_done", {"text": "done"})

    monkeypatch.setattr(sess, "_send_locked", _fake_send_locked)
    list(sess.send("hi"))
    cards = [
        c for c in sess._display_transcript
        if isinstance(c, dict) and c.get("type") == "card"
    ]
    assert len(cards) == 1
    assert cards[0]["id"] == "a1"
    assert cards[0]["call_id"] == "call-promo"
    # Action result wins — not native complete/interrupted settling.
    assert cards[0]["result"]["status"] == "ok"
    assert cards[0]["result"] is not None
    assert cards[0]["result"].get("status") != "complete"
    assert cards[0]["result"].get("status") != "interrupted"


def test_persist_cursor_tool_prep_does_not_rewrite_prior_turn_same_call_id():
    """Current-turn lookup must not mutate an older card that reused call_id."""
    cfg = HarnessConfig()
    session = ConversationalSession(cfg)
    older = {
        "type": "card",
        "id": "call-reuse",
        "kind": "Read",
        "goal": "old.ts",
        "call_id": "call-reuse",
        "result": None,
    }
    session._display_transcript = [
        {"type": "message", "role": "user", "text": "prior"},
        older,
        {"type": "message", "role": "assistant", "text": "prior done"},
        {"type": "message", "role": "user", "text": "now"},
    ]
    created = session._persist_cursor_tool_prep({
        "name": "Grep",
        "goal": "new.ts",
        "id": "call-reuse",
        "status": "in_progress",
    })
    assert created is not older
    assert created["kind"] == "Grep"
    assert created["goal"] == "new.ts"
    assert older["kind"] == "Read"
    assert older["goal"] == "old.ts"
    assert older["result"] is None
    assert session._display_transcript.count(older) == 1
    assert session._display_transcript[-1] is created

