import copy
import hashlib
import os
from types import SimpleNamespace

from harness.api.command_approvals import (
    CommandApprovalServices,
    post_command_approval,
    post_command_rejection,
)
from harness.config import HarnessConfig
from harness.conversation import ConversationalSession


COMMAND_HASH = "a" * 64


class _Runner:
    harness_session_id = "session-a"

    def __init__(self):
        self.decisions = []

    def decide_command_approval(self, **decision):
        self.decisions.append(decision)
        if decision["workspace_root"] != "/workspace/a":
            raise PermissionError("command approval workspace does not match")
        return {
            "workspace_root": "/workspace/a",
            "command": "ssh prod reboot",
        }


def _services(runners):
    registry = SimpleNamespace(get=lambda session_id: runners.get(session_id))
    return CommandApprovalServices(get_runners=lambda: registry)


def test_approval_targets_one_session_and_hash():
    runner_a = _Runner()
    runner_b = _Runner()
    runner_b.harness_session_id = "session-b"
    status, payload = post_command_approval(
        {
            "session_id": "session-a",
            "workspace_root": "/workspace/a",
            "command_hash": COMMAND_HASH,
        },
        _services({"session-a": runner_a, "session-b": runner_b}),
    )

    assert status == 200
    assert payload["decision"] == "approved"
    assert payload["retry_command"] == "ssh prod reboot"
    assert runner_a.decisions == [{
        "command_hash": COMMAND_HASH,
        "workspace_root": "/workspace/a",
        "approve": True,
    }]
    assert runner_b.decisions == []


def test_rejection_does_not_return_retry_command():
    runner = _Runner()
    status, payload = post_command_rejection(
        {
            "session_id": "session-a",
            "workspace_root": "/workspace/a",
            "command_hash": COMMAND_HASH,
        },
        _services({"session-a": runner}),
    )

    assert status == 200
    assert payload["decision"] == "rejected"
    assert payload["retry_command"] == ""
    assert runner.decisions[0]["approve"] is False


def test_approval_rejects_wrong_workspace_and_unknown_hash_shape():
    runner = _Runner()
    services = _services({"session-a": runner})
    wrong_workspace = post_command_approval(
        {
            "session_id": "session-a",
            "workspace_root": "/workspace/b",
            "command_hash": COMMAND_HASH,
        },
        services,
    )
    malformed_hash = post_command_approval(
        {
            "session_id": "session-a",
            "workspace_root": "/workspace/a",
            "command_hash": "not-a-hash",
        },
        services,
    )

    assert wrong_workspace[0] == 403
    assert malformed_hash[0] == 400


def test_approval_does_not_fall_back_to_active_or_other_session():
    status, payload = post_command_approval(
        {
            "session_id": "missing",
            "workspace_root": "/workspace/a",
            "command_hash": COMMAND_HASH,
        },
        _services({"session-a": _Runner()}),
    )

    assert status == 404
    assert "runner" in payload["error"]


def test_api_approve_after_export_load_history_roundtrip(tmp_path):
    """HTTP approve works on a session rebuilt from durable display hydrate."""
    cfg = HarnessConfig(repo=str(tmp_path), state_dir=str(tmp_path / "st"))
    source = ConversationalSession(cfg)
    source.harness_session_id = "session-a"
    command = "ssh prod reboot"
    command_hash = hashlib.sha256(command.encode()).hexdigest()
    pending = source.register_pending_command_approval(
        command=command,
        command_hash=command_hash,
        action_id="call-api",
        category="remote",
        reason="ssh",
        matched="ssh",
    )
    exported = copy.deepcopy(source.export_transcript_data())

    restored = ConversationalSession(cfg)
    restored.harness_session_id = "session-a"
    restored.load_history(copy.deepcopy(exported))
    services = _services({"session-a": restored})

    status, payload = post_command_approval(
        {
            "session_id": "session-a",
            "workspace_root": pending["workspace_root"],
            "command_hash": command_hash,
        },
        services,
    )
    assert status == 200
    assert payload["decision"] == "approved"
    assert payload["retry_command"] == command
    assert command_hash not in restored._pending_command_approvals

    # Wrong workspace still refused after restore.
    restored2 = ConversationalSession(cfg)
    restored2.harness_session_id = "session-a"
    restored2.load_history(copy.deepcopy(exported))
    wrong = post_command_approval(
        {
            "session_id": "session-a",
            "workspace_root": str(tmp_path / "other"),
            "command_hash": command_hash,
        },
        _services({"session-a": restored2}),
    )
    assert wrong[0] == 403
    assert command_hash in restored2._pending_command_approvals
    assert os.path.realpath(str(tmp_path / "other")) != pending["workspace_root"]
