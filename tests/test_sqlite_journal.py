"""NFS-safe SQLite journal helper."""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

from harness.sqlite_journal import (
    _DARWIN_NETWORK_FSTYPES,
    _LINUX_NETWORK_FSTYPES,
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


def test_darwin_network_fstypes_exclude_local_apfs():
    """f_type 26 is APFS (local). 316 treated it as NETFS and forced TRUNCATE."""
    assert 26 not in _DARWIN_NETWORK_FSTYPES
    assert _DARWIN_NETWORK_FSTYPES == frozenset({6, 13, 28})


def _fstype_from_proc_mounts(path: str):
    target = os.path.realpath(path)
    best_len = -1
    best = None
    with open("/proc/mounts", encoding="utf-8") as mounts:
        for raw_line in mounts:
            parts = raw_line.split()
            if len(parts) < 3:
                continue
            mount_point = parts[1].replace("\\040", " ")
            mount_point = mount_point.rstrip("/") or "/"
            if target == mount_point or (
                mount_point != "/" and target.startswith(mount_point + os.sep)
            ):
                if len(mount_point) > best_len:
                    best_len = len(mount_point)
                    best = parts[2]
    return best


@pytest.mark.skipif(sys.platform != "darwin", reason="real Darwin APFS/HFS detector")
def test_darwin_local_disk_is_not_network_uses_wal(tmp_path):
    """Real detector (no injected statfs): local APFS/HFS is not network."""
    assert is_network_filesystem(tmp_path) is False
    db = tmp_path / "local.sqlite"
    conn = sqlite3.connect(str(db))
    try:
        mode = configure_sqlite_connection(conn, db)
        assert mode == "wal"
        row = conn.execute("PRAGMA journal_mode").fetchone()
        assert row is not None
        assert row[0].lower() == "wal"
    finally:
        conn.close()


@pytest.mark.skipif(sys.platform != "linux", reason="real Linux /proc/mounts detector")
def test_linux_real_proc_mounts_cwd_and_tmp(tmp_path):
    """Real detector agrees with /proc/mounts for cwd and a temp dir."""
    for probe in (os.getcwd(), str(tmp_path)):
        fstype = _fstype_from_proc_mounts(probe)
        expected_network = False
        if fstype is not None:
            base = fstype.split(".", 1)[0].lower()
            expected_network = (
                base in _LINUX_NETWORK_FSTYPES
                or fstype.lower() in _LINUX_NETWORK_FSTYPES
            )
        assert is_network_filesystem(probe) is expected_network
        mode = journal_mode_for(Path(probe) / "probe.sqlite")
        assert mode == ("truncate" if expected_network else "wal")
    if not is_network_filesystem(tmp_path):
        db = tmp_path / "local.sqlite"
        conn = sqlite3.connect(str(db))
        try:
            mode = configure_sqlite_connection(conn, db)
            assert mode == "wal"
            row = conn.execute("PRAGMA journal_mode").fetchone()
            assert row is not None
            assert row[0].lower() == "wal"
        finally:
            conn.close()


@pytest.mark.skipif(sys.platform != "win32", reason="real Win32 drive-type detector")
def test_win32_local_temp_is_not_network(tmp_path):
    """Real detector: a local temp dir is not DRIVE_REMOTE / UNC."""
    assert is_network_filesystem(tmp_path) is False
    db = tmp_path / "local.sqlite"
    conn = sqlite3.connect(str(db))
    try:
        mode = configure_sqlite_connection(conn, db)
        assert mode == "wal"
        row = conn.execute("PRAGMA journal_mode").fetchone()
        assert row is not None
        assert row[0].lower() == "wal"
    finally:
        conn.close()
