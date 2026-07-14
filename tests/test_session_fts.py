"""Hermetic tests for FTS5 session transcript recall."""
from __future__ import annotations

import json
import os
import tempfile

from harness.session_fts import (
    index_session_transcript,
    reindex_transcripts,
    remove_session_from_index,
    search_sessions,
)
from harness.sessions import save_transcript


def _synthetic_transcript(user_text: str, assistant_text: str = "") -> dict:
    history = [{"role": "user", "content": user_text}]
    display = [{"type": "message", "role": "user", "text": user_text}]
    if assistant_text:
        history.append({"role": "assistant", "content": assistant_text})
        display.append({"type": "message", "role": "assistant", "text": assistant_text})
    return {"history": history, "display": display, "job_ids": []}


def test_index_and_search_hits():
    with tempfile.TemporaryDirectory() as state_dir:
        assert index_session_transcript(
            state_dir,
            "sess-alpha",
            _synthetic_transcript(
                "how do I configure FTS5 session recall",
                "Use sqlite virtual tables over transcripts",
            ),
        )
        hits = search_sessions(state_dir, "FTS5 recall", limit=10)
        assert hits
        assert hits[0]["session_id"] == "sess-alpha"
        assert "snippet" in hits[0]
        assert "rank" in hits[0]
        assert isinstance(hits[0]["rank"], float)


def test_empty_query_returns_empty():
    with tempfile.TemporaryDirectory() as state_dir:
        index_session_transcript(
            state_dir,
            "sess-beta",
            _synthetic_transcript("something searchable about widgets"),
        )
        assert search_sessions(state_dir, "") == []
        assert search_sessions(state_dir, "   ") == []


def test_no_match_returns_empty():
    with tempfile.TemporaryDirectory() as state_dir:
        index_session_transcript(
            state_dir,
            "sess-gamma",
            _synthetic_transcript("talk about databases and indexing"),
        )
        assert search_sessions(state_dir, "xylophone-zzztop") == []


def test_reindex_skips_corrupt_transcript_files():
    with tempfile.TemporaryDirectory() as state_dir:
        trans_dir = os.path.join(state_dir, "transcripts")
        os.makedirs(trans_dir)
        good_path = os.path.join(trans_dir, "good-sess.json")
        with open(good_path, "w", encoding="utf-8") as f:
            json.dump(
                _synthetic_transcript("unique pineapple search token"),
                f,
            )
        bad_path = os.path.join(trans_dir, "bad-sess.json")
        with open(bad_path, "w", encoding="utf-8") as f:
            f.write("{not valid json!!!")

        stats = reindex_transcripts(state_dir)
        assert stats["indexed"] == 1
        assert stats["skipped"] >= 1
        hits = search_sessions(state_dir, "pineapple")
        assert len(hits) == 1
        assert hits[0]["session_id"] == "good-sess"


def test_save_transcript_hooks_fts_index():
    with tempfile.TemporaryDirectory() as state_dir:
        save_transcript(
            state_dir,
            "hook-sess",
            _synthetic_transcript("durable state mango indexing"),
        )
        hits = search_sessions(state_dir, "mango")
        assert len(hits) == 1
        assert hits[0]["session_id"] == "hook-sess"


def test_remove_session_from_index():
    with tempfile.TemporaryDirectory() as state_dir:
        index_session_transcript(
            state_dir,
            "gone-sess",
            _synthetic_transcript("temporary blueberry content"),
        )
        assert search_sessions(state_dir, "blueberry")
        assert remove_session_from_index(state_dir, "gone-sess")
        assert search_sessions(state_dir, "blueberry") == []


def test_index_rejects_unsafe_session_id():
    with tempfile.TemporaryDirectory() as state_dir:
        assert index_session_transcript(
            state_dir,
            "../evil",
            _synthetic_transcript("should not index"),
        ) is False
        assert search_sessions(state_dir, "should") == []
