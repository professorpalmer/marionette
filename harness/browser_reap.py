"""Reap Chrome/Chromium Marionette launched, never the user's live profile.

Owned Chrome is started in its own process group so it can be killpg'd from
CDP. Electron's backend-tree SIGTERM therefore misses it on quit/update, and
the leftover window holds Cookies open for the next boot.
"""
from __future__ import annotations

import ntpath
import os
import posixpath
import re
import signal
import subprocess
import sys
import time
from typing import Dict, List, Optional, Tuple

_BROWSER_HINTS = ("chrome", "chromium", "msedge", "brave", "google chrome")
_TEMP_PROFILE_PREFIX = "pm-cdp-"


def own_profile_roots(home: Optional[str] = None, platform: Optional[str] = None) -> List[str]:
    """Profile roots under this instance's HOME.

    Exact paths, not substrings: a sibling install (another HOME) has its own
    ``.pmharness/browser-profile`` and its Chrome is not ours to kill.
    """
    base = home if home is not None else os.path.expanduser("~")
    join = ntpath.join if (platform or sys.platform) == "win32" else posixpath.join
    return [
        join(base, ".pmharness", "browser-profile"),
        join(base, ".pmharness", "browser-profile-real"),
        join(base, ".puppetmaster", "browser-profile"),
        join(base, ".puppetmaster", "browser-profiles"),
    ]


def _normalize_dir(path: str, platform: str) -> str:
    out = (path or "").strip().strip("\"'")
    if platform == "win32":
        out = out.replace("\\", "/")
    out = out.rstrip("/")
    # Windows and default macOS volumes ignore case.
    return out.lower() if platform in ("win32", "darwin") else out


def user_data_dir(cmdline: str) -> str:
    """Value of --user-data-dir. It can hold spaces, so it ends at the next flag."""
    raw = cmdline or ""
    flag = "--user-data-dir="
    start = raw.find(flag)
    if start < 0:
        return ""
    rest = raw[start + len(flag):]
    match = re.search(r"\s--[a-zA-Z]", rest)
    return (rest[: match.start()] if match else rest).strip()


def profile_ownership(
    cmdline: str, home: Optional[str] = None, platform: Optional[str] = None
) -> Optional[str]:
    """``"home"`` for this HOME's profiles, ``"temp"`` for a pm-cdp-* profile.

    Every install shares the pm-cdp-* name, so the caller reaps a temp profile
    only when the owner chain allows (see ``_temp_profile_reapable``).
    """
    low = (cmdline or "").lower()
    if not any(name in low for name in _BROWSER_HINTS):
        return None
    plat = platform or sys.platform
    found = _normalize_dir(user_data_dir(cmdline), plat)
    if not found:
        return None
    for root in own_profile_roots(home, plat):
        norm = _normalize_dir(root, plat)
        if found == norm or found.startswith(norm + "/"):
            return "home"
    if found.rsplit("/", 1)[-1].startswith(_TEMP_PROFILE_PREFIX):
        return "temp"
    return None


def cmdline_is_marionette_browser(
    cmdline: str, home: Optional[str] = None, platform: Optional[str] = None
) -> bool:
    """True when argv is a browser on this HOME's profile, not the user's Chrome."""
    return profile_ownership(cmdline, home, platform) == "home"


def _temp_profile_reapable(pid: int, parents: Dict[int, int], self_pid: int) -> bool:
    """Ours when this process is an ancestor; orphaned when the parent is gone.

    A live foreign parent means another Marionette instance still drives it.
    """
    parent = parents.get(pid, 0)
    if parent <= 1 or parent not in parents:
        return True
    seen = set()
    cur = parent
    while cur > 1 and cur not in seen:
        if cur == self_pid:
            return True
        seen.add(cur)
        cur = parents.get(cur, 0)
    return False


def _list_processes() -> List[Tuple[int, int, str]]:
    rows: List[Tuple[int, int, str]] = []
    if os.name == "nt":
        try:
            out = subprocess.check_output(
                [
                    "wmic",
                    "process",
                    "get",
                    "ProcessId,ParentProcessId,CommandLine",
                    "/FORMAT:LIST",
                ],
                stderr=subprocess.DEVNULL,
                timeout=8,
            )
        except Exception:
            return rows
        return parse_wmic_list(out.decode("utf-8", "replace"))
    try:
        out = subprocess.check_output(
            ["ps", "-axww", "-o", "pid=,ppid=,args="],
            stderr=subprocess.DEVNULL,
            timeout=8,
        )
    except Exception:
        return rows
    return parse_posix_ps(out.decode("utf-8", "replace"))


def parse_posix_ps(text: str) -> List[Tuple[int, int, str]]:
    rows: List[Tuple[int, int, str]] = []
    for line in text.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) != 3:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        if pid > 1:
            rows.append((pid, ppid, parts[2]))
    return rows


def parse_wmic_list(text: str) -> List[Tuple[int, int, str]]:
    rows: List[Tuple[int, int, str]] = []
    pid = ppid = 0
    cmd = ""
    for line in text.splitlines() + [""]:
        raw = line.strip()
        if not raw:
            if pid > 1:
                rows.append((pid, ppid, cmd))
            pid = ppid = 0
            cmd = ""
            continue
        key, _, value = raw.partition("=")
        key = key.lower()
        if key == "commandline":
            cmd = value
        elif key in ("processid", "parentprocessid"):
            try:
                num = int(value.strip())
            except ValueError:
                num = 0
            if key == "processid":
                pid = num
            else:
                ppid = num
    return rows


def reap_targets(
    rows: List[Tuple[int, int, str]],
    home: Optional[str] = None,
    platform: Optional[str] = None,
    self_pid: Optional[int] = None,
) -> List[int]:
    parents = {pid: ppid for pid, ppid, _ in rows}
    me = os.getpid() if self_pid is None else self_pid
    targets: List[int] = []
    for pid, _, cmd in rows:
        kind = profile_ownership(cmd, home, platform)
        if kind == "home" or (kind == "temp" and _temp_profile_reapable(pid, parents, me)):
            targets.append(pid)
    return targets


def _signal_pid(pid: int, sig: int) -> bool:
    if pid <= 1 or pid == os.getpid() or pid == os.getppid():
        return False
    try:
        os.kill(pid, sig)
        return True
    except OSError:
        return False


def reap_marionette_browsers(timeout_s: float = 2.0) -> List[int]:
    """SIGTERM then SIGKILL matching browsers. Returns signaled pids."""
    targets = reap_targets(_list_processes())
    signaled: List[int] = []
    for pid in targets:
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    check=False,
                    timeout=5,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                signaled.append(pid)
            except Exception:
                continue
            continue
        if _signal_pid(pid, signal.SIGTERM):
            signaled.append(pid)
    if os.name != "nt" and signaled and timeout_s > 0:
        deadline = time.monotonic() + float(timeout_s)
        while time.monotonic() < deadline:
            still = []
            for pid in signaled:
                try:
                    os.kill(pid, 0)
                    still.append(pid)
                except OSError:
                    continue
            if not still:
                break
            time.sleep(0.05)
        for pid in signaled:
            _signal_pid(pid, signal.SIGKILL)
    return signaled


def shutdown_owned_browsers() -> List[int]:
    """Drop the in-process CDP session, then reap leftover owned Chrome."""
    try:
        from puppetmaster import browser_cdp as engine
        reset = getattr(engine, "reset_session", None)
        if callable(reset):
            reset(keep_profile=True)
    except Exception:
        pass
    return reap_marionette_browsers()
