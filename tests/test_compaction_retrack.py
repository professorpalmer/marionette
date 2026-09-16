from __future__ import annotations

from harness.working_set import merge_working_set_into_history, remember_path, working_set_note


def test_edited_and_mentioned_paths_survive_compact_note():
    edited = set()
    mentioned = set()
    remember_path(edited, "/repo/src/app.py", "/repo")
    remember_path(mentioned, "docs/guide.md", "/repo")
    note = working_set_note(edited, mentioned)
    history = [{"role": "system", "content": "sys"}, {"role": "user", "content": "old"}]
    merge_working_set_into_history(history, note)
    merge_working_set_into_history(history, note)
    assert sum(1 for msg in history if "Working set still tracked" in str(msg.get("content"))) == 1
    assert "edited: src/app.py" in history[1]["content"]
    assert "mentioned: docs/guide.md" in history[1]["content"]


def test_conversation_retrack_after_compact(tmp_path):
    from harness.conversation import ConversationalSession

    session = ConversationalSession.__new__(ConversationalSession)
    session.config = type("C", (), {"repo": "/repo"})()
    session._edited_paths = {"src/app.py"}
    session._mentioned_paths = {"docs/guide.md"}
    session._history = [{"role": "system", "content": "sys"}, {"role": "user", "content": "old"}]
    session._retrack_after_compact()
    assert "edited: src/app.py" in session._history[1]["content"]
    assert "mentioned: docs/guide.md" in session._history[1]["content"]
