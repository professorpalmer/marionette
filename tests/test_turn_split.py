"""A local pilot's long tool loop is compacted every N steps; cloud pilots never are."""
import json

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.turn_split import split_due
from tests.test_adaptive_steps import FakePilot


class LocalFakePilot(FakePilot):
    _spec = "local:loop/bonsai"


def _run(tmp_path, monkeypatch, pilot_cls, steps):
    monkeypatch.setenv("HARNESS_LOCAL_TURN_SPLIT_STEPS", "10")
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=str(tmp_path))
    cfg.repo = str(tmp_path)
    session = ConversationalSession(cfg)
    turns = [json.dumps({"say": f"Step {i}", "actions": [{"kind": "run_command", "command": f"echo {i}"}]})
             for i in range(steps)] + [json.dumps({"say": "Done.", "actions": []})]
    session.pilot = pilot_cls(turns)
    calls = []

    def record(force=False, emergency=False):
        calls.append((force, emergency))
        return iter(())

    monkeypatch.setattr(session, "_maybe_compact_history", record)
    events = list(session.send("go"))
    assert any(e.kind == "assistant_done" for e in events)
    return [c for c in calls if c[0]]


def test_local_pilot_compacts_every_n_steps(tmp_path, monkeypatch):
    assert _run(tmp_path, monkeypatch, LocalFakePilot, 25) == [(True, False), (True, False)]


def test_cloud_pilot_keeps_one_turn(tmp_path, monkeypatch):
    assert _run(tmp_path, monkeypatch, FakePilot, 25) == []


def test_split_due():
    assert [s for s in range(31) if split_due(s, 10)] == [10, 20, 30]
    assert not any(split_due(s, 0) for s in range(31))
