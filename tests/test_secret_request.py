from types import SimpleNamespace
from unittest.mock import MagicMock

from harness.pilot import PilotAction
from harness.send_loop_phases import LOCAL_ACTION_KINDS, dispatch_local_action


def test_request_secret_is_local_kind():
    assert "request_secret" in LOCAL_ACTION_KINDS


def test_dispatch_request_secret_emits_card_and_not_secret(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    act = PilotAction(
        kind="request_secret",
        arguments={
            "label": "PyPI token for puppetmaster-ai",
            "connector": "pypi",
            "field": "token",
            "description": "Project-scoped token",
        },
    )
    session = SimpleNamespace(
        harness_session_id="sess-a",
        _pending_secret_requests={},
        _display_transcript=[],
        _history=[],
        _append_action_result=MagicMock(),
    )
    events = list(dispatch_local_action(session, act, "a1", True, []))
    kinds = [ev.kind for ev in events]
    assert "secret_request" in kinds
    data = events[0].data
    assert data["connector"] == "pypi"
    assert data["ends_turn"] is True
    assert "value" not in data
    dumped = str(events)
    assert "pypi-" not in dumped
    assert session._display_transcript[0]["type"] == "secret_request"
    assert "value" not in session._display_transcript[0]


def test_dismiss_does_not_reask_same_breath(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    from harness.secret_request import decide_secret_request, declined_this_breath
    session = SimpleNamespace(
        harness_session_id="sess-a",
        _pending_secret_requests={},
        _display_transcript=[],
        _history=[],
    )
    decide_secret_request(session, connector="pypi", field="token", provided=False)
    assert declined_this_breath(session, "pypi", "token") is True
    act = PilotAction(
        kind="request_secret",
        arguments={
            "label": "PyPI token for puppetmaster-ai",
            "connector": "pypi",
            "field": "token",
        },
    )
    session._append_action_result = MagicMock()
    events = list(dispatch_local_action(session, act, "a2", True, []))
    kinds = [ev.kind for ev in events]
    assert "secret_request" not in kinds
    assert events[-1].kind == "action_result"
