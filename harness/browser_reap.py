"""Reap Chrome/Chromium Marionette launched, never the user's live profile.

Owned Chrome is started in its own process group so it can be killpg'd from
CDP. Electron's backend-tree SIGTERM therefore misses it on quit/update, and
the leftover window holds Cookies open for the next boot.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from typing import List, Tuple

_PROFILE_MARKERS = (
    "/.pmharness/browser-profile",
    "\\.pmharness\\browser-profile",
    "/.puppetmaster/browser-profile",
    "\\.puppetmaster\\browser-profile",
    "/pm-cdp-",
    "\\pm-cdp-",
)
_BROWSER_HINTS = ("chrome", "chromium", "msedge", "brave", "google chrome")


def cmdline_is_marionette_browser(cmdline: str) -> bool:
    """True when argv is a browser we spawned, not the user's Chrome."""
    low = (cmdline or "").lower()
    if not any(name in low for name in _BROWSER_HINTS):
        return False
    if "--user-data-dir=" not in low:
        return False
    return any(marker.lower() in low for marker in _PROFILE_MARKERS)


def _list_process_cmdlines() -> List[Tuple[int, str]]:
    rows: List[Tuple[int, str]] = []
    if os.name == "nt":
        try:
            out = subprocess.check_output(
                [
                    "wmic",
                    "process",
                    "get",
                    "ProcessId,CommandLine",
                    "/FORMAT:LIST",
                ],
                stderr=subprocess.DEVNULL,
                timeout=8,
            )
        except Exception:
            return rows
        text = out.decode("utf-8", "replace")
        pid = 0
        cmd = ""
        for line in text.splitlines():
            raw = line.strip()
            if not raw:
                if pid > 1 and cmd:
                    rows.append((pid, cmd))
                pid = 0
                cmd = ""
                continue
            if raw.lower().startswith("commandline="):
                cmd = raw.split("=", 1)[1]
            elif raw.lower().startswith("processid="):
                try:
                    pid = int(raw.split("=", 1)[1].strip())
                except ValueError:
                    pid = 0
        if pid > 1 and cmd:
            rows.append((pid, cmd))
        return rows
    try:
        out = subprocess.check_output(
            ["ps", "-axww", "-o", "pid=,args="],
            stderr=subprocess.DEVNULL,
            timeout=8,
        )
    except Exception:
        return rows
    for line in out.decode("utf-8", "replace").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(None, 1)
        if len(parts) != 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        rows.append((pid, parts[1]))
    return rows


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
    targets = [
        pid
        for pid, cmd in _list_process_cmdlines()
        if cmdline_is_marionette_browser(cmd)
    ]
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
