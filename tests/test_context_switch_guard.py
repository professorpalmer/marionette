"""Hermetic tests for the context-switch destructive-command latch."""
from harness.command_policy import classify_command, guard_destructive_command
from harness.context_switch_guard import (
    confirm_workspace,
    is_armed,
    note_switch,
    reset_for_tests,
    snapshot,
)


DANGER = "rm -rf /tmp/marionette-scratch"
SAFE = "ls -la"


def setup_function(_fn=None):
    reset_for_tests()


def teardown_function(_fn=None):
    reset_for_tests()


def test_arm_blocks_danger_until_confirm():
    assert classify_command(DANGER).danger is True
    assert is_armed() is False
    note_switch("session", "/old/repo", "/new/repo")
    assert is_armed() is True
    blocked = guard_destructive_command(DANGER)
    assert blocked.danger is True
    # Classify category wins; latch must not rewrite remote-shell / rm / etc.
    assert blocked.category == classify_command(DANGER).category
    assert blocked.category != "context-switch-unconfirmed"
    assert is_armed() is True

    confirm_workspace("/new/repo")
    assert is_armed() is False
    allowed = guard_destructive_command(DANGER)
    assert allowed.danger is True
    assert allowed.category != "context-switch-unconfirmed"
    assert allowed.category == classify_command(DANGER).category


def test_safe_command_never_blocked_by_latch():
    note_switch("workspace", "/old", "/new")
    assert is_armed() is True
    assert classify_command(SAFE).danger is False
    verdict = guard_destructive_command(SAFE)
    assert verdict.danger is False
    assert verdict.category == ""


def test_same_root_switch_does_not_arm():
    note_switch("session", "/same/repo", "/same/repo")
    assert is_armed() is False
    assert guard_destructive_command(DANGER).category == classify_command(DANGER).category


def test_note_switch_snapshot():
    snap = note_switch("relocate", "/a", "/b")
    assert snap["armed"] is True
    assert snap["kind"] == "relocate"
    assert snapshot()["new"] == "/b"
