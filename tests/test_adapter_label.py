"""Bridge labels the execution adapter so demo substrate is never mistaken for
real codebase analysis. Drives real Puppetmaster (local demo adapter)."""
import tempfile
from pmharness.intent import DriverIntent
from pmharness.bridge import execute_intent


def test_bridge_labels_demo_adapter():
    intent = DriverIntent(action="run_swarm", goal="Investigate something", rationale="x")
    res = execute_intent(intent, state_dir=tempfile.mkdtemp())
    assert res is not None
    assert res.adapter == "demo"  # default role path = local demo substrate
    assert res.num_artifacts > 0


def test_session_artifacts_event_carries_adapter():
    from harness.config import HarnessConfig
    from harness.session import Session
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    s = Session(cfg)
    events = list(s.run("Audit this repo for the biggest risk."))
    arts = [e for e in events if e.kind == "artifacts"]
    assert arts and arts[0].data.get("adapter") == "demo"
