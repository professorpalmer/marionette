"""Consent-gated real Chromium profile snapshot: copy, lock, wipe, env wire."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import harness.browser_auth as auth
import harness.browser_real_profile as rp


def _write_cookie_db(path: Path, marker: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE cookies (host TEXT, name TEXT, value TEXT)")
        conn.execute(
            "INSERT INTO cookies VALUES ('example.com', 'sid', ?)", (marker,)
        )
        conn.commit()
    finally:
        conn.close()


def _cookie_marker(path: Path) -> str:
    conn = sqlite3.connect(str(path))
    try:
        row = conn.execute("SELECT value FROM cookies WHERE name = 'sid'").fetchone()
        return row[0] if row else ""
    finally:
        conn.close()


def _make_src(root: Path, last_used: str = "Default", marker: str = "v1") -> Path:
    profile = root / last_used
    (profile / "Network").mkdir(parents=True)
    (profile / "Cache" / "Cache_Data").mkdir(parents=True)
    (profile / "Cache" / "Cache_Data" / "big").write_text("x" * 100)
    (profile / "Preferences").write_text("{}", encoding="utf-8")
    _write_cookie_db(profile / "Cookies", marker)
    _write_cookie_db(profile / "Network" / "Cookies", marker + "-net")
    (root / "Local State").write_text(
        json.dumps({"profile": {"last_used": last_used}}),
        encoding="utf-8",
    )
    return root


def test_real_profile_default_off_under_pytest(monkeypatch):
    monkeypatch.delenv("HARNESS_BROWSER_REAL_PROFILE", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/test_browser_real_profile.py")
    assert rp.real_profile_enabled() is False


def test_real_profile_enabled_only_when_env_truthy(monkeypatch):
    monkeypatch.setenv("HARNESS_BROWSER_REAL_PROFILE", "1")
    assert rp.real_profile_enabled() is True
    monkeypatch.setenv("HARNESS_BROWSER_REAL_PROFILE", "0")
    assert rp.real_profile_enabled() is False


def test_snapshot_copies_auth_skips_cache_and_last_used(tmp_path, monkeypatch):
    monkeypatch.setattr(rp.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    src = _make_src(tmp_path / "real", last_used="Profile 1", marker="from-p1")
    (src / "Default").mkdir()
    _write_cookie_db(src / "Default" / "Cookies", "from-default")

    copy_dir, err = rp.snapshot_real_profile("chrome", src=src)
    assert err is None
    assert copy_dir == str(tmp_path / "home" / ".pmharness" / "browser-profile-real" / "chrome")
    dest = Path(copy_dir)
    assert dest.joinpath("Local State").is_file()
    assert dest.joinpath(".marionette-snapshot-complete").read_text(encoding="utf-8") == "Profile 1"
    assert _cookie_marker(dest / "Default" / "Cookies") == "from-p1"
    assert (dest / "Default" / "Preferences").is_file()
    assert not (dest / "Default" / "Cache").exists()
    assert not (dest / "SingletonLock").exists()


def test_snapshot_missing_src_names_path(tmp_path, monkeypatch):
    monkeypatch.setattr(rp.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    missing = tmp_path / "nope"
    copy_dir, err = rp.snapshot_real_profile("chrome", src=missing)
    assert copy_dir is None
    assert err is not None
    assert str(missing) in err


def test_snapshot_lock_error(tmp_path, monkeypatch):
    monkeypatch.setattr(rp.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    src = _make_src(tmp_path / "real")
    real_open = open

    def locked_open(path, mode="r", *args, **kwargs):
        text = str(path)
        if "Cookies" in text and "rb" in str(mode):
            raise PermissionError("locked")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", locked_open)
    copy_dir, err = rp.snapshot_real_profile("chrome", src=src)
    assert copy_dir is None
    assert err is not None
    assert err.startswith("profile locked:")


def test_cleanup_wipes_copy_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(rp.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    src = _make_src(tmp_path / "real")
    copy_dir, err = rp.snapshot_real_profile("chrome", src=src)
    assert err is None and Path(copy_dir).is_dir()
    monkeypatch.setenv("PM_BROWSER_USER_DATA_DIR", copy_dir)
    rp.cleanup_real_profile_snapshots()
    assert not Path(copy_dir).exists()
    assert (os.environ.get("PM_BROWSER_USER_DATA_DIR") or "") == ""
    rp.cleanup_real_profile_snapshots()


def test_ensure_shared_browser_env_points_at_copy_when_enabled(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(rp.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(auth.Path, "home", classmethod(lambda cls: home))
    src = _make_src(tmp_path / "real")
    monkeypatch.setattr(rp, "real_profile_data_dir", lambda browser="chrome", system=None: src)
    monkeypatch.setenv("HARNESS_BROWSER_AUTH", "1")
    monkeypatch.setenv("HARNESS_BROWSER_REAL_PROFILE", "1")
    monkeypatch.delenv("PM_BROWSER_USER_DATA_DIR", raising=False)
    monkeypatch.delenv("PM_BROWSER_CDP_PORT", raising=False)
    monkeypatch.delenv("PM_BROWSER_HEADED", raising=False)
    applied = auth.ensure_shared_browser_env()
    expected = str(home / ".pmharness" / "browser-profile-real" / "chrome")
    assert applied["user_data_dir"] == expected
    assert Path(expected, "Default", "Cookies").is_file()


def test_ensure_shared_browser_env_keeps_preset_user_data_dir(tmp_path, monkeypatch):
    preset = tmp_path / "preset-profile"
    preset.mkdir()
    monkeypatch.setenv("HARNESS_BROWSER_AUTH", "1")
    monkeypatch.setenv("HARNESS_BROWSER_REAL_PROFILE", "1")
    monkeypatch.setenv("PM_BROWSER_USER_DATA_DIR", str(preset))
    monkeypatch.delenv("PM_BROWSER_CDP_PORT", raising=False)
    applied = auth.ensure_shared_browser_env()
    assert applied["user_data_dir"] == str(preset)


def test_ensure_shared_browser_env_falls_back_when_snapshot_fails(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(rp.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(auth.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(
        rp,
        "real_profile_data_dir",
        lambda browser="chrome", system=None: tmp_path / "missing-chrome",
    )
    monkeypatch.setenv("HARNESS_BROWSER_AUTH", "1")
    monkeypatch.setenv("HARNESS_BROWSER_REAL_PROFILE", "1")
    monkeypatch.delenv("PM_BROWSER_USER_DATA_DIR", raising=False)
    monkeypatch.delenv("PM_BROWSER_CDP_PORT", raising=False)
    applied = auth.ensure_shared_browser_env()
    assert applied["user_data_dir"] == str(home / ".pmharness" / "browser-profile")


def test_real_profile_data_dir_darwin(monkeypatch):
    monkeypatch.setattr(rp.Path, "home", classmethod(lambda cls: Path("/Users/t")))
    got = rp.real_profile_data_dir("chrome", system="Darwin")
    assert got == Path("/Users/t/Library/Application Support/Google/Chrome")


def test_real_profile_data_dir_windows(monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\T\AppData\Local")
    got = rp.real_profile_data_dir("chrome", system="Windows")
    assert got == Path(r"C:\Users\T\AppData\Local") / "Google" / "Chrome" / "User Data"


def test_real_profile_data_dir_linux_first_existing(tmp_path, monkeypatch):
    monkeypatch.setattr(rp.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    snap = tmp_path / "snap" / "google-chrome" / "current" / ".config" / "google-chrome"
    snap.mkdir(parents=True)
    got = rp.real_profile_data_dir("chrome", system="Linux")
    assert got == snap
