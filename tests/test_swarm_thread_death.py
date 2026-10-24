"""A swarm thread that dies without a terminal message fails the action."""
from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

from harness.pilot import PilotAction
from harness.send_loop_dispatch import dispatch_swarm_action


def test_dead_swarm_thread_fails_the_action(monkeypatch):
    import harness.send_loop_dispatch as dispatch

    session = SimpleNamespace(
        config=SimpleNamespace(repo="/repo/wiki"),
        harness_session_id="plainsid",
        _session_job_ids=[],
        _register_local_job=MagicMock(),
        _finish_local_job=MagicMock(),
        _append_action_result=MagicMock(),
        _display_transcript=[],
    )

    def die(*_a, **_k):
        raise RuntimeError("thread died")

    monkeypatch.setattr(threading, "excepthook", lambda _args: None)
    monkeypatch.setattr(dispatch, "_non_git_workspace_error", lambda *_a, **_k: None)
    monkeypatch.setattr(dispatch, "stream_swarm", die)
    events = []
    runner = threading.Thread(target=lambda: events.extend(dispatch_swarm_action(
        session, PilotAction(kind="run_swarm", goal="audit keys", roles=["explore"]), "a-1", True,
        counters={"swarms": 0, "demo_swarms": 0}, turn_findings=[],
    )), daemon=True)
    runner.start()
    runner.join(timeout=20)

    assert not runner.is_alive()
    result = next(e for e in events if e.kind == "action_result")
    assert "ended without a result" in result.data["error"]
    session._finish_local_job.assert_called_once()
    assert session._finish_local_job.call_args.kwargs["status"] == "failed"
