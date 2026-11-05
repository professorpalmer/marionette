"""Hermetic bridge tests for Puppetmaster prewalk (plan-then-cheap).

No live Cursor / provider calls -- Orchestrator and adapter pick are mocked.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pmharness.bridge as bridge
from pmharness.intent import DriverIntent


@dataclass
class _FakeJob:
    id: str = "job_prewalk_test"
    status: str = "complete"


@dataclass
class _FakeResult:
    job: _FakeJob = field(default_factory=_FakeJob)
    status: str = "complete"
    mode: str = "subprocess"
    artifacts: list = field(default_factory=list)
    summary: str = "prewalk ok"


class _CapturingOrchestrator:
    last_goal: str = ""
    last_specs: Any = None
    last_worker_mode: Any = None
    last_label: Any = None

    def __init__(self, store: Any) -> None:
        self.store = store

    def run(self, goal: str, specs=None, worker_mode=None, label=None, **_kwargs):
        type(self).last_goal = goal
        type(self).last_specs = specs
        type(self).last_worker_mode = worker_mode
        type(self).last_label = label
        return _FakeResult()


def test_execute_prewalk_uses_build_prewalk_specs(monkeypatch, tmp_path):
    captured: dict = {}

    def fake_build_prewalk_specs(goal, cwd, **kwargs):
        captured["goal"] = goal
        captured["cwd"] = cwd
        captured["kwargs"] = kwargs

        @dataclass
        class _Spec:
            role: str
            instruction: str = ""
            adapter: str = "local"
            payload: dict = field(default_factory=dict)

        return [
            _Spec(role="plan", adapter="local", payload={"cwd": cwd, "mode": "analysis"}),
            _Spec(
                role="implement",
                adapter=kwargs.get("implement_adapter", "agentic"),
                payload={
                    "cwd": cwd,
                    "mode": "implement",
                    "prewalk": True,
                    "allow_dirty": kwargs.get("allow_dirty"),
                },
            ),
        ]

    monkeypatch.setattr(
        "puppetmaster.prewalk.build_prewalk_specs", fake_build_prewalk_specs
    )
    monkeypatch.setattr(
        "puppetmaster.orchestrator.Orchestrator", _CapturingOrchestrator
    )
    monkeypatch.setattr(
        bridge, "_resolve_prewalk_implement_adapter", lambda requested="": "agentic"
    )
    monkeypatch.setenv("HARNESS_ALLOW_DIRTY", "1")
    monkeypatch.setenv("HARNESS_ALLOW_NON_WORKTREE", "1")

    intent = DriverIntent(
        action="run_prewalk",
        goal="plan then implement a settings toggle",
    )
    result = bridge.execute_intent(
        intent,
        state_dir=str(tmp_path / "state"),
        cwd=str(tmp_path),
        worker_mode="subprocess",
    )

    assert result is not None
    assert result.job_id == "job_prewalk_test"
    assert result.adapter == "prewalk:agentic"
    assert captured["goal"] == "plan then implement a settings toggle"
    assert captured["cwd"] == str(tmp_path)
    assert captured["kwargs"]["allow_dirty"] is True
    assert captured["kwargs"]["allow_non_worktree"] is True
    assert captured["kwargs"]["implement_adapter"] == "agentic"
    assert _CapturingOrchestrator.last_goal == intent.goal
    assert _CapturingOrchestrator.last_worker_mode == "subprocess"
    specs = _CapturingOrchestrator.last_specs
    assert specs is not None and len(specs) == 2
    assert {s.role for s in specs} == {"plan", "implement"}
    implement = next(s for s in specs if s.role == "implement")
    assert implement.payload.get("prewalk") is True
    assert implement.payload.get("allow_dirty") is True


def test_execute_intent_answer_still_noop():
    assert bridge.execute_intent(DriverIntent(action="answer")) is None


def test_run_prewalk_requires_cwd(monkeypatch, tmp_path):
    monkeypatch.delenv("HARNESS_REPO", raising=False)
    monkeypatch.setattr(
        bridge, "_resolve_prewalk_implement_adapter", lambda requested="": "agentic"
    )
    intent = DriverIntent(action="run_prewalk", goal="prewalk the fix")
    try:
        bridge.execute_intent(intent, state_dir=str(tmp_path / "state"))
        assert False, "expected ValueError for missing cwd"
    except ValueError as exc:
        assert "cwd" in str(exc).lower() or "HARNESS_REPO" in str(exc)
