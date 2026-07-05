"""Tests for the worker's escaped-write detector.

The ProviderWorker runs inside a git worktree and captures its patch via
`git diff` on that worktree. If the agent uses `run_command` to write to an
absolute path OUTSIDE the worktree, the write is real on disk but invisible
to the worktree diff. `_detect_escaped_writes` scans the accumulated events
for those escapes so the finalizer can surface them loudly instead of
falsely reporting "no changes produced".

These tests use plain dicts as event stand-ins to keep the helper's contract
independent of the ConvEvent import graph (the helper accepts both).
"""

import os
import tempfile

from harness.worker import _detect_escaped_writes, WorkerResult


def _ev(kind, data):
    """Minimal ConvEvent stand-in: the helper accepts dicts and objects."""
    return {"kind": kind, "data": data}


def _run_cmd(cmd):
    """Shape one run_command action_start event the way conversation.py emits."""
    return _ev("action_start", {"kind": "run_command", "goal": cmd})


def test_detects_absolute_path_cat_redirect_outside_worktree():
    # Classic escape: `cat > /abs/path` while cwd is the worktree. run_command
    # sets cwd but does NOT confine writes, so the file lands outside and the
    # worktree diff cannot see it.
    with tempfile.TemporaryDirectory() as wt:
        outside = "/tmp/outside_marionette_test/x"
        events = [_run_cmd(f"cat > {outside}")]
        found = _detect_escaped_writes(events, wt)
        assert outside in found, found


def test_detects_various_write_forms():
    with tempfile.TemporaryDirectory() as wt:
        events = [
            _run_cmd("echo hi >> /var/tmp/pm_esc_a"),
            _run_cmd("tee -a /var/tmp/pm_esc_b < input"),
            _run_cmd("cp file.txt /var/tmp/pm_esc_c"),
            _run_cmd("mv old /var/tmp/pm_esc_d"),
            _run_cmd("mkdir -p /var/tmp/pm_esc_e"),
            _run_cmd("python -c \"open('/var/tmp/pm_esc_f','w').write('x')\""),
        ]
        found = _detect_escaped_writes(events, wt)
        for expected in (
            "/var/tmp/pm_esc_a",
            "/var/tmp/pm_esc_b",
            "/var/tmp/pm_esc_c",
            "/var/tmp/pm_esc_d",
            "/var/tmp/pm_esc_e",
            "/var/tmp/pm_esc_f",
        ):
            assert expected in found, (expected, found)


def test_ignores_writes_inside_worktree():
    # An absolute path INSIDE the worktree is captured by `git diff` -- do NOT
    # flag it. Also covers the "same path as wt" edge (a write to wt itself is
    # weird but still inside).
    with tempfile.TemporaryDirectory() as wt:
        inside = os.path.join(wt, "sub", "file.txt")
        events = [
            _run_cmd(f"cat > {inside}"),
            _run_cmd(f"tee {inside}.log"),
            _run_cmd(f"mkdir -p {os.path.join(wt, 'newdir')}"),
        ]
        assert _detect_escaped_writes(events, wt) == []


def test_ignores_relative_paths():
    # Relative paths resolve under the run_command cwd (= worktree), so they
    # are inside by definition. The helper only flags ABSOLUTE targets.
    with tempfile.TemporaryDirectory() as wt:
        events = [
            _run_cmd("echo hi > out.txt"),
            _run_cmd("cat > sub/file.txt"),
            _run_cmd("cp a.txt b.txt"),
            _run_cmd("mkdir -p build"),
        ]
        assert _detect_escaped_writes(events, wt) == []


def test_worktree_prefix_is_boundary_aware():
    # "/tmp/wt" must NOT be treated as containing "/tmp/wtx/y". A naive
    # startswith check would false-negative here.
    events = [_run_cmd("cat > /tmp/wtx/y")]
    found = _detect_escaped_writes(events, "/tmp/wt")
    assert "/tmp/wtx/y" in found


def test_never_raises_on_malformed_events():
    # Every combination of "wrong shape" must be tolerated: None, empty list,
    # events missing kind/data, non-string commands, events with unexpected
    # types. The helper is called at finalize-time and must not turn a benign
    # empty-diff report into a crash.
    wt = "/tmp/some_wt_that_need_not_exist"

    # None / empty inputs
    assert _detect_escaped_writes(None, wt) == []
    assert _detect_escaped_writes([], wt) == []
    assert _detect_escaped_writes([_run_cmd("cat > /x")], "") == []

    # Malformed events
    malformed = [
        None,
        {},
        {"kind": "action_start"},                   # no data
        {"kind": "action_start", "data": None},     # data None
        {"kind": "action_start", "data": "nope"},   # data wrong type
        {"kind": "message", "data": {"text": "hi"}},  # wrong kind
        {"kind": "action_start", "data": {"kind": "read_file", "path": "/etc/passwd"}},  # not run_command
        {"kind": "action_start", "data": {"kind": "run_command"}},  # no goal
        {"kind": "action_start", "data": {"kind": "run_command", "goal": None}},
        {"kind": "action_start", "data": {"kind": "run_command", "goal": 12345}},
        {"kind": "action_start", "data": {"kind": "run_command", "goal": ""}},
    ]
    # Must not raise, and must return an empty list because none of these
    # actually describe an escape.
    assert _detect_escaped_writes(malformed, wt) == []


def test_multiple_writes_deduped_and_sorted():
    # Same escape produced twice should collapse; results are sorted for
    # deterministic reporting in the summary line.
    with tempfile.TemporaryDirectory() as wt:
        events = [
            _run_cmd("cat > /var/tmp/pm_esc_z"),
            _run_cmd("echo again >> /var/tmp/pm_esc_z"),
            _run_cmd("cat > /var/tmp/pm_esc_a"),
        ]
        found = _detect_escaped_writes(events, wt)
        assert found == sorted(set(found))
        assert "/var/tmp/pm_esc_a" in found
        assert "/var/tmp/pm_esc_z" in found


def test_worker_result_has_escaped_paths_field():
    # Additive field: default must be an empty list, and callers using the
    # existing kwargs must still work unchanged.
    r = WorkerResult(ok=True)
    assert r.escaped_paths == []
    r2 = WorkerResult(ok=False, escaped_paths=["/tmp/x"])
    assert r2.escaped_paths == ["/tmp/x"]
