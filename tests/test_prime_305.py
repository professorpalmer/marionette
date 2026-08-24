"""v0.9.305: refine slash/history, SessionGoal budget pause, ipython depth, opt-in trace."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from harness.harness_refine import HISTORY_FILENAME, HarnessRefineController, get_refine_controller
from harness.ipython_kernel import MAX_IPYTHON_DEPTH, _ipython_depth
from harness.pilot import PilotAction
from harness.session_goal import SessionGoal
from harness.session_trace import (
    TRACE_FILENAME,
    export_session_trace,
    session_trace_export_enabled,
    session_trace_upload_enabled,
)
from harness.tool_dispatch import ToolDispatchMixin


class _Sess:
    def __init__(self, state_dir: str) -> None:
        self.state_dir = state_dir
        self._auto_mode = False
        self._frozen_system_prompt = "frozen"
        self.session_id = "s-test"
        self._history = []
        self._tokens_used = 0
        self._last_turn_tokens = 0


def test_refine_slash_proposes_on_existing_controller(tmp_path: Path) -> None:
    sess = _Sess(str(tmp_path))
    ctl = get_refine_controller(sess)
    listed = ctl.handle_slash("/refine")
    assert listed["ok"] is True
    assert listed["history"] == []
    proposed = ctl.handle_slash("/refine memory local latch the door")
    assert proposed["ok"] is True
    assert proposed["proposed"]["kind"] == "memory"
    assert proposed["proposed"]["scope"] == "local"
    assert "latch" in proposed["proposed"]["text"]
    assert (tmp_path / HISTORY_FILENAME).exists() is False
    accepted = ctl.accept(proposed["proposed"]["id"])
    assert accepted["ok"] is True
    path = tmp_path / HISTORY_FILENAME
    rec = json.loads(path.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert rec["event"] == "accept"
    assert rec["text"] == "latch the door"


def test_refine_history_jsonl_append_only(tmp_path: Path) -> None:
    sess = _Sess(str(tmp_path))
    ctl = HarnessRefineController(sess)
    prop = ctl.propose(kind="memory", text="remember the latch")
    assert prop is not None
    assert ctl.accept(prop.id).get("ok") is True
    path = tmp_path / HISTORY_FILENAME
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[-1])
    assert rec["event"] == "accept"
    rb = ctl.rollback()
    assert rb.get("ok") is True
    rec2 = json.loads(path.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert rec2["event"] == "rollback"
    assert len(path.read_text(encoding="utf-8").strip().splitlines()) == 2


def test_token_budget_pauses_and_stops_continuation() -> None:
    goal = SessionGoal()
    goal.set("ship it", token_budget=10)
    goal.record_turn_usage(tokens=4)
    assert goal.status == "active"
    assert goal.budget_exceeded is False
    goal.record_turn_usage(tokens=10)
    assert goal.status == "paused"
    assert goal.budget_exceeded is True
    assert goal.token_count >= 10
    assert not goal.is_active()
    d = goal.to_dict()
    assert d["budget_exceeded"] is True
    restored = SessionGoal.from_dict(d)
    assert restored.budget_exceeded is True
    restored.set("new goal", token_budget=20)
    assert restored.budget_exceeded is False
    assert restored.status == "active"


def test_ipython_depth_guard_no_new_kernel(tmp_path: Path) -> None:
    assert MAX_IPYTHON_DEPTH >= 1
    session = SimpleNamespace(
        config=SimpleNamespace(repo=str(tmp_path), state_dir=str(tmp_path)),
        _ipython_kernel=None,
    )
    _ipython_depth.n = MAX_IPYTHON_DEPTH
    try:
        act = PilotAction(kind="run_ipython", content="1 + 1")
        ok, status, val = ToolDispatchMixin._do_run_ipython(session, act)
        assert ok is False
        assert status == "error"
        assert "depth" in str(val).lower()
        assert session._ipython_kernel is None
    finally:
        _ipython_depth.n = 0


def test_trace_export_off_by_default(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("HARNESS_SESSION_TRACE_EXPORT", raising=False)
    monkeypatch.delenv("HARNESS_SESSION_TRACE_UPLOAD", raising=False)
    assert session_trace_export_enabled() is False
    assert session_trace_upload_enabled() is False
    sess = _Sess(str(tmp_path))
    assert export_session_trace(sess) is None
    assert not (tmp_path / TRACE_FILENAME).exists()


def test_trace_export_opt_in_no_upload(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HARNESS_SESSION_TRACE_EXPORT", "1")
    monkeypatch.delenv("HARNESS_SESSION_TRACE_UPLOAD", raising=False)
    sess = _Sess(str(tmp_path))
    sess._session_goal = SessionGoal()
    sess._session_goal.set("trace me", token_budget=5)
    out = export_session_trace(sess)
    assert out and out["ok"] is True
    assert out["uploaded"] is False
    data = json.loads((tmp_path / TRACE_FILENAME).read_text(encoding="utf-8"))
    assert data["goal"]["text"] == "trace me"
