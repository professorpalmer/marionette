"""Debounced SessionStore._save coalescing + pytest-sync flush."""
from __future__ import annotations

import json
import time
from unittest import mock

import harness.sessions as sessions
from harness.sessions import SessionStore


def test_save_flushes_synchronously_under_pytest(tmp_path):
    path = tmp_path / "harness_sessions.json"
    store = SessionStore(str(path))
    store.create(title="A", repo=str(tmp_path / "repo"))
    assert path.is_file()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert len(data.get("sessions") or []) == 1


def test_debounced_save_coalesces_mutations(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(sessions, "_SAVE_DEBOUNCE_S", 0.08)
    path = tmp_path / "harness_sessions.json"
    store = SessionStore(str(path))

    replaces = {"n": 0}
    real_replace = sessions.os.replace

    def counting_replace(src, dst, *args, **kwargs):
        result = real_replace(src, dst, *args, **kwargs)
        replaces["n"] += 1
        return result

    monkeypatch.setattr(sessions.os, "replace", counting_replace)

    store.create(title="One", repo=str(tmp_path / "r1"))
    store.rename(store.active or "", "Two")
    store.archive(store.active or "", True)
    mid = replaces["n"]
    assert mid == 0
    assert not path.exists() or replaces["n"] == 0

    deadline = time.time() + 1.0
    while replaces["n"] < 1 and time.time() < deadline:
        time.sleep(0.02)
    assert replaces["n"] == 1
    data = json.loads(path.read_text(encoding="utf-8"))
    assert len(data["sessions"]) == 1
    assert data["sessions"][0]["title"] == "Two"
    assert data["sessions"][0]["archived"] is True


def test_delete_flushes_immediately_outside_pytest(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(sessions, "_SAVE_DEBOUNCE_S", 5.0)
    path = tmp_path / "harness_sessions.json"
    store = SessionStore(str(path))
    row = store.create(title="Keep", repo=str(tmp_path / "repo"))
    # create is debounced when PYTEST_CURRENT_TEST is cleared
    store.flush()
    assert path.is_file()

    replaces = {"n": 0}
    real_replace = sessions.os.replace

    def counting_replace(src, dst, *args, **kwargs):
        result = real_replace(src, dst, *args, **kwargs)
        replaces["n"] += 1
        return result

    monkeypatch.setattr(sessions.os, "replace", counting_replace)
    store.delete(row["id"])
    assert replaces["n"] == 1
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data.get("sessions") == []


def test_flush_writes_pending_dirty_state(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(sessions, "_SAVE_DEBOUNCE_S", 5.0)
    path = tmp_path / "harness_sessions.json"
    store = SessionStore(str(path))
    with mock.patch.object(sessions.os, "replace", wraps=sessions.os.replace) as wrapped:
        store.create(title="Pending", repo=str(tmp_path / "repo"))
        assert wrapped.call_count == 0
        store.flush()
        assert wrapped.call_count == 1
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["sessions"][0]["title"] == "Pending"
