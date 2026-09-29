"""An unchanged transcript save must not rewrite the file or rebuild its index.

Every session switch saves the outgoing session; for an idle long session
that was a full JSON write plus a full FTS re-index each time.
"""
from __future__ import annotations

import os

import pytest

import harness.sessions as sessions


@pytest.fixture
def counts(tmp_path, monkeypatch):
    calls = {"write": 0, "index": 0}
    real_write = sessions._write_transcript

    def write(*a, **k):
        calls["write"] += 1
        return real_write(*a, **k)

    monkeypatch.setattr(sessions, "_write_transcript", write)
    monkeypatch.setattr("harness.session_fts.index_session_transcript", lambda *a, **k: calls.__setitem__("index", calls["index"] + 1))
    monkeypatch.setattr(sessions, "_LAST_SAVED", {})
    (tmp_path / "transcripts").mkdir()
    return calls


def _hist(n):
    return {"history": [{"role": "user", "content": f"m{i}"} for i in range(n)], "display": []}


def test_repeat_save_of_same_content_is_skipped(tmp_path, counts):
    for _ in range(5):
        sessions.save_transcript(str(tmp_path), "s1", _hist(50))
    assert counts == {"write": 1, "index": 1}
    sessions.save_transcript(str(tmp_path), "s1", _hist(51))
    assert counts == {"write": 2, "index": 2}


def test_file_changed_by_another_writer_is_rewritten(tmp_path, counts):
    sessions.save_transcript(str(tmp_path), "s1", _hist(5))
    path = tmp_path / "transcripts" / "s1.json"
    path.write_text("[]", encoding="utf-8")
    os.utime(path, ns=(1, 1))
    sessions.save_transcript(str(tmp_path), "s1", _hist(5))
    assert counts["write"] == 2
    assert sessions.load_transcript(str(tmp_path), "s1") == _hist(5)


def test_checkpoint_then_unchanged_final_save_indexes_once_without_rewriting(tmp_path, counts):
    sessions.save_transcript(str(tmp_path), "s1", _hist(5), index=False)
    sessions.save_transcript(str(tmp_path), "s1", _hist(5))
    sessions.save_transcript(str(tmp_path), "s1", _hist(5))
    assert counts == {"write": 1, "index": 1}
