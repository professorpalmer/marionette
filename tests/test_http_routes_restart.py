"""POST /api/restart must persist, stop MCP, signal Electron, then self-terminate."""
from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import harness.api.session_control as sc_api
import harness.backend_restart_signal as restart_signal
import harness.http_routes as routes


def _restart_svc(tmp_path):
    class FakeMcp:
        def __init__(self):
            self.stopped = []

        def stop_all(self):
            self.stopped.append("yes")

    mcp = FakeMcp()
    mcp_svc = SimpleNamespace(mcp=mcp)
    session_svc = SimpleNamespace()
    svc = SimpleNamespace(
        session_control_services=lambda: session_svc,
        diag=lambda *a, **k: None,
        mcp_services=lambda: mcp_svc,
    )
    return svc, mcp


def test_post_restart_stops_mcp_before_exit(monkeypatch, tmp_path):
    written: list[str] = []
    svc, mcp = _restart_svc(tmp_path)

    monkeypatch.setattr(sc_api, "prepare_session_restart", lambda _svc: (True, None))
    monkeypatch.setattr(
        restart_signal,
        "write_intentional_restart_signal",
        lambda *a, **k: written.append(str(tmp_path / "backend-restart.json")) or written[-1],
    )
    # Avoid spawning a real self-terminate thread against the test process.
    monkeypatch.setattr(routes.threading, "Thread", lambda *a, **k: SimpleNamespace(start=lambda: None))

    handler = MagicMock()

    routes._post_restart(handler, {}, svc, svc.mcp_services)

    assert mcp.stopped == ["yes"]
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


def test_write_read_restart_outcome_roundtrip(tmp_path):
    path = restart_signal.write_restart_outcome(
        str(tmp_path),
        requested_at=1_700_000_000_000,
        requested_pid=99,
        prepared_ok=True,
        outcome="ok",
    )
    assert path.endswith(restart_signal.OUTCOME_NAME)
    payload = restart_signal.read_restart_outcome(str(tmp_path))
    assert payload == {
        "requested_at": 1_700_000_000_000,
        "requested_pid": 99,
        "prepared_ok": True,
        "outcome": "ok",
        "error": "",
    }


def test_read_restart_outcome_missing_or_corrupt(tmp_path):
    assert restart_signal.read_restart_outcome(str(tmp_path)) is None
    (tmp_path / restart_signal.OUTCOME_NAME).write_text("not-json", encoding="utf-8")
    assert restart_signal.read_restart_outcome(str(tmp_path)) is None
    (tmp_path / restart_signal.OUTCOME_NAME).write_text("[]", encoding="utf-8")
    assert restart_signal.read_restart_outcome(str(tmp_path)) is None


def test_record_boot_restart_outcome_writes_ok_and_leaves_signal(tmp_path):
    restart_signal.write_intentional_restart_signal(str(tmp_path), pid=7)
    path = restart_signal.record_boot_restart_outcome(str(tmp_path))
    assert path and path.endswith(restart_signal.OUTCOME_NAME)
    assert (tmp_path / restart_signal.SIGNAL_NAME).is_file()
    outcome = restart_signal.read_restart_outcome(str(tmp_path))
    signal = restart_signal.read_intentional_restart_signal(str(tmp_path))
    assert outcome["outcome"] == "ok"
    assert outcome["prepared_ok"] is True
    assert outcome["requested_at"] == signal["at"]
    assert outcome["requested_pid"] == 7
    assert outcome["error"] == ""


def test_record_boot_restart_outcome_noop_without_signal(tmp_path):
    assert restart_signal.record_boot_restart_outcome(str(tmp_path)) is None
    assert restart_signal.read_restart_outcome(str(tmp_path)) is None


def test_record_boot_restart_outcome_stale_signal_noop(tmp_path):
    restart_signal.write_intentional_restart_signal(str(tmp_path), pid=3)
    stale = restart_signal.read_intentional_restart_signal(str(tmp_path))
    stale["at"] = int(time.time() * 1000) - (restart_signal.SIGNAL_MAX_AGE_MS + 5_000)
    (tmp_path / restart_signal.SIGNAL_NAME).write_text(json.dumps(stale), encoding="utf-8")
    assert restart_signal.record_boot_restart_outcome(str(tmp_path)) is None
    assert restart_signal.read_restart_outcome(str(tmp_path)) is None


def test_record_boot_restart_outcome_idempotent(tmp_path):
    restart_signal.write_intentional_restart_signal(str(tmp_path), pid=5)
    restart_signal.record_boot_restart_outcome(str(tmp_path))
    first = restart_signal.read_restart_outcome(str(tmp_path))
    (tmp_path / restart_signal.OUTCOME_NAME).write_text(
        json.dumps({**first, "error": "must-not-overwrite"}),
        encoding="utf-8",
    )
    restart_signal.record_boot_restart_outcome(str(tmp_path))
    second = restart_signal.read_restart_outcome(str(tmp_path))
    assert second["error"] == "must-not-overwrite"
    assert second["outcome"] == "ok"
    assert second["requested_at"] == first["requested_at"]


def test_record_boot_restart_outcome_upgrades_pending(tmp_path):
    restart_signal.write_intentional_restart_signal(str(tmp_path), pid=8)
    signal = restart_signal.read_intentional_restart_signal(str(tmp_path))
    restart_signal.write_restart_outcome(
        str(tmp_path),
        requested_at=signal["at"],
        requested_pid=signal["pid"],
        prepared_ok=True,
        outcome="pending",
    )
    restart_signal.record_boot_restart_outcome(str(tmp_path))
    outcome = restart_signal.read_restart_outcome(str(tmp_path))
    assert outcome["outcome"] == "ok"
    assert outcome["requested_at"] == signal["at"]


def test_post_restart_prepare_fail_writes_failed_outcome(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    svc, mcp = _restart_svc(tmp_path)
    started: list[int] = []

    monkeypatch.setattr(sc_api, "prepare_session_restart", lambda _svc: (False, "disk full"))
    monkeypatch.setattr(
        routes.threading,
        "Thread",
        lambda *a, **k: started.append(1) or SimpleNamespace(start=lambda: None),
    )

    handler = MagicMock()
    routes._post_restart(handler, {}, svc, svc.mcp_services)

    assert mcp.stopped == []
    assert started == []
    handler._send.assert_called_once()
    status, body = handler._send.call_args[0]
    assert status == 500
    payload = json.loads(body)
    assert payload["ok"] is False
    assert payload["error"] == "disk full"
    assert not (tmp_path / restart_signal.SIGNAL_NAME).exists()
    outcome = restart_signal.read_restart_outcome(str(tmp_path))
    assert outcome is not None
    assert outcome["outcome"] == "failed"
    assert outcome["prepared_ok"] is False
    assert outcome["error"] == "disk full"


def test_post_restart_success_writes_pending_outcome(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    svc, mcp = _restart_svc(tmp_path)

    monkeypatch.setattr(sc_api, "prepare_session_restart", lambda _svc: (True, None))
    monkeypatch.setattr(routes.threading, "Thread", lambda *a, **k: SimpleNamespace(start=lambda: None))

    handler = MagicMock()
    routes._post_restart(handler, {}, svc, svc.mcp_services)

    assert mcp.stopped == ["yes"]
    signal = restart_signal.read_intentional_restart_signal(str(tmp_path))
    assert signal is not None
    outcome = restart_signal.read_restart_outcome(str(tmp_path))
    assert outcome["outcome"] == "pending"
    assert outcome["prepared_ok"] is True
    assert outcome["requested_at"] == signal["at"]
    assert outcome["requested_pid"] == signal["pid"]


def test_get_restart_last_reads_outcome(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    status, empty = restart_signal.get_restart_last()
    assert status == 200
    assert empty == {"ok": True, "restart_outcome": None}
    restart_signal.write_restart_outcome(
        str(tmp_path),
        requested_at=111,
        requested_pid=9,
        prepared_ok=True,
        outcome="ok",
    )
    status, payload = restart_signal.get_restart_last()
    assert status == 200
    assert payload["ok"] is True
    assert payload["restart_outcome"]["outcome"] == "ok"
    assert payload["restart_outcome"]["requested_at"] == 111


def test_no_rebuild_and_restart_pilot_tool():
    from harness.pilot import VALID_ACTION_KINDS, build_tools_schema

    assert "rebuild_and_restart" not in VALID_ACTION_KINDS
    names = {item["function"]["name"] for item in build_tools_schema()}
    assert "rebuild_and_restart" not in names
