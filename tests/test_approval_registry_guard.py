from __future__ import annotations

import time

from harness.conversation import ConversationalSession


class _Cfg:
    repo = ""
    driver = "local"
    model = "dummy"


def _session(tmp_path):
    session = ConversationalSession.__new__(ConversationalSession)
    session.config = _Cfg()
    session.harness_session_id = "sess"
    session.state_dir = str(tmp_path)
    session._pending_command_approvals = {}
    session._approved_commands = set()
    session._approval_sweeper = True
    session._command_approval_lock_guard = lambda: _Null()
    session._upsert_display_command_approval = lambda *a, **k: None
    session._approval_workspace_key = lambda root: root
    return session


class _Null:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_257th_pending_approval_is_denied(tmp_path, monkeypatch):
    session = _session(tmp_path)
    monkeypatch.setattr("harness.conversation.MAX_PENDING_APPROVALS", 2)
    first = session.register_pending_command_approval(
        command="echo 1", command_hash="a" * 64, action_id="1"
    )
    session.register_pending_command_approval(
        command="echo 2", command_hash="b" * 64, action_id="2"
    )
    third = session.register_pending_command_approval(
        command="echo 3", command_hash="c" * 64, action_id="3"
    )
    assert first.get("command_hash") == "a" * 64
    assert third.get("denied") == "approval registry full"
    assert "c" * 64 not in session._pending_command_approvals
    assert len(session._pending_command_approvals) == 2


def test_expired_approvals_resolve_deny(tmp_path, monkeypatch):
    session = _session(tmp_path)
    monkeypatch.setattr("harness.conversation.APPROVAL_TTL_SECONDS", 1)
    session.register_pending_command_approval(
        command="echo 1", command_hash="d" * 64, action_id="1"
    )
    session._pending_command_approvals["d" * 64]["expires_at"] = time.time() - 1
    session._sweep_expired_approvals()
    assert session._pending_command_approvals == {}
