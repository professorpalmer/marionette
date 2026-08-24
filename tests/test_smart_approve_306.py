"""v0.9.306: persistent allowlist, turn identity, smart_approve verdicts."""
from __future__ import annotations

import os
from pathlib import Path

from harness.approval_identity import approval_turn, get_approval_turn_id
from harness.command_allowlist import (
    ALLOWLIST_FILENAME,
    allowlist_add,
    allowlist_contains,
    load_allowlist,
)
from harness.command_policy import classify_command, suggested_amendment
from harness.smart_approve import smart_approve_verdict


def test_allowlist_persists_under_harness_state_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    cmd = "pytest tests/test_smart_approve_306.py"
    assert allowlist_contains(cmd) is False
    assert allowlist_add(cmd) is True
    path = tmp_path / ALLOWLIST_FILENAME
    assert path.is_file()
    # New reader (fresh load) still sees the command.
    assert allowlist_contains(cmd) is True
    assert cmd in load_allowlist()
    # Isolation: a second state dir does not inherit.
    other = tmp_path / "other"
    other.mkdir()
    assert allowlist_contains(cmd, state_dir=str(other)) is False


def test_turn_identity_is_contextvar_scoped() -> None:
    assert get_approval_turn_id() is None
    with approval_turn("turn-7"):
        assert get_approval_turn_id() == "turn-7"
        with approval_turn(8):
            assert get_approval_turn_id() == "8"
        assert get_approval_turn_id() == "turn-7"
    assert get_approval_turn_id() is None


def test_smart_approve_allowlist_wins_even_for_danger(tmp_path: Path) -> None:
    danger = "rm -rf /tmp/scratch-ok"
    assert classify_command(danger).danger is True
    assert smart_approve_verdict(danger, state_dir=str(tmp_path)) == "pending"
    assert allowlist_add(danger, state_dir=str(tmp_path))
    assert smart_approve_verdict(danger, state_dir=str(tmp_path)) == "approve"


def test_smart_approve_amendment_then_pending() -> None:
    force = "git push --force origin main"
    assert suggested_amendment(force)
    assert smart_approve_verdict(force) == "amend"
    ssh = "ssh prod reboot"
    assert classify_command(ssh).danger is True
    assert suggested_amendment(ssh) is None
    assert smart_approve_verdict(ssh) == "pending"


def test_danger_without_allowlist_stays_pending(tmp_path: Path) -> None:
    """Do not auto-run danger unless the allowlist already has the command."""
    cmd = "sudo reboot"
    assert classify_command(cmd).danger is True
    assert smart_approve_verdict(cmd, state_dir=str(tmp_path)) == "pending"
    assert allowlist_contains(cmd, state_dir=str(tmp_path)) is False


def test_decide_reads_turn_identity_and_persists_allowlist(tmp_path: Path) -> None:
    """Mirrors decide_command_approval side effects without importing conversation."""
    cmd = "ssh prod reboot"
    pending = {"command": cmd}
    with approval_turn("t-42"):
        result = dict(pending)
        turn_id = get_approval_turn_id()
        if turn_id:
            result["turn_id"] = turn_id
        assert allowlist_add(cmd, state_dir=str(tmp_path))
    assert result["turn_id"] == "t-42"
    assert get_approval_turn_id() is None
    assert allowlist_contains(cmd, state_dir=str(tmp_path))
    assert smart_approve_verdict(cmd, state_dir=str(tmp_path)) == "approve"
