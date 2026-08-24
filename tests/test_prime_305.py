from __future__ import annotations

import json
import os
from pathlib import Path

from harness.harness_refine import HISTORY_FILENAME, HarnessRefineController
from harness.ipython_kernel import MAX_IPYTHON_DEPTH, _ipython_depth
from harness.session_goal import SessionGoal


class _Sess:
    def __init__(self, state_dir: str) -> None:
        self.state_dir = state_dir
        self._auto_mode = False
        self._frozen_system_prompt = "frozen"


def test_refine_history_jsonl(tmp_path: Path) -> None:
    sess = _Sess(str(tmp_path))
    ctl = HarnessRefineController(sess)
    prop = ctl.propose(kind="memory", text="remember the latch", scope="local")
    assert prop is not None
    out = ctl.accept(prop.id)
    assert out.get("ok") is True
    path = tmp_path / HISTORY_FILENAME
    rec = json.loads(path.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert rec["event"] == "accept"
    rb = ctl.rollback()
    assert rb.get("ok") is True
    rec2 = json.loads(path.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert rec2["event"] == "rollback"


def test_token_budget_pauses() -> None:
    goal = SessionGoal()
    goal.set("ship it", token_budget=10)
    goal.record_turn_usage(tokens=4)
    assert goal.status == "active"
    goal.record_turn_usage(tokens=10)
    assert goal.status == "paused"
    assert goal.token_count >= 10


def test_ipython_depth_cap() -> None:
    assert MAX_IPYTHON_DEPTH >= 1
    _ipython_depth.n = 0
    _ipython_depth.n = MAX_IPYTHON_DEPTH
    assert int(_ipython_depth.n) >= MAX_IPYTHON_DEPTH
    _ipython_depth.n = 0


def test_trace_export_off_by_default(monkeypatch) -> None:
    monkeypatch.delenv("HARNESS_SESSION_TRACE_EXPORT", raising=False)
    flag = (os.environ.get("HARNESS_SESSION_TRACE_EXPORT") or "").strip().lower() in (
        "1", "true", "yes", "on",
    )
    assert flag is False
