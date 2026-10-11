from __future__ import annotations

from harness.working_set import merge_working_set_into_history, remember_path, working_set_note

_RESIDUAL = {"role": "user", "content": "[Earlier conversation summarized to fit context]\nbody", "_compressed_summary": True}


def test_edited_and_mentioned_paths_go_into_the_residual():
    edited = set()
    mentioned = set()
    remember_path(edited, "/repo/src/app.py", "/repo")
    remember_path(mentioned, "docs/guide.md", "/repo")
    note = working_set_note(edited, mentioned)
    history = [{"role": "system", "content": "sys"}, dict(_RESIDUAL), {"role": "assistant", "content": "ok"}]
    merge_working_set_into_history(history, note)
    merge_working_set_into_history(history, note)
    assert len(history) == 3
    assert history[1]["content"].count("Files in this task") == 1
    assert "edited: src/app.py" in history[1]["content"]
    assert "mentioned: docs/guide.md" in history[1]["content"]


def test_legacy_standalone_note_is_removed():
    legacy = {"role": "user", "content": "Working set still tracked after compact:\n- edited: old.py"}
    history = [{"role": "system", "content": "sys"}, legacy, dict(_RESIDUAL)]
    merge_working_set_into_history(history, working_set_note({"src/app.py"}, set()))
    assert [m.get("_compressed_summary") for m in history[1:]] == [True]
    assert "edited: src/app.py" in history[1]["content"]


def test_conversation_retrack_after_compact(tmp_path):
    from harness.conversation import ConversationalSession

    session = ConversationalSession.__new__(ConversationalSession)
    session.config = type("C", (), {"repo": "/repo"})()
    session._edited_paths = {"src/app.py"}
    session._mentioned_paths = {"docs/guide.md"}
    session._history = [{"role": "system", "content": "sys"}, dict(_RESIDUAL)]
    session._retrack_after_compact()
    assert "edited: src/app.py" in session._history[1]["content"]
    assert "mentioned: docs/guide.md" in session._history[1]["content"]
