"""The resource report is counts and kinds only, never free text."""
import os
import subprocess
import sys
import threading

import pytest

from harness.api.resources import ResourceServices, _child_kind, _thread_kind, get_resources


def test_thread_kinds_collapse_per_object_suffixes():
    assert _thread_kind("pmh-cmd-1a2b3c4d") == "pmh-cmd"
    assert _thread_kind("Thread-12") == "Thread"
    assert _thread_kind("Thread-17 (_read_loop_unix)") == "_read_loop_unix"
    assert _thread_kind("approval-sweep") == "approval-sweep"
    assert _thread_kind("schedule-lease-deadbeefcafe") == "schedule-lease"


def test_child_kind_never_carries_free_text():
    assert _child_kind(["/v/bin/python", "-m", "puppetmaster", "--state-dir", "/Users/x/secret", "dashboard", "--port", "8787"]) \
        == "python -m puppetmaster dashboard"
    assert _child_kind(["/v/bin/python", "-I", "-S", "/site/puppetmaster/readonly_worker.py", "--ready"]) \
        == "python readonly_worker.py"
    assert _child_kind(["agent", "Fix the login bug in /Users/x/app"]) == "agent"


@pytest.mark.skipif(os.name != "posix", reason="ps-based child listing")
def test_report_lists_children_threads_and_counts():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    stop = threading.Event()
    t = threading.Thread(target=stop.wait, name="probe-thread-99", daemon=True)
    t.start()
    try:
        status, body = get_resources(ResourceServices(counts=lambda: {"session_runners": 2}))
        assert status == 200 and body["ok"]
        assert body["counts"] == {"session_runners": 2}
        assert body["threads"]["by_kind"].get("probe-thread") == 1
        mine = [c for c in body["children"] if c["pid"] == child.pid]
        assert mine and mine[0]["kind"] == os.path.basename(sys.executable)
        assert body["rss_mb"] and body["rss_mb"] > 0
    finally:
        stop.set()
        child.kill()
        child.wait()
