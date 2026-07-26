"""Bridge labels the execution adapter so demo substrate is never mistaken for
real codebase analysis. Demo requires HARNESS_ALLOW_DEMO_SWARM=1."""
import pytest
pytestmark = pytest.mark.swarm
import tempfile
from pmharness.intent import DriverIntent
from pmharness.bridge import execute_intent


def test_bridge_labels_demo_adapter(monkeypatch):
    monkeypatch.setenv("HARNESS_ALLOW_DEMO_SWARM", "1")
    monkeypatch.delenv("HARNESS_SWARM_ADAPTER", raising=False)
    monkeypatch.delenv("HARNESS_REPO", raising=False)
    intent = DriverIntent(action="run_swarm", goal="Investigate something", rationale="x")
    res = execute_intent(intent, state_dir=tempfile.mkdtemp())
    assert res is not None
    assert res.adapter == "demo"
    assert res.num_artifacts > 0


def test_session_artifacts_event_carries_adapter(monkeypatch):
    monkeypatch.setenv("HARNESS_ALLOW_DEMO_SWARM", "1")
    from harness.config import HarnessConfig
    from harness.session import Session
    cfg = HarnessConfig(
        driver="stub-oracle-v2",
        state_dir=tempfile.mkdtemp(),
        swarm_adapter="demo",
    )
    s = Session(cfg)
    events = list(s.run("Audit this repo for the biggest risk."))
    arts = [e for e in events if e.kind == "artifacts"]
    assert arts and arts[0].data.get("adapter") == "demo"
