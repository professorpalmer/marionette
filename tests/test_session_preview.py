"""Cheap session-list preview (OMP-style listing without full hydrate)."""
from __future__ import annotations

import json
import os

from harness.sessions import (
    SessionStore,
    attach_session_previews,
    save_transcript,
    transcript_preview,
)


def test_transcript_preview_first_user_turn(tmp_path):
    state = str(tmp_path)
    sid = "abc123def456"
    save_transcript(
        state,
        sid,
        {
            "history": [
                {"role": "user", "content": "hello from preview wave"},
                {"role": "assistant", "content": "ok"},
            ],
            "display": [],
            "job_ids": [],
        },
    )
    assert transcript_preview(state, sid) == "hello from preview wave"
    assert transcript_preview(state, sid, max_chars=5) == "hello"


def test_transcript_preview_large_file_prefix_scan(tmp_path):
    state = str(tmp_path)
    sid = "bigfile000001"
    trans_dir = tmp_path / "transcripts"
    trans_dir.mkdir()
    # Build a large JSON prefix so size > _PREVIEW_READ_BYTES, then a user turn.
    pad = "x" * 9000
    payload = {
        "history": [
            {"role": "assistant", "content": pad},
            {"role": "user", "content": "needle in large transcript"},
        ],
        "display": [],
        "job_ids": [],
    }
    # Ensure file is large; if user turn falls outside first 8k, prefix scan may
    # miss — place user first for the cheap-listing contract.
    payload["history"] = [
        {"role": "user", "content": "needle in large transcript"},
        {"role": "assistant", "content": pad},
    ]
    (trans_dir / f"{sid}.json").write_text(json.dumps(payload), encoding="utf-8")
    assert os.path.getsize(trans_dir / f"{sid}.json") > 8000
    assert "needle" in transcript_preview(state, sid)


def test_list_includes_preview_not_history(tmp_path):
    store = SessionStore(str(tmp_path / "harness_sessions.json"))
    row = store.create(title="T", repo=str(tmp_path / "repo"))
    sid = row["id"]
    save_transcript(
        str(tmp_path),
        sid,
        {
            "history": [{"role": "user", "content": "list me softly"}],
            "display": [],
            "job_ids": [],
        },
    )
    listed = store.list(state_dir=str(tmp_path))
    hit = next(r for r in listed if r["id"] == sid)
    assert hit.get("preview") == "list me softly"
    assert "history" not in hit


def test_attach_session_previews_empty_ok(tmp_path):
    rows = [{"id": "missing000001", "title": "x"}]
    attach_session_previews(rows, str(tmp_path))
    assert rows[0]["preview"] == ""
