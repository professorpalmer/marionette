"""Fully-Auto (run_auto): governor bounds it, codegraph-gate refuses unindexed
analysis, and it stops when the pilot is done. The autonomy safety proof.

Swarm EXECUTION is faked here (monkeypatched execute_intent) so these tests
prove the GOVERNOR LOOP deterministically and fast -- they are not a Puppetmaster
integration test. The real swarm path is covered by the offline E2E test.
"""
import tempfile
import pytest
from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.autobudget import AutoBudget
from pmharness.drivers.openai_compat import DriverResponse
from pmharness.bridge import BridgeResult


def _fake_result(n=1):
    return BridgeResult(
        job_id="job_fake", status="complete", mode="analysis",
        num_artifacts=n, artifact_types=["finding"], summary="fake",
        artifacts=[{"type": "finding", "headline": "fake finding"}] * n,
        adapter="local")


@pytest.fixture(autouse=True)
def _fast_swarm(monkeypatch):
    """Replace real Puppetmaster execution with an instant deterministic result."""
    fake = lambda intent, **kw: _fake_result(1)
    # send_loop owns the run_swarm call site after the mixin peel; patch both
    # bindings so either import path stays hermetic.
    monkeypatch.setattr("harness.send_loop.execute_intent", fake)
    monkeypatch.setattr("harness.conversation.execute_intent", fake)


class _NeverDonePilot:
    """Always fires another swarm -- to prove the GOVERNOR stops it, not the pilot."""
    name = "neverdone"
    def complete(self, prompt, *, system=None):
        return DriverResponse(
            text='{"say":"still digging","actions":[{"kind":"run_swarm","goal":"keep going"}]}',
            tokens_out=10, latency_ms=1.0)


class _DonePilot:
    """Fires one swarm then declares done -- to prove the loop stops cleanly."""
    name = "done"
    def __init__(self): self.n = 0
    def complete(self, prompt, *, system=None):
        self.n += 1
        if self.n == 1:
            t = '{"say":"checking","actions":[{"kind":"run_swarm","goal":"look"}]}'
        else:
            t = '{"say":"Objective met.","actions":[]}'
        return DriverResponse(text=t, tokens_out=10, latency_ms=1.0)


def test_auto_refuses_unindexed_analysis(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()  # no .codegraph
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp(),
                        repo=str(repo), swarm_adapter="openai")
    s = ConversationalSession(cfg)
    s.pilot = _DonePilot()
    events = list(s.run_auto("audit it", AutoBudget(max_swarms=5)))
    assert events[0].kind == "auto_halt"
    assert "no .codegraph index" in events[0].data["reason"]


def test_auto_governor_stops_neverending_pilot():
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    s = ConversationalSession(cfg)
    s.pilot = _NeverDonePilot()
    events = list(s.run_auto("dig forever", AutoBudget(max_swarms=3)))
    halts = [e for e in events if e.kind == "auto_halt"]
    assert halts, "governor must halt a never-ending pilot"
    assert "ceiling" in halts[-1].data["reason"] or "stall" in halts[-1].data["reason"]
    # and it must not have run unbounded
    assert sum(1 for e in events if e.kind == "action_result") <= 12


def test_auto_stops_when_pilot_done():
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    s = ConversationalSession(cfg)
    s.pilot = _DonePilot()
    # Prompt-size tokens_in estimates (~chars//4) dominate with a full PILOT_SYSTEM;
    # give headroom so the idle "objective met" halt wins over the token ceiling.
    events = list(s.run_auto(
        "quick check",
        AutoBudget(max_swarms=20, max_tokens=500_000),
    ))
    halts = [e for e in events if e.kind == "auto_halt"]
    assert halts and "objective met" in halts[-1].data["reason"]


class _BigTokenPilot:
    """Never finishes, reports large tokens_out each turn -> token ceiling must trip."""
    name = "bigtoken"
    def complete(self, prompt, *, system=None):
        return DriverResponse(
            text='{"say":"working","actions":[{"kind":"run_swarm","goal":"go"}]}',
            tokens_out=5000, latency_ms=1.0)


def test_auto_token_ceiling_trips():
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    s = ConversationalSession(cfg)
    s.pilot = _BigTokenPilot()
    # high swarm ceiling so TOKENS are what stops it, not swarms
    events = list(s.run_auto("dig", AutoBudget(max_swarms=999, max_tokens=8000)))
    halts = [e for e in events if e.kind == "auto_halt"]
    assert halts, "must halt"
    assert "token" in halts[-1].data["reason"].lower()


def test_auto_distill_on_completion(tmp_path, monkeypatch):
    """With HARNESS_AUTO_DISTILL=1, a finished auto run with >=2 findings yields a
    'distilled' event proposing PENDING candidates (still human-gated)."""
    monkeypatch.setenv("HARNESS_AUTO_DISTILL", "1")
    import harness.skill_store as sks, harness.rule_store as rks
    monkeypatch.setattr(sks, "SKILLS_DIR", tmp_path / "skills")
    monkeypatch.setattr(rks, "RULES_PATH", tmp_path / "rules.json")

    class _DistillPilot:
        name = "dp"
        def __init__(self): self.n = 0
        def complete(self, prompt, *, system=None):
            sysl = (system or "").lower()
            if "reusable skill" in sysl:
                return DriverResponse(text='{"name":"Found pattern","description":"d","body":"1. step"}', tokens_out=5, latency_ms=1.0)
            if "convention" in sysl:
                return DriverResponse(text='{"rules":[]}', tokens_out=5, latency_ms=1.0)
            self.n += 1
            if self.n <= 2:
                return DriverResponse(text='{"say":"x","actions":[{"kind":"run_swarm","goal":"g"}]}', tokens_out=5, latency_ms=1.0)
            return DriverResponse(text='{"say":"done","actions":[]}', tokens_out=5, latency_ms=1.0)

    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=str(tmp_path))
    s = ConversationalSession(cfg)
    s.pilot = _DistillPilot()
    events = list(s.run_auto("investigate", AutoBudget(max_swarms=20)))
    distilled = [e for e in events if e.kind == "distilled"]
    assert distilled, "expected a distilled event on completion"
    assert distilled[0].data.get("skill", {}).get("status") in ("proposed", "duplicate", "skipped")


def test_auto_no_distill_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_AUTO_DISTILL", "false")
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=str(tmp_path))
    s = ConversationalSession(cfg)
    s.pilot = _DonePilot()
    events = list(s.run_auto("x", AutoBudget(max_swarms=20)))
    assert not [e for e in events if e.kind == "distilled"]


def test_auto_cancel_halts(tmp_path):
    """A cancel signal raised mid-run makes run_auto halt promptly (client
    disconnect). run_auto clears the flag at start, so we set it during the run."""
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=str(tmp_path))
    s = ConversationalSession(cfg)

    class _CancelOnFirstSwarm:
        name = "c"
        def __init__(self, sess): self.sess = sess; self.n = 0
        def complete(self, prompt, *, system=None):
            self.n += 1
            if self.n == 1:
                return DriverResponse(text='{"say":"x","actions":[{"kind":"run_swarm","goal":"g"}]}', tokens_out=5, latency_ms=1.0)
            # cancel arrives (simulating explicit Stop / interrupt) before the next turn
            self.sess.cancel()
            return DriverResponse(text='{"say":"more","actions":[{"kind":"run_swarm","goal":"g2"}]}', tokens_out=5, latency_ms=1.0)

    s.pilot = _CancelOnFirstSwarm(s)
    events = list(s.run_auto("x", AutoBudget(max_swarms=999, max_tokens=10_000_000)))
    halts = [e for e in events if e.kind == "auto_halt"]
    assert halts and "cancel" in halts[-1].data["reason"].lower()


def test_send_rejects_concurrent(tmp_path):
    """A second send() while one is in flight is rejected, not interleaved."""
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=str(tmp_path))
    s = ConversationalSession(cfg)
    s.pilot = _DonePilot()
    s._busy.acquire()  # simulate an in-flight request holding the lock
    try:
        events = list(s.send("hello"))
        errs = [e for e in events if e.kind == "error" and "busy" in e.data.get("error", "")]
        assert errs, "concurrent send should be rejected with a busy error"
    finally:
        s._busy.release()


def test_auto_killswitch(tmp_path):
    ks = tmp_path / "STOP"; ks.write_text("x")  # pre-tripped
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    s = ConversationalSession(cfg)
    s.pilot = _NeverDonePilot()
    events = list(s.run_auto("go", AutoBudget(max_swarms=99, killswitch_path=str(ks))))
    halts = [e for e in events if e.kind == "auto_halt"]
    assert halts and "killswitch" in halts[-1].data["reason"]
