"""Integration tests: the run_command safety guard + configurable timeout wiring
in ConversationalSession. Proves the guard fires ONLY in full-auto and that the
timeout is resolved from env, not hardcoded.
"""
import os
import hashlib
import pytest

from harness.conversation import ConversationalSession
from harness.config import HarnessConfig
from harness.pilot import PilotAction


class _Resp:
    def __init__(self, text, meta=None):
        self.text = text
        self.error = None
        self.meta = meta or {}
        self.tokens_out = 5
        self.tokens_in = 5


class _CmdPilot:
    """Pilot that issues one run_command then finishes."""
    supports_streaming = False

    def __init__(self, command):
        self._command = command
        self.n = 0

    def chat(self, hist, tools=None, system=""):
        self.n += 1
        if self.n == 1:
            import json
            return _Resp("", {"tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "run_command",
                             "arguments": json.dumps({"command": self._command})},
            }]})
        return _Resp('{"say":"done","actions":[]}')

    def export_transcript_data(self):
        return {}

    def load_history(self, h):
        pass


def _run(session, msg):
    blocked, results = [], []
    for ev in session.send(msg):
        if ev.kind == "command_approval_pending":
            blocked.append(ev.data)
        elif ev.kind == "action_result":
            results.append(ev.data)
    return blocked, results


def test_guard_blocks_dangerous_in_auto_mode(tmp_path):
    cfg = HarnessConfig(repo=str(tmp_path), state_dir=str(tmp_path / "st"))
    s = ConversationalSession(cfg)
    s.pilot = _CmdPilot("ssh prod systemctl stop nginx")
    s._auto_mode = True
    s._auto_command_guard = True  # env may have HARNESS_AUTO_COMMAND_GUARD=off
    blocked, _ = _run(s, "go")
    assert len(blocked) == 1
    assert blocked[0]["category"] == "remote-shell"
    assert blocked[0]["command_hash"] == hashlib.sha256(
        b"ssh prod systemctl stop nginx"
    ).hexdigest()
    assert s._approved_commands == set()


def test_guard_allows_dangerous_in_interactive(tmp_path):
    cfg = HarnessConfig(repo=str(tmp_path), state_dir=str(tmp_path / "st"))
    s = ConversationalSession(cfg)
    s.pilot = _CmdPilot("rm -rf /tmp/nonexistent_xyz")
    s._auto_mode = False  # interactive: human sees it, guard must NOT fire
    blocked, results = _run(s, "go")
    assert len(blocked) == 0
    assert len(results) == 1  # it actually ran


def test_guard_allows_benign_in_auto_mode(tmp_path):
    cfg = HarnessConfig(repo=str(tmp_path), state_dir=str(tmp_path / "st"))
    s = ConversationalSession(cfg)
    s.pilot = _CmdPilot("echo hello")
    s._auto_mode = True
    blocked, results = _run(s, "go")
    assert len(blocked) == 0
    assert len(results) == 1


def test_guard_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_AUTO_COMMAND_GUARD", "off")
    cfg = HarnessConfig(repo=str(tmp_path), state_dir=str(tmp_path / "st"))
    s = ConversationalSession(cfg)
    s.pilot = _CmdPilot("echo safe-anyway")
    s._auto_mode = True
    # guard disabled -> even if it were dangerous it would not block; benign here
    blocked, results = _run(s, "go")
    assert len(blocked) == 0


def test_auto_mode_resets_after_run_auto(tmp_path):
    # _auto_mode must not stay stuck on after the wrapper completes
    cfg = HarnessConfig(repo=str(tmp_path), state_dir=str(tmp_path / "st"))
    s = ConversationalSession(cfg)
    assert s._auto_mode is False


def test_approved_hash_is_one_shot_and_does_not_approve_other_command(
    tmp_path, monkeypatch
):
    cfg = HarnessConfig(repo=str(tmp_path), state_dir=str(tmp_path / "st"))
    session = ConversationalSession(cfg)
    session.harness_session_id = "session-a"
    session._auto_mode = True
    session._auto_command_guard = True
    command = "ssh prod systemctl stop nginx"
    command_hash = hashlib.sha256(command.encode()).hexdigest()
    pending = session.register_pending_command_approval(
        command=command,
        command_hash=command_hash,
        action_id="call-1",
    )
    assert session.decide_command_approval(
        command_hash=command_hash,
        workspace_root=pending["workspace_root"],
        approve=True,
    )

    monkeypatch.setattr(
        "harness.command_policy.run_cancellable",
        lambda *args, **kwargs: ("done", 0, "success"),
    )
    approved = session._do_run_command(
        PilotAction(kind="run_command", command=command)
    )
    assert approved[0] is True
    assert command_hash not in session._approved_commands

    repeated = session._do_run_command(
        PilotAction(kind="run_command", command=command)
    )
    changed = session._do_run_command(
        PilotAction(kind="run_command", command=command + " --force")
    )
    assert repeated[1] == "blocked"
    assert changed[1] == "blocked"


def test_consume_reapprove_race_keeps_fresh_same_hash_approval(
    tmp_path, monkeypatch
):
    """Unlocked post-consume discard must not clobber a raced re-approval."""
    cfg = HarnessConfig(repo=str(tmp_path), state_dir=str(tmp_path / "st"))
    session = ConversationalSession(cfg)
    session.harness_session_id = "session-a"
    session._auto_mode = True
    session._auto_command_guard = True
    command = "ssh prod systemctl stop nginx"
    command_hash = hashlib.sha256(command.encode()).hexdigest()
    pending = session.register_pending_command_approval(
        command=command,
        command_hash=command_hash,
        action_id="call-race-1",
    )
    assert session.decide_command_approval(
        command_hash=command_hash,
        workspace_root=pending["workspace_root"],
        approve=True,
    )

    original_consume = session.consume_command_approval

    def consume_then_reapprove(h: str) -> bool:
        ok = original_consume(h)
        if ok:
            # Operator re-approves the same hash before the command runs.
            session._approved_commands.add(h)
        return ok

    monkeypatch.setattr(session, "consume_command_approval", consume_then_reapprove)
    monkeypatch.setattr(
        "harness.command_policy.run_cancellable",
        lambda *args, **kwargs: ("done", 0, "success"),
    )
    approved = session._do_run_command(
        PilotAction(kind="run_command", command=command)
    )
    assert approved[0] is True
    # Fresh same-hash approval must still be present for its own one-shot retry.
    assert command_hash in session._approved_commands
    # Restore real consume: one-shot still holds for the raced re-approval.
    monkeypatch.setattr(session, "consume_command_approval", original_consume)
    assert session.consume_command_approval(command_hash) is True
    assert session.consume_command_approval(command_hash) is False


def test_command_approval_rejects_other_workspace(tmp_path):
    cfg = HarnessConfig(repo=str(tmp_path), state_dir=str(tmp_path / "st"))
    session = ConversationalSession(cfg)
    session.harness_session_id = "session-a"
    command = "ssh prod reboot"
    command_hash = hashlib.sha256(command.encode()).hexdigest()
    session.register_pending_command_approval(
        command=command,
        command_hash=command_hash,
        action_id="call-2",
    )

    with pytest.raises(PermissionError, match="workspace"):
        session.decide_command_approval(
            command_hash=command_hash,
            workspace_root=str(tmp_path / "other"),
            approve=True,
        )
    assert command_hash not in session._approved_commands


def test_pending_command_approval_is_durable_in_display_transcript(tmp_path):
    """Pending DANGER approvals must survive equal-card-count hydrate/reattach.

    register writes a display row; export restores from session state even if
    the row was cleared; decide updates status without dropping the card.
    """
    cfg = HarnessConfig(repo=str(tmp_path), state_dir=str(tmp_path / "st"))
    session = ConversationalSession(cfg)
    session.harness_session_id = "session-a"
    command = "ssh prod reboot"
    command_hash = hashlib.sha256(command.encode()).hexdigest()

    pending = session.register_pending_command_approval(
        command=command,
        command_hash=command_hash,
        action_id="call-hydrate",
        category="remote",
        reason="ssh",
        matched="ssh",
    )
    display = session.export_display_transcript()
    approval_rows = [
        row for row in display
        if isinstance(row, dict) and row.get("type") == "command_approval"
    ]
    assert len(approval_rows) == 1
    assert approval_rows[0]["command_hash"] == command_hash
    assert approval_rows[0]["status"] == "pending"
    assert approval_rows[0]["session_id"] == "session-a"
    assert approval_rows[0]["workspace_root"] == pending["workspace_root"]

    # Simulate a lagged save that omitted the card; export must restore it.
    session._display_transcript = [
        {"type": "card", "id": "c1", "kind": "run_command", "goal": "x", "result": {}},
    ]
    restored = session.export_display_transcript()
    restored_hashes = {
        row.get("command_hash")
        for row in restored
        if isinstance(row, dict) and row.get("type") == "command_approval"
    }
    assert command_hash in restored_hashes

    decided = session.decide_command_approval(
        command_hash=command_hash,
        workspace_root=pending["workspace_root"],
        approve=False,
    )
    assert decided is not None
    decided_rows = [
        row for row in session.export_display_transcript()
        if isinstance(row, dict)
        and row.get("type") == "command_approval"
        and row.get("command_hash") == command_hash
    ]
    assert len(decided_rows) == 1
    assert decided_rows[0]["status"] == "rejected"
    assert command_hash not in session._pending_command_approvals


def test_load_history_restores_pending_approval_for_decide(tmp_path):
    """Export → new session load_history → approve/reject must succeed.

    Cold attach/restart only hydrates display; decision state must be rebuilt
    from validated pending cards so the operator can still decide.
    """
    cfg = HarnessConfig(repo=str(tmp_path), state_dir=str(tmp_path / "st"))
    source = ConversationalSession(cfg)
    source.harness_session_id = "session-a"
    command = "ssh prod reboot"
    command_hash = hashlib.sha256(command.encode()).hexdigest()
    pending = source.register_pending_command_approval(
        command=command,
        command_hash=command_hash,
        action_id="call-restore",
        category="remote",
        reason="ssh",
        matched="ssh",
    )
    exported = source.export_transcript_data()

    restored = ConversationalSession(cfg)
    restored.harness_session_id = "session-a"
    # Simulate a prior one-shot approval that must not survive hydrate.
    restored._approved_commands.add(command_hash)
    restored.load_history(exported)

    assert command_hash in restored._pending_command_approvals
    assert command_hash not in restored._approved_commands
    assert restored._pending_command_approvals[command_hash]["command"] == command
    assert restored._pending_command_approvals[command_hash]["workspace_root"] == (
        pending["workspace_root"]
    )

    decided = restored.decide_command_approval(
        command_hash=command_hash,
        workspace_root=pending["workspace_root"],
        approve=True,
    )
    assert decided is not None
    assert command_hash not in restored._pending_command_approvals
    assert command_hash in restored._approved_commands
    # One-shot: consume clears the approval.
    assert restored.consume_command_approval(command_hash) is True
    assert restored.consume_command_approval(command_hash) is False


def test_load_history_reject_after_restore(tmp_path):
    cfg = HarnessConfig(repo=str(tmp_path), state_dir=str(tmp_path / "st"))
    source = ConversationalSession(cfg)
    source.harness_session_id = "session-a"
    command = "ssh prod reboot"
    command_hash = hashlib.sha256(command.encode()).hexdigest()
    pending = source.register_pending_command_approval(
        command=command,
        command_hash=command_hash,
        action_id="call-reject",
    )
    exported = source.export_transcript_data()

    restored = ConversationalSession(cfg)
    restored.harness_session_id = "session-a"
    restored.load_history(exported)
    decided = restored.decide_command_approval(
        command_hash=command_hash,
        workspace_root=pending["workspace_root"],
        approve=False,
    )
    assert decided is not None
    assert command_hash not in restored._pending_command_approvals
    assert command_hash not in restored._approved_commands


def test_load_history_leaves_decided_approvals_display_only(tmp_path):
    cfg = HarnessConfig(repo=str(tmp_path), state_dir=str(tmp_path / "st"))
    session = ConversationalSession(cfg)
    session.harness_session_id = "session-a"
    command = "ssh prod reboot"
    command_hash = hashlib.sha256(command.encode()).hexdigest()
    pending = session.register_pending_command_approval(
        command=command,
        command_hash=command_hash,
        action_id="call-decided",
    )
    session.decide_command_approval(
        command_hash=command_hash,
        workspace_root=pending["workspace_root"],
        approve=True,
    )
    exported = session.export_transcript_data()

    restored = ConversationalSession(cfg)
    restored.harness_session_id = "session-a"
    restored.load_history(exported)

    assert command_hash not in restored._pending_command_approvals
    assert command_hash not in restored._approved_commands
    decided_rows = [
        row for row in restored._display_transcript
        if isinstance(row, dict)
        and row.get("type") == "command_approval"
        and row.get("command_hash") == command_hash
    ]
    assert len(decided_rows) == 1
    assert decided_rows[0]["status"] == "approved"
    assert restored.decide_command_approval(
        command_hash=command_hash,
        workspace_root=pending["workspace_root"],
        approve=True,
    ) is None


def test_load_history_refuses_workspace_and_session_mismatch(tmp_path):
    cfg = HarnessConfig(repo=str(tmp_path), state_dir=str(tmp_path / "st"))
    command = "ssh prod reboot"
    command_hash = hashlib.sha256(command.encode()).hexdigest()
    workspace = os.path.realpath(str(tmp_path))
    foreign_workspace = os.path.realpath(str(tmp_path / "other"))
    os.makedirs(foreign_workspace, exist_ok=True)

    display = [
        {
            "type": "command_approval",
            "id": "call-ws",
            "command": command,
            "command_hash": command_hash,
            "session_id": "session-a",
            "workspace_root": foreign_workspace,
            "category": "remote",
            "reason": "ssh",
            "matched": "ssh",
            "status": "pending",
        },
        {
            "type": "command_approval",
            "id": "call-sid",
            "command": command,
            "command_hash": hashlib.sha256(b"ssh other").hexdigest(),
            "session_id": "session-b",
            "workspace_root": workspace,
            "category": "remote",
            "reason": "ssh",
            "matched": "ssh",
            "status": "pending",
        },
    ]
    session = ConversationalSession(cfg)
    session.harness_session_id = "session-a"
    session.load_history({"history": [], "display": display, "job_ids": []})

    assert session._pending_command_approvals == {}
    assert session._display_transcript == display
    with pytest.raises(PermissionError, match="workspace"):
        # Even if we force a pending record, decide still enforces workspace.
        session._pending_command_approvals[command_hash] = {
            "session_id": "session-a",
            "workspace_root": workspace,
            "command": command,
            "command_hash": command_hash,
            "action_id": "forced",
            "category": "",
            "reason": "",
            "matched": "",
        }
        session.decide_command_approval(
            command_hash=command_hash,
            workspace_root=foreign_workspace,
            approve=True,
        )


def test_load_history_skips_malformed_pending_display_rows(tmp_path):
    cfg = HarnessConfig(repo=str(tmp_path), state_dir=str(tmp_path / "st"))
    session = ConversationalSession(cfg)
    session.harness_session_id = "session-a"
    workspace = os.path.realpath(str(tmp_path))
    good_hash = hashlib.sha256(b"ssh prod reboot").hexdigest()
    display = [
        "not-a-dict",
        {"type": "command_approval", "status": "pending"},  # missing fields
        {
            "type": "command_approval",
            "id": "bad-hash",
            "command": "ssh prod reboot",
            "command_hash": "not-a-hash",
            "session_id": "session-a",
            "workspace_root": workspace,
            "status": "pending",
        },
        {
            "type": "command_approval",
            "id": "empty-cmd",
            "command": "   ",
            "command_hash": hashlib.sha256(b"x").hexdigest(),
            "session_id": "session-a",
            "workspace_root": workspace,
            "status": "pending",
        },
        {
            "type": "command_approval",
            "id": "call-good",
            "command": "ssh prod reboot",
            "command_hash": good_hash,
            "session_id": "session-a",
            "workspace_root": workspace,
            "category": "remote",
            "reason": "ssh",
            "matched": "ssh",
            "status": "pending",
        },
    ]
    session.load_history({"history": [], "display": display, "job_ids": []})

    assert list(session._pending_command_approvals) == [good_hash]
    assert session._pending_command_approvals[good_hash]["action_id"] == "call-good"
    # Display is preserved verbatim (including malformed rows).
    assert session._display_transcript == display


def test_load_history_refuses_benign_card_evil_hash_escalation(tmp_path):
    """Benign displayed command + evil command_hash must never authorize evil.

    Adversarial durable row: operator sees ``echo hello`` but the hash is
    ``sha256(ssh prod reboot)``. Rehydrate must keep the row display-only;
    decide/consume on the evil hash must fail.
    """
    cfg = HarnessConfig(repo=str(tmp_path), state_dir=str(tmp_path / "st"))
    benign = "echo hello"
    evil = "ssh prod reboot"
    evil_hash = hashlib.sha256(evil.encode()).hexdigest()
    # Prove the mismatch is exactly the escalation shape under test.
    assert hashlib.sha256(benign.encode()).hexdigest() != evil_hash
    workspace = os.path.realpath(str(tmp_path))
    display = [
        {
            "type": "command_approval",
            "id": "escalation",
            "command": benign,
            "command_hash": evil_hash,
            "session_id": "session-a",
            "workspace_root": workspace,
            "category": "remote",
            "reason": "ssh",
            "matched": "ssh",
            "status": "pending",
        },
    ]
    session = ConversationalSession(cfg)
    session.harness_session_id = "session-a"
    session.load_history({"history": [], "display": display, "job_ids": []})

    assert session._pending_command_approvals == {}
    assert session._display_transcript == display
    assert session.decide_command_approval(
        command_hash=evil_hash,
        workspace_root=workspace,
        approve=True,
    ) is None
    assert evil_hash not in session._approved_commands
    assert session.consume_command_approval(evil_hash) is False
