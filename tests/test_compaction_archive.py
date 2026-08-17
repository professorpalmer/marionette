"""Session-scoped compaction archive sidecar."""
from __future__ import annotations

import json

from harness.api.sessions import remove_session_transcript
from harness.compaction_archive import (
    ARCHIVE_LOAD_MAX_BYTES,
    ARCHIVE_MAX_MESSAGES,
    ARCHIVE_MAX_SERIALIZED_BYTES,
    ARCHIVE_TRUNCATION_FLAG,
    ARCHIVE_TRUNCATION_PREFIX,
    append_compaction_archive,
    compaction_archive_path,
    load_compaction_archive_messages,
    remove_compaction_archive,
    retain_archive_messages,
)
from harness.sessions import save_transcript


def test_append_survives_residual_transcript_persist(tmp_path):
    state_dir = str(tmp_path)
    sid = "sess_archive"
    assert append_compaction_archive(
        state_dir,
        sid,
        [{"role": "user", "content": "elided-middle"}],
    )
    save_transcript(
        state_dir,
        sid,
        {
            "history": [{"role": "user", "content": "residual-tail"}],
            "display": [],
            "job_ids": [],
        },
    )
    rows = load_compaction_archive_messages(state_dir, sid)
    assert [m.get("content") for m in rows] == ["elided-middle"]


def test_append_does_not_replace_prior_elided_rows(tmp_path):
    state_dir = str(tmp_path)
    sid = "sess_archive_gen"
    append_compaction_archive(state_dir, sid, [{"role": "user", "content": "gen-1"}])
    append_compaction_archive(state_dir, sid, [{"role": "user", "content": "gen-2"}])
    rows = load_compaction_archive_messages(state_dir, sid)
    assert [m.get("content") for m in rows] == ["gen-1", "gen-2"]


def test_corrupt_archive_fails_closed(tmp_path):
    state_dir = str(tmp_path)
    sid = "sess_bad"
    path = compaction_archive_path(state_dir, sid)
    from pathlib import Path

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("{nope", encoding="utf-8")
    assert load_compaction_archive_messages(state_dir, sid) == []
    # A later append replaces the unreadable sidecar with valid rows.
    assert append_compaction_archive(state_dir, sid, [{"role": "user", "content": "recovered"}])
    assert [m.get("content") for m in load_compaction_archive_messages(state_dir, sid)] == [
        "recovered"
    ]


def test_session_isolation_and_traversal(tmp_path):
    state_dir = str(tmp_path)
    append_compaction_archive(state_dir, "alpha", [{"role": "user", "content": "secret-a"}])
    append_compaction_archive(state_dir, "beta", [{"role": "user", "content": "secret-b"}])
    assert [m.get("content") for m in load_compaction_archive_messages(state_dir, "alpha")] == [
        "secret-a"
    ]
    assert [m.get("content") for m in load_compaction_archive_messages(state_dir, "beta")] == [
        "secret-b"
    ]
    # Same sanitizer as save_transcript: "../alpha" collapses to "alpha".
    assert [m.get("content") for m in load_compaction_archive_messages(state_dir, "../alpha")] == [
        "secret-a"
    ]
    assert append_compaction_archive(state_dir, "../etc/passwd", [{"role": "user", "content": "x"}])
    from pathlib import Path

    trans = Path(state_dir) / "transcripts"
    written = list(trans.glob("*.archive.json"))
    assert written
    for path in written:
        assert path.parent == trans
        assert ".." not in path.name
    assert not (Path(state_dir) / "etc").exists()


def test_remove_session_transcript_clears_archive(tmp_path):
    state_dir = str(tmp_path)
    sid = "sess_clear"
    append_compaction_archive(state_dir, sid, [{"role": "user", "content": "gone"}])
    save_transcript(state_dir, sid, {"history": [], "display": [], "job_ids": []})
    archive = compaction_archive_path(state_dir, sid)
    from pathlib import Path

    assert Path(archive).is_file()
    remove_session_transcript(sid, state_dir=state_dir)
    assert not Path(archive).exists()
    assert load_compaction_archive_messages(state_dir, sid) == []
    remove_compaction_archive(state_dir, sid)  # idempotent


def test_small_session_is_not_truncated(tmp_path):
    state_dir = str(tmp_path)
    sid = "sess_small"
    rows = [{"role": "user", "content": f"keep-{i}"} for i in range(6)]
    assert append_compaction_archive(state_dir, sid, rows)
    loaded = load_compaction_archive_messages(state_dir, sid)
    assert [m.get("content") for m in loaded] == [f"keep-{i}" for i in range(6)]
    assert not any(m.get(ARCHIVE_TRUNCATION_FLAG) for m in loaded)


def test_message_cap_keeps_oldest_and_newest_with_marker(tmp_path, monkeypatch):
    import harness.compaction_archive as archive_mod

    monkeypatch.setattr(archive_mod, "ARCHIVE_MAX_MESSAGES", 8)
    monkeypatch.setattr(archive_mod, "ARCHIVE_MAX_SERIALIZED_BYTES", 64 * 1024)
    monkeypatch.setattr(archive_mod, "ARCHIVE_LOAD_MAX_BYTES", 128 * 1024)
    state_dir = str(tmp_path)
    sid = "sess_msg_cap"
    incoming = [{"role": "user", "content": f"row-{i}"} for i in range(20)]
    assert append_compaction_archive(state_dir, sid, incoming)
    loaded = load_compaction_archive_messages(state_dir, sid)
    assert len(loaded) <= 8
    assert any(m.get(ARCHIVE_TRUNCATION_FLAG) for m in loaded)
    assert any(
        isinstance(m.get("content"), str) and m["content"].startswith(ARCHIVE_TRUNCATION_PREFIX)
        for m in loaded
    )
    contents = [m.get("content") for m in loaded if not m.get(ARCHIVE_TRUNCATION_FLAG)]
    assert contents[0] == "row-0"
    assert contents[-1] == "row-19"
    assert "row-10" not in contents


def test_byte_cap_bounds_sidecar_and_signals_truncation(tmp_path, monkeypatch):
    import harness.compaction_archive as archive_mod

    monkeypatch.setattr(archive_mod, "ARCHIVE_MAX_MESSAGES", 400)
    monkeypatch.setattr(archive_mod, "ARCHIVE_MAX_SERIALIZED_BYTES", 800)
    monkeypatch.setattr(archive_mod, "ARCHIVE_LOAD_MAX_BYTES", 8 * 1024)
    state_dir = str(tmp_path)
    sid = "sess_byte_cap"
    incoming = [{"role": "user", "content": f"blob-{i}-" + ("x" * 120)} for i in range(20)]
    assert append_compaction_archive(state_dir, sid, incoming)
    loaded = load_compaction_archive_messages(state_dir, sid)
    assert any(m.get(ARCHIVE_TRUNCATION_FLAG) for m in loaded)
    serialized = json.dumps(loaded, default=str).encode("utf-8")
    assert len(serialized) <= 800
    assert len(loaded) < 20
    from pathlib import Path

    path = Path(compaction_archive_path(state_dir, sid))
    assert path.stat().st_size <= 8 * 1024
    contents = [m.get("content") for m in loaded if not m.get(ARCHIVE_TRUNCATION_FLAG)]
    assert contents[0].startswith("blob-0-")
    assert contents[-1].startswith("blob-19-")


def test_oversized_sidecar_fails_closed_on_load(tmp_path, monkeypatch):
    import harness.compaction_archive as archive_mod

    monkeypatch.setattr(archive_mod, "ARCHIVE_LOAD_MAX_BYTES", 300)
    state_dir = str(tmp_path)
    sid = "sess_huge"
    path = compaction_archive_path(state_dir, sid)
    from pathlib import Path

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(
        '{"version": 1, "session_id": "sess_huge", "messages": ['
        + f'{{"role":"user","content":"{"pad" * 200}"}}'
        + "]}",
        encoding="utf-8",
    )
    assert Path(path).stat().st_size > 300
    assert load_compaction_archive_messages(state_dir, sid) == []
    # A later append replaces the oversized sidecar with a bounded document.
    assert append_compaction_archive(state_dir, sid, [{"role": "user", "content": "recovered"}])
    assert [m.get("content") for m in load_compaction_archive_messages(state_dir, sid)] == [
        "recovered"
    ]


def test_real_caps_bound_repeated_appends(tmp_path):
    state_dir = str(tmp_path)
    sid = "sess_real_cap"
    for wave in range(0, ARCHIVE_MAX_MESSAGES + 40, 40):
        batch = [{"role": "user", "content": f"w{wave}-{i}"} for i in range(40)]
        assert append_compaction_archive(state_dir, sid, batch)
    loaded = load_compaction_archive_messages(state_dir, sid)
    assert len(loaded) <= ARCHIVE_MAX_MESSAGES
    assert any(m.get(ARCHIVE_TRUNCATION_FLAG) for m in loaded)
    contents = [m.get("content") for m in loaded if not m.get(ARCHIVE_TRUNCATION_FLAG)]
    assert contents[0] == "w0-0"
    assert contents[-1].startswith("w")


def test_retain_helper_is_noop_under_caps():
    rows = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
    assert retain_archive_messages(rows) == rows
    assert ARCHIVE_MAX_MESSAGES >= 8
    assert ARCHIVE_MAX_SERIALIZED_BYTES >= 1024
    assert ARCHIVE_LOAD_MAX_BYTES >= ARCHIVE_MAX_SERIALIZED_BYTES
