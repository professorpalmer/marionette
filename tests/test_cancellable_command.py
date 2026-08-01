"""Tests for the cancellable command runner. The gap it closes: subprocess.run
blocks the thread uninterruptibly, so a user Stop could not kill a long/unbounded
command. run_cancellable polls a cancel event and kills the whole process group.
"""
import os
import sys
import threading
import time

import pytest

from harness.command_policy import run_cancellable


def _sleep_cmd(seconds, then_echo=None):
    """A long-running command that works under both /bin/sh and cmd.exe.

    POSIX `sleep` doesn't exist on Windows, and cmd has no `;` chaining --
    a Python one-liner via the current interpreter behaves identically on
    both, so these tests exercise the real kill path everywhere.
    """
    body = f"import time; time.sleep({seconds})"
    if then_echo:
        body += f"; print({then_echo!r})"
    return f'"{sys.executable}" -c "{body}"'


def test_normal_completion():
    out, code, status = run_cancellable("echo hello", timeout=10)
    assert "hello" in out
    assert code == 0
    assert status == "ok"


def test_nonzero_exit():
    out, code, status = run_cancellable("exit 3", timeout=10)
    assert code == 3
    assert status == "ok"


def test_cancel_kills_promptly():
    ev = threading.Event()
    threading.Thread(target=lambda: (time.sleep(0.3), ev.set())).start()
    t0 = time.time()
    out, code, status = run_cancellable(_sleep_cmd(30), timeout=None, cancel_event=ev)
    elapsed = time.time() - t0
    assert status == "cancelled"
    assert code == 130
    assert elapsed < 5, f"cancel took {elapsed}s -- should be sub-second"
    assert "interrupted by user" in out


def test_stale_preset_cancel_does_not_kill_fresh_command():
    """A cancel flag already set at launch is stale (sibling-stream poison /
    leftover interrupt), NOT a stop aimed at this command. The runner must ignore
    it and let the command complete, instead of instakilling it and mislabeling
    the output "[interrupted by user]". This is the regression that made every
    shell command die while file reads (which never consult the flag) worked."""
    ev = threading.Event()
    ev.set()  # poisoned BEFORE the command runs
    # Must outlive one poll interval (0.1s) so the runner actually reaches its
    # cancel check -- a sub-0.1s command finishes first and never exercises the
    # race, so it cannot distinguish level- from edge-triggered behavior.
    out, code, status = run_cancellable(_sleep_cmd(0.3, then_echo="alive"), timeout=10, cancel_event=ev)
    assert status == "ok", f"stale pre-set flag wrongly cancelled: {status}"
    assert code == 0
    assert "alive" in out
    assert "interrupted by user" not in out


def test_cancel_set_during_run_still_kills():
    """Edge-triggering must not disarm a REAL stop: a clear->set transition that
    happens while the command is running is honored and kills the group."""
    ev = threading.Event()  # clear at launch
    threading.Thread(target=lambda: (time.sleep(0.3), ev.set())).start()
    t0 = time.time()
    out, code, status = run_cancellable(_sleep_cmd(30), timeout=None, cancel_event=ev)
    elapsed = time.time() - t0
    assert status == "cancelled"
    assert code == 130
    assert elapsed < 5, f"cancel took {elapsed}s -- should be sub-second"
    assert "interrupted by user" in out


def test_timeout_kills():
    t0 = time.time()
    out, code, status = run_cancellable(_sleep_cmd(30), timeout=1)
    elapsed = time.time() - t0
    assert status == "timeout"
    assert elapsed < 5
    assert "TimeoutExpired" in out


import sys as _sys


@pytest.mark.skipif(
    _sys.platform == "win32",
    reason="pgrep/sh job-control constructs are POSIX-only; the Windows tree-kill "
           "path (taskkill /T) is covered by test_cancel_kills_promptly.",
)
@pytest.mark.skipif(
    _sys.platform.startswith("linux"),
    reason=(
        "Known limitation: Popen(shell=True) uses /bin/sh, which is dash on "
        "Linux. With the pathological 'cmd & cmd & wait' construct, dash's "
        "backgrounded children can escape the process group, so killpg cannot "
        "reap them -- a survivor lingers. macOS /bin/sh keeps them in-group. "
        "Real Stop usage (a single long-running command) is group-killed "
        "correctly on every platform (see test_cancel_kills_promptly). Tracked "
        "as a dash-specific edge case; not worth a session-reaper rewrite now."
    ),
)
def test_process_group_kill_no_orphans():
    # Children spawned by the shell must also die (group kill, not just the parent
    # shell). Unique sentinel sleep DURATION (visible in each child's argv, unlike
    # a comment which the shell drops on exec) so pgrep counts only our children.
    # Poll for reap (SIGTERM -> grace -> SIGKILL + OS reaping is async).
    import subprocess as sp
    dur = "778231"  # unique sentinel; appears in each child sleep's argv
    ev = threading.Event()
    threading.Thread(target=lambda: (time.sleep(0.3), ev.set())).start()
    run_cancellable(f"sleep {dur} & sleep {dur} & wait", timeout=None, cancel_event=ev)
    deadline = time.time() + 8.0
    remaining = None
    while time.time() < deadline:
        n = sp.run(f"pgrep -f 'sleep {dur}' | wc -l", shell=True, capture_output=True, text=True)
        remaining = n.stdout.strip()
        if remaining == "0":
            break
        time.sleep(0.2)
    assert remaining == "0", f"child processes were orphaned, not group-killed (remaining={remaining})"


def test_bad_command_does_not_raise():
    out, code, status = run_cancellable("this_command_does_not_exist_xyz", timeout=5)
    # shell returns 127 for not-found; never raises
    assert code != 0
    assert status in ("ok", "error")


def test_output_cap_marks_truncated_and_preserves_prefix(monkeypatch):
    """Wave 1: runaway stdout must surface status=truncated with partial output,
    never a silent ok that looks like a successful validation dump."""
    import harness.command_policy as cp

    monkeypatch.setattr(cp, "MAX_CAPTURED_OUTPUT", 2048)
    # Print enough bytes to trip the cap; portable across shells.
    body = (
        "import sys;"
        "sys.stdout.write('HEADMARK' + ('Z' * 8000));"
        "sys.stdout.flush()"
    )
    out, code, status = run_cancellable(
        f'"{sys.executable}" -c "{body}"',
        timeout=10,
    )
    assert status == "truncated"
    assert code == -1
    assert out.startswith("HEADMARK")
    assert "truncated" in out.lower()
    assert len(out) <= 2048 + 80  # cap + marker room


def test_run_cancellable_off_mode_unchanged(monkeypatch):
    """Default HARNESS_OS_SANDBOX=off preserves direct Popen path."""
    monkeypatch.delenv("HARNESS_OS_SANDBOX", raising=False)
    out, code, status = run_cancellable("echo direct_path", timeout=10)
    assert "direct_path" in out
    assert code == 0
    assert status == "ok"


def test_run_cancellable_required_unavailable_fails_closed(monkeypatch):
    """required mode must fail before spawn when no sandbox backend exists."""
    from harness import os_sandbox

    os_sandbox.reset_probe_cache()
    monkeypatch.setenv("HARNESS_OS_SANDBOX", "required")
    monkeypatch.setattr(
        os_sandbox,
        "detect_sandbox_capability",
        lambda *a, **k: os_sandbox.SandboxCapability("test", None, False),
    )
    out, code, status = run_cancellable("echo should_not_run", timeout=10)
    assert status == "error"
    assert code == -1
    assert "required" in out.lower()
    assert "should_not_run" not in out


def test_run_cancellable_auto_uses_sandbox_when_available(monkeypatch):
    """auto mode wraps the command when a sandbox backend is reported available."""
    from harness import os_sandbox

    os_sandbox.reset_probe_cache()
    monkeypatch.setenv("HARNESS_OS_SANDBOX", "auto")
    captured = {}

    def _fake_prepare(command, *, cwd=None, env=None):
        captured["command"] = command
        return os_sandbox.SandboxSpawnPlan(
            command=f"sh -c {command!r}",
            cleanup=None,
        )

    monkeypatch.setattr(os_sandbox, "prepare_sandbox_spawn", _fake_prepare)
    out, code, status = run_cancellable("echo wrapped", timeout=10)
    assert captured.get("command") == "echo wrapped"
    assert "wrapped" in out
    assert code == 0
    assert status == "ok"


@pytest.mark.skipif(
    __import__("sys").platform != "darwin"
    or not __import__("harness.os_sandbox", fromlist=["_probe_macos_sandbox_exec"])._probe_macos_sandbox_exec(),
    reason="macOS sandbox-exec not available",
)
def test_run_cancellable_sandbox_integration_echo():
    from harness import os_sandbox

    os_sandbox.reset_probe_cache()
    prev = os.environ.get("HARNESS_OS_SANDBOX")
    os.environ["HARNESS_OS_SANDBOX"] = "auto"
    try:
        out, code, status = run_cancellable("echo sandbox_integration", timeout=10)
        assert status == "ok"
        assert code == 0
        assert "sandbox_integration" in out
    finally:
        if prev is None:
            os.environ.pop("HARNESS_OS_SANDBOX", None)
        else:
            os.environ["HARNESS_OS_SANDBOX"] = prev
        os_sandbox.reset_probe_cache()


@pytest.mark.skipif(
    __import__("sys").platform != "linux"
    or not __import__("harness.os_sandbox", fromlist=["_probe_linux_bubblewrap"])._probe_linux_bubblewrap(),
    reason="Linux bubblewrap not available",
)
def test_run_cancellable_sandbox_integration_echo_linux():
    from harness import os_sandbox

    os_sandbox.reset_probe_cache()
    prev = os.environ.get("HARNESS_OS_SANDBOX")
    os.environ["HARNESS_OS_SANDBOX"] = "auto"
    try:
        out, code, status = run_cancellable("echo sandbox_integration", timeout=10)
        assert status == "ok"
        assert code == 0
        assert "sandbox_integration" in out
    finally:
        if prev is None:
            os.environ.pop("HARNESS_OS_SANDBOX", None)
        else:
            os.environ["HARNESS_OS_SANDBOX"] = prev
        os_sandbox.reset_probe_cache()
