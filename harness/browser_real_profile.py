"""Consent-gated snapshot of the user's real Chromium profile auth files.

When ``HARNESS_BROWSER_REAL_PROFILE`` is on, copy last-used Chrome / Chromium
/ Edge / Brave auth files into a Marionette-owned directory and launch against
that copy — never the live user-data-dir (SingletonLock / Chrome >= 136).
Default off, including on desktop, until the user consents. Under pytest/CI
the flag stays off unless the env is explicitly truthy.

Never logs or returns cookies or passwords. Never kills the user's browser.
A locked cookie DB (Windows PermissionError) fails closed so we do not launch
a silently signed-out copy.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import sqlite3
from pathlib import Path
from typing import Optional, Tuple
from urllib.request import pathname2url

REAL_PROFILE_ENV = "HARNESS_BROWSER_REAL_PROFILE"
_TRUTHY = ("1", "true", "yes", "on")
_SNAPSHOT_DONE_MARKER = ".marionette-snapshot-complete"
_COPY_ROOT_NAME = "browser-profile-real"

# Profile-dir names skipped on the first copytree. Auth SQLite files are
# excluded here and copied via online-backup so a live Chrome lock cannot
# abort the tree copy or leave a torn DB.
_COPYTREE_IGNORES = (
    "Cache",
    "Code Cache",
    "GPUCache",
    "Service Worker",
    "Crashpad",
    "Cookies",
    "Login Data",
    "Login Data For Account",
    "Web Data",
    "*-journal",
    "*-wal",
    "*-shm",
    "SingletonLock",
    "SingletonSocket",
    "SingletonCookie",
)

_AUTH_DB_RELS = (
    "Cookies",
    "Network/Cookies",
    "Login Data",
    "Login Data For Account",
    "Web Data",
)
_AUTH_PLAIN_RELS = ("Preferences",)

_BROWSER_PATHS = {
    "chrome": (
        ("Google", "Chrome"),
        ("Google", "Chrome", "User Data"),
        "google-chrome",
    ),
    "edge": (
        ("Microsoft Edge",),
        ("Microsoft", "Edge", "User Data"),
        "microsoft-edge",
    ),
    "brave": (
        ("BraveSoftware", "Brave-Browser"),
        ("BraveSoftware", "Brave-Browser", "User Data"),
        "BraveSoftware/Brave-Browser",
    ),
    "chromium": (
        ("Chromium",),
        ("Chromium", "User Data"),
        "chromium",
    ),
}

_LINUX_SNAP_PARTS = {
    "chrome": ("snap", "google-chrome", "current", ".config", "google-chrome"),
    "chromium": ("snap", "chromium", "common", "chromium"),
    "brave": ("snap", "brave", "current", ".config", "BraveSoftware", "Brave-Browser"),
}

_LINUX_FLATPAK_IDS = {
    "chrome": "com.google.Chrome",
    "chromium": "org.chromium.Chromium",
    "brave": "com.brave.Browser",
    "edge": "com.microsoft.Edge",
}


def is_live_browser_user_data_dir(path: str, system: Optional[str] = None) -> bool:
    """True when ``path`` is (or is inside) a real installed-browser profile."""
    raw = (path or "").strip()
    if not raw:
        return False
    try:
        resolved = Path(raw).expanduser().resolve()
    except (OSError, RuntimeError):
        return False
    hosts = (system,) if system else ("Darwin", "Windows", "Linux")
    for host in hosts:
        for browser in _BROWSER_PATHS:
            live = real_profile_data_dir(browser, system=host)
            try:
                live_resolved = Path(live).resolve()
            except (OSError, RuntimeError):
                continue
            if resolved == live_resolved or live_resolved in resolved.parents:
                return True
    return False


def real_profile_enabled() -> bool:
    """Return whether the user consented to a real-profile snapshot.

    Off unless ``HARNESS_BROWSER_REAL_PROFILE`` is 1/true/yes/on. Unlike
    browser auth, desktop does not default this on — consent is required.
    """
    raw = (os.environ.get(REAL_PROFILE_ENV) or "").strip().lower()
    return raw in _TRUTHY


def real_profile_data_dir(browser: str = "chrome", system: Optional[str] = None) -> Path:
    """Return the OS default user-data-dir for ``browser``."""
    key = browser if browser in _BROWSER_PATHS else "chrome"
    mac_parts, win_parts, linux_name = _BROWSER_PATHS[key]
    host = system or platform.system()
    home = Path.home()
    if host == "Darwin":
        return home / "Library" / "Application Support" / Path(*mac_parts)
    if host == "Windows":
        local = os.environ.get("LOCALAPPDATA") or str(home / "AppData" / "Local")
        return Path(local).joinpath(*win_parts)
    config = os.environ.get("XDG_CONFIG_HOME") or str(home / ".config")
    candidates = [Path(config).joinpath(*linux_name.split("/"))]
    snap = _LINUX_SNAP_PARTS.get(key)
    if snap:
        candidates.append(home.joinpath(*snap))
    flatpak_id = _LINUX_FLATPAK_IDS.get(key)
    if flatpak_id:
        candidates.append(
            home / ".var" / "app" / flatpak_id / "config" / Path(*linux_name.split("/"))
        )
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[0]


def real_profile_copy_dir(browser: str = "chrome") -> Path:
    """Return the Marionette-owned snapshot dir for ``browser``."""
    return Path.home() / ".pmharness" / _COPY_ROOT_NAME / browser


def _last_used_profile(src: Path) -> str:
    """Return ``Local State`` ``profile.last_used``, or Default."""
    try:
        with open(src / "Local State", encoding="utf-8", errors="replace") as fh:
            state = json.load(fh)
        last = ((state.get("profile") or {}).get("last_used")) or "Default"
    except (OSError, ValueError, TypeError, AttributeError):
        last = "Default"
    if not isinstance(last, str) or not (src / last).is_dir():
        return "Default"
    return last


def _copy_auth_db(src_file: Path, dst_file: Path) -> bool:
    """Copy one SQLite auth DB via URI readonly + ``Connection.backup``."""
    if not src_file.is_file():
        return False
    dst_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        if dst_file.exists():
            dst_file.unlink()
    except OSError:
        pass
    uri = "file:%s?mode=ro" % pathname2url(os.path.abspath(str(src_file)))
    try:
        source = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            dest = sqlite3.connect(str(dst_file))
            try:
                source.backup(dest)
            finally:
                dest.close()
        finally:
            source.close()
        return True
    except Exception:
        return False


def _cookie_db_path(src: Path, source_profile: str) -> Optional[Path]:
    for rel in ("Network/Cookies", "Cookies"):
        cand = src / source_profile / rel
        if cand.is_file():
            return cand
    return None


def _lock_error(src: Path, source_profile: str) -> Optional[str]:
    db = _cookie_db_path(src, source_profile)
    if db is None:
        return None
    try:
        with open(db, "rb"):
            return None
    except PermissionError:
        return (
            "profile locked: cookie database is in use. Fully quit Chrome "
            "(including any background/tray instance) and retry. Closing "
            "Chrome may be required on Windows if the copy fails."
        )
    except OSError:
        return None


def _chmod_owner_only(path: Path) -> None:
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def snapshot_real_profile(
    browser: str = "chrome",
    src: Optional[Path] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """Copy last-used profile auth into the Marionette-owned copy dir.

    Returns ``(copy_dir, None)`` on success or ``(None, error)`` on failure.
    The error for a locked cookie DB always starts with ``profile locked:``.
    """
    if not real_profile_enabled():
        return None, (
            "real Chrome profile copy requires explicit consent "
            "(HARNESS_BROWSER_REAL_PROFILE=1)"
        )
    if src is None:
        src_path = real_profile_data_dir(browser)
    else:
        src_path = Path(src)
    if not src_path.is_dir():
        return None, (
            "profile directory for %r was not found (%s). Launch that browser "
            "at least once, or turn off Use my Chrome login."
            % (browser, src_path)
        )
    last_used = _last_used_profile(src_path)
    locked = _lock_error(src_path, last_used)
    if locked:
        return None, locked

    copy_dir = real_profile_copy_dir(browser)
    try:
        copy_dir.mkdir(parents=True, exist_ok=True)
        parent = copy_dir.parent
        _chmod_owner_only(parent)
        _chmod_owner_only(copy_dir)

        local_state = src_path / "Local State"
        if local_state.is_file():
            shutil.copy2(str(local_state), str(copy_dir / "Local State"))

        marker = copy_dir / _SNAPSHOT_DONE_MARKER
        if not marker.is_file():
            src_profile = src_path / last_used
            dst_default = copy_dir / "Default"
            if not src_profile.is_dir():
                return None, (
                    "profile directory for %r was not found (%s). Launch that "
                    "browser at least once, or turn off Use my Chrome login."
                    % (browser, src_profile)
                )
            shutil.rmtree(str(dst_default), ignore_errors=True)
            try:
                shutil.copytree(
                    str(src_profile),
                    str(dst_default),
                    symlinks=False,
                    ignore=shutil.ignore_patterns(*_COPYTREE_IGNORES),
                    ignore_dangling_symlinks=True,
                    dirs_exist_ok=True,
                )
            except shutil.Error:
                pass

        dst_default = copy_dir / "Default"
        for rel in _AUTH_DB_RELS:
            src_file = src_path / last_used / rel
            if not src_file.is_file():
                continue
            if not _copy_auth_db(src_file, dst_default / rel):
                return None, (
                    "could not copy login data from the %r profile. Fully quit "
                    "the browser and retry, or turn off Use my Chrome login."
                    % browser
                )
        for rel in _AUTH_PLAIN_RELS:
            src_file = src_path / last_used / rel
            if not src_file.is_file():
                continue
            dest = dst_default / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(str(src_file), str(dest))
            except OSError:
                return None, (
                    "could not copy login data from the %r profile. Fully quit "
                    "the browser and retry, or turn off Use my Chrome login."
                    % browser
                )

        for leftover in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            try:
                (copy_dir / leftover).unlink()
            except OSError:
                pass

        marker.write_text(last_used, encoding="utf-8")
    except OSError as e:
        return None, "could not snapshot the %r profile into %s: %s" % (
            browser,
            copy_dir,
            e,
        )
    return str(copy_dir), None


def cleanup_real_profile_snapshots() -> None:
    """Delete the real-profile copy tree. Idempotent; ignore_errors."""
    root = Path.home() / ".pmharness" / _COPY_ROOT_NAME
    shutil.rmtree(str(root), ignore_errors=True)
    current = (os.environ.get("PM_BROWSER_USER_DATA_DIR") or "").strip()
    if not current:
        return
    try:
        cur = Path(current).resolve()
        root_resolved = root.resolve()
        if root_resolved == cur or root_resolved in cur.parents:
            os.environ.pop("PM_BROWSER_USER_DATA_DIR", None)
    except (OSError, ValueError):
        pass
