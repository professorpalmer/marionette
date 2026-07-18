"""Cheap session-list preview (OMP-style listing without full hydrate)."""
from __future__ import annotations

import json
import os

import harness.sessions as sessions
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


def test_list_preview_cache_skips_reread(tmp_path, monkeypatch):
    store = SessionStore(str(tmp_path / "harness_sessions.json"))
    row = store.create(title="T", repo=str(tmp_path / "repo"))
    sid = row["id"]
    save_transcript(
        str(tmp_path),
        sid,
        {
            "history": [{"role": "user", "content": "cached preview line"}],
            "display": [],
            "job_ids": [],
        },
    )
    with sessions._preview_cache_lock:
        sessions._preview_cache.clear()

    opens = {"n": 0}
    real_open = open

    def counting_open(file, *args, **kwargs):
        path = file if isinstance(file, str) else str(file)
        if "transcripts" in path.replace("\\", "/") and path.endswith(".json"):
            opens["n"] += 1
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", counting_open)
    listed = store.list(state_dir=str(tmp_path))
    assert next(r for r in listed if r["id"] == sid).get("preview") == "cached preview line"
    after_first = opens["n"]
    assert after_first >= 1
    listed2 = store.list(state_dir=str(tmp_path))
    assert next(r for r in listed2 if r["id"] == sid).get("preview") == "cached preview line"
    assert opens["n"] == after_first

    save_transcript(
        str(tmp_path),
        sid,
        {
            "history": [{"role": "user", "content": "after rewrite"}],
            "display": [],
            "job_ids": [],
        },
    )
    listed3 = store.list(state_dir=str(tmp_path))
    assert next(r for r in listed3 if r["id"] == sid).get("preview") == "after rewrite"
    assert opens["n"] > after_first
