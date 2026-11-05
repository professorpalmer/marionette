"""Finish bookkeeping isolates export/hook errors and persists only the owner."""
from unittest.mock import Mock

import pytest

from harness import server
from harness import hooks as _hooks


def test_finalize_turn_swallows_transcript_serialization_error(monkeypatch, owned_server):
    pilot = owned_server._pilot
    export = Mock(side_effect=RuntimeError("unserializable transcript object"))
    save = Mock()
    monkeypatch.setattr(pilot, "export_transcript_data", export)
    monkeypatch.setattr(server, "save_transcript", save)
    monkeypatch.setattr(_hooks, "run_hooks", lambda *a, **k: None)

    server._finalize_turn({"trigger": "chat", "session_id": pilot.harness_session_id, "pilot": pilot})
    export.assert_called_once_with()
    save.assert_not_called()


def test_finalize_turn_swallows_posthook_error(monkeypatch):
    class _Sessions:
        active = None  # no transcript persist; isolate the hook failure

    monkeypatch.setattr(server, "_sessions", _Sessions())
    hook = Mock(side_effect=RuntimeError("bad hook"))
    monkeypatch.setattr(_hooks, "run_hooks", hook)
    ctx = {"trigger": "chat"}

    server._finalize_turn(ctx)
    hook.assert_called_once_with("postRun", ctx)


@pytest.mark.parametrize("owner_state", ["current", "retired", "unregistered"])
def test_finalize_turn_persists_only_current_owner(monkeypatch, owned_server, owner_state):
    pilot = owned_server._pilot
    sid = pilot.harness_session_id
    transcript = {"messages": [{"role": "user", "content": "keep me"}]}
    export = Mock(return_value=transcript)
    save = Mock()
    hook = Mock()
    monkeypatch.setattr(pilot, "export_transcript_data", export)
    monkeypatch.setattr(server, "save_transcript", save)
    monkeypatch.setattr(_hooks, "run_hooks", hook)
    if owner_state != "current":
        replacement = server._build_conversational_pilot()
        if owner_state == "retired":
            server._runners.replace(sid, replacement, notify=False)
        else:
            from harness.session_runners import SessionRunnerRegistry
            registry = SessionRunnerRegistry()
            registry.get_or_create(sid, lambda: replacement)
            monkeypatch.setattr(server, "_runners", registry)
        monkeypatch.setattr(server, "_pilot", replacement)
    ctx = {"trigger": "chat", "session_id": sid, "pilot": pilot}

    server._finalize_turn(ctx)

    if owner_state == "retired":
        hook.assert_not_called()
    else:
        hook.assert_called_once_with("postRun", ctx)
    if owner_state != "current":
        export.assert_not_called()
        save.assert_not_called()
    else:
        export.assert_called_once_with()
        save.assert_called_once_with(server._cfg.state_dir, sid, transcript)
