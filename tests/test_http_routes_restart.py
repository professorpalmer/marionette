"""POST /api/restart must persist, stop MCP, signal Electron, then self-terminate."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import harness.api.session_control as sc_api
import harness.backend_restart_signal as restart_signal
import harness.http_routes as routes


def test_post_restart_stops_mcp_before_exit(monkeypatch, tmp_path):
    stopped: list[str] = []
    written: list[str] = []

    class FakeMcp:
        def stop_all(self):
            stopped.append("yes")

    mcp_svc = SimpleNamespace(mcp=FakeMcp())
    session_svc = SimpleNamespace()

    svc = SimpleNamespace(
        session_control_services=lambda: session_svc,
        diag=lambda *a, **k: None,
        mcp_services=lambda: mcp_svc,
    )

    monkeypatch.setattr(sc_api, "prepare_session_restart", lambda _svc: (True, None))
    monkeypatch.setattr(
        restart_signal,
        "write_intentional_restart_signal",
        lambda *a, **k: written.append(str(tmp_path / "backend-restart.json")) or written[-1],
    )
    # Avoid spawning a real self-terminate thread against the test process.
    monkeypatch.setattr(routes.threading, "Thread", lambda *a, **k: SimpleNamespace(start=lambda: None))

    routes._post_restart._svc = svc  # type: ignore[attr-defined]
    routes._post_restart._mcp = svc.mcp_services  # type: ignore[attr-defined]

    handler = MagicMock()

    routes._post_restart(handler, {})

    assert stopped == ["yes"]
    assert written, "must write intentional restart signal for Electron"
    handler._send.assert_called_once()
    status, body = handler._send.call_args[0]
    assert status == 200
    assert json.loads(body)["restarting"] is True


def test_write_intentional_restart_signal_is_fresh(tmp_path):
    path = restart_signal.write_intentional_restart_signal(str(tmp_path), pid=4242)
    raw = (tmp_path / "backend-restart.json").read_text(encoding="utf-8")
    payload = json.loads(raw)
    assert path.endswith("backend-restart.json")
    assert payload["pid"] == 4242
    assert payload["reason"] == "api_restart"
    assert isinstance(payload["at"], int) and payload["at"] > 0
