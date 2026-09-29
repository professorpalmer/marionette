"""A mid-turn transcript checkpoint must not hold the global swap lock.

Checkpoints run every 2s on long turns. Writing (export, JSON, disk, search
index) under _pilot_swap_lock stalled session switches and config reads that
take the same lock. Ownership is still resolved under the lock; the write
and index skip happen outside it. The end-of-turn save keeps both.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

import harness.server as srv
from harness.session_runners import SessionRunnerRegistry
from harness.sessions import SessionStore, load_transcript


@pytest.fixture
def turn(tmp_path, monkeypatch):
    store = SessionStore(str(tmp_path / "harness_sessions.json"))
    sid = store.create(title="A")["id"]
    marker = {"history": [{"role": "assistant", "content": "from-A"}], "display": []}
    pilot = SimpleNamespace(harness_session_id=sid, export_transcript_data=lambda: marker)
    runners = SessionRunnerRegistry()
    runners.get_or_create(sid, lambda: pilot)
    monkeypatch.setattr(srv, "_sessions", store)
    monkeypatch.setattr(srv, "_runners", runners)
    monkeypatch.setattr(srv, "_pilot", pilot)
    monkeypatch.setattr(srv._cfg, "state_dir", str(tmp_path))
    indexed = []
    monkeypatch.setattr("harness.session_fts.index_session_transcript", lambda d, s, m: indexed.append(s))
    return {"ctx": {"session_id": sid, "pilot": pilot}, "sid": sid, "marker": marker, "state_dir": str(tmp_path), "indexed": indexed}


def test_checkpoint_writes_outside_the_swap_lock_and_skips_indexing(turn, monkeypatch):
    import harness.sessions as sessions

    in_write = threading.Event()
    release = threading.Event()
    real_write = sessions._write_transcript

    def slow_write(*args, **kwargs):
        in_write.set()
        assert release.wait(5)
        return real_write(*args, **kwargs)

    monkeypatch.setattr(sessions, "_write_transcript", slow_write)
    worker = threading.Thread(target=srv._checkpoint_transcript, args=(turn["ctx"],))
    worker.start()
    try:
        assert in_write.wait(5)
        got = srv._pilot_swap_lock.acquire(timeout=1)
        assert got, "checkpoint write held the global swap lock"
        srv._pilot_swap_lock.release()
    finally:
        release.set()
        worker.join(5)
    assert load_transcript(turn["state_dir"], turn["sid"]) == turn["marker"]
    assert turn["indexed"] == []


def test_end_of_turn_save_still_indexes(turn):
    srv._persist_turn_transcript(turn["ctx"])
    assert load_transcript(turn["state_dir"], turn["sid"]) == turn["marker"]
    assert turn["indexed"] == [turn["sid"]]
