from __future__ import annotations

from pathlib import Path

from harness.browser_real_profile import (
    is_live_browser_user_data_dir,
    real_profile_data_dir,
)


def _linux_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    (tmp_path / ".config" / "google-chrome").mkdir(parents=True)
    return tmp_path


def test_snap_profile_is_live_when_xdg_profile_also_exists(tmp_path, monkeypatch):
    home = _linux_home(tmp_path, monkeypatch)
    snap = home / "snap" / "google-chrome" / "current" / ".config" / "google-chrome"
    snap.mkdir(parents=True)
    assert is_live_browser_user_data_dir(str(snap), system="Linux") is True
    assert is_live_browser_user_data_dir(str(snap / "Default"), system="Linux") is True


def test_flatpak_profile_is_live_when_xdg_profile_also_exists(tmp_path, monkeypatch):
    home = _linux_home(tmp_path, monkeypatch)
    flat = home / ".var" / "app" / "com.google.Chrome" / "config" / "google-chrome"
    flat.mkdir(parents=True)
    assert is_live_browser_user_data_dir(str(flat), system="Linux") is True


def test_xdg_profile_stays_live_and_other_dirs_stay_not_live(tmp_path, monkeypatch):
    home = _linux_home(tmp_path, monkeypatch)
    snap = home / "snap" / "google-chrome" / "current" / ".config" / "google-chrome"
    snap.mkdir(parents=True)
    assert is_live_browser_user_data_dir(
        str(home / ".config" / "google-chrome"), system="Linux"
    ) is True
    scratch = home / "marionette-profile"
    scratch.mkdir()
    assert is_live_browser_user_data_dir(str(scratch), system="Linux") is False
    assert is_live_browser_user_data_dir(str(home / "snap"), system="Linux") is False


def test_snapshot_source_stays_first_existing_candidate(tmp_path, monkeypatch):
    home = _linux_home(tmp_path, monkeypatch)
    snap = home / "snap" / "google-chrome" / "current" / ".config" / "google-chrome"
    snap.mkdir(parents=True)
    assert real_profile_data_dir("chrome", system="Linux") == Path(
        home / ".config" / "google-chrome"
    )
