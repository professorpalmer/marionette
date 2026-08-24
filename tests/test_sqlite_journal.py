"""NFS-safe SQLite journal helper."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from harness.sqlite_journal import (
    configure_sqlite_connection,
    host_scoped_state_path,
    is_network_filesystem,
    journal_mode_for,
)


def _network_on(path: str) -> bool:
    return True


def _network_off(path: str) -> bool:
    return False


def test_journal_mode_wal_on_local_fs(tmp_path):
    db = tmp_path / "local.sqlite"
    assert journal_mode_for(db, statfs=_network_off) == "wal"


def test_journal_mode_truncate_on_network_fs(tmp_path):
    db = tmp_path / "remote.sqlite"
    assert journal_mode_for(db, statfs=_network_on) == "truncate"


def test_configure_sqlite_connection_applies_mode(tmp_path):
    db = tmp_path / "mode.sqlite"
    conn = sqlite3.connect(str(db))
    try:
        mode = configure_sqlite_connection(conn, db, statfs=_network_on)
        assert mode == "truncate"
        row = conn.execute("PRAGMA journal_mode").fetchone()
        assert row is not None
        assert row[0].lower() == "truncate"
    finally:
        conn.close()


def test_configure_sqlite_connection_busy_timeout(tmp_path):
    db = tmp_path / "busy.sqlite"
    conn = sqlite3.connect(str(db))
    try:
        configure_sqlite_connection(conn, db, busy_timeout_ms=1234, statfs=_network_off)
        row = conn.execute("PRAGMA busy_timeout").fetchone()
        assert row == (1234,)
    finally:
        conn.close()


def test_is_network_filesystem_uses_injected_statfs(tmp_path):
    path = tmp_path / "probe"
    path.mkdir()
    assert is_network_filesystem(path, statfs=_network_on) is True
    assert is_network_filesystem(path, statfs=_network_off) is False


def test_host_scoped_state_path_unchanged_on_local_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    base = Path.home() / ".pmharness" / "state" / "schedules.sqlite"
    assert host_scoped_state_path(base, statfs=_network_off) == base


def test_host_scoped_state_path_adds_host_dir_on_network_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    base = Path.home() / ".pmharness" / "state" / "schedules.sqlite"
    scoped = host_scoped_state_path(base, statfs=_network_on)
    assert scoped.parent.name  # hostname token directory
    assert scoped.name == "schedules.sqlite"
    assert scoped.parts[-3:-1] == ("hosts", scoped.parts[-2])


def test_host_scoped_state_path_non_pmharness_path_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    other = Path.home() / "Documents" / "data.sqlite"
    assert host_scoped_state_path(other, statfs=_network_on) == other


def test_schedule_store_uses_journal_helper(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    db = tmp_path / "schedules.sqlite"
    from harness.schedule_store import ScheduleStore

    store = ScheduleStore(str(db))
    try:
        row = store._conn.execute("PRAGMA journal_mode").fetchone()
        assert row is not None
        assert row[0].lower() in {"wal", "truncate"}
    finally:
        store.close()


@pytest.mark.parametrize(
    "path_suffix",
    [
        "state/schedules.sqlite",
        "memory_graph.sqlite",
    ],
)
def test_host_scoped_paths_under_state_and_root(path_suffix, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    base = Path.home() / ".pmharness" / path_suffix
    scoped = host_scoped_state_path(base, statfs=_network_on)
    assert "hosts" in scoped.parts
    assert scoped.name == Path(path_suffix).name
