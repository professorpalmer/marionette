"""Daemon-owned pilot sessions: attach/detach without cancelling turns."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from harness.api.sessions import (
    get_sessions_runners,
    post_sessions_attach,
    post_sessions_detach,
)
from harness.session_runners import SessionRunnerRegistry


def _session_svc(runners: SessionRunnerRegistry, *, active: str = "sess-a"):
    sessions = MagicMock()
    sessions.active = active
    sessions.list.return_value = [
        {"id": "sess-a", "title": "Alpha"},
        {"id": "sess-b", "title": "Beta"},
    ]
    sessions.switch.return_value = {"ok": True, "active": "sess-b"}

    pilot = MagicMock()
    pilot.export_transcript_data.return_value = {"history": [], "display": []}

    return SimpleNamespace(
        sessions=sessions,
        runners=runners,
        cfg=SimpleNamespace(repo="/tmp/repo"),
        get_pilot=lambda: pilot,
        sessions_state_dir=lambda: "/tmp/state",
        save_active_transcript=lambda: None,
        attach_view=lambda sid, defer_cold_build=False: pilot,
        sync_pilot_session_id=lambda: None,
        attach_view_transcript_payload=lambda _p, sid: {"history": [], "display": []},
        lease_exhausted_body=lambda e: {"ok": False, "code": "lease_exhausted"},
        diag=lambda *a, **k: None,
        is_app_install_root=lambda _p: False,
    )


def test_detach_clears_active_view_but_keeps_runner():
    reg = SessionRunnerRegistry(max_concurrent_sessions=3)
    busy = MagicMock()
    busy.is_turn_busy = MagicMock(return_value=True)
    reg.get_or_create("sess-a", lambda: busy)
    reg.set_active_view("sess-a")
    svc = _session_svc(reg)
    code, payload = post_sessions_detach({"id": "sess-a"}, svc)
    assert code == 200
    assert payload["ok"] is True
    assert payload["was_active_view"] is True
    assert reg.active_view_id is None
    assert reg.get("sess-a") is busy


def test_runners_list_reports_daemon_tree():
    reg = SessionRunnerRegistry(max_concurrent_sessions=3)
    reg.get_or_create("sess-a", lambda: MagicMock())
    reg.set_active_view("sess-a")
    code, payload = get_sessions_runners(_session_svc(reg))
    assert code == 200
    assert payload["ok"] is True
    assert payload["active_view_id"] == "sess-a"
    assert payload["runners"][0]["session_id"] == "sess-a"


def test_attach_points_view_at_live_runner():
    reg = SessionRunnerRegistry(max_concurrent_sessions=3)
    reg.get_or_create("sess-b", lambda: MagicMock())
    svc = _session_svc(reg, active="sess-a")
    code, payload = post_sessions_attach({"id": "sess-b"}, svc)
    assert code == 200
    assert payload["ok"] is True
    assert payload["id"] == "sess-b"
    svc.sessions.switch.assert_called_once_with("sess-b")
