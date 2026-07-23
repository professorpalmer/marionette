"""AutoBudget governor: every ceiling must provably HALT. These tests are the
safety proof -- they run BEFORE any autonomy depends on the governor."""
import os
import time
import tempfile
from harness.autobudget import AutoBudget


def test_proceeds_when_under_all_ceilings():
    b = AutoBudget(max_tokens=1000, max_seconds=100, max_swarms=10).start()
    assert b.check() is None


def test_token_ceiling_halts():
    b = AutoBudget(max_tokens=100).start()
    b.add_tokens(150)
    assert "token ceiling" in (b.check() or "")


def test_swarm_ceiling_halts():
    b = AutoBudget(max_swarms=2).start()
    b.add_swarm(); b.add_swarm()
    assert "swarm ceiling" in (b.check() or "")


def test_time_ceiling_halts():
    b = AutoBudget(max_seconds=0).start()
    time.sleep(0.01)
    assert "time ceiling" in (b.check() or "")


def test_killswitch_halts(tmp_path):
    ks = tmp_path / "STOP"
    b = AutoBudget(max_tokens=10**9, killswitch_path=str(ks)).start()
    assert b.check() is None       # not yet
    ks.write_text("stop")
    assert "killswitch" in (b.check() or "")


def test_stall_halts():
    b = AutoBudget(max_idle_steps=2).start()
    b.note_findings(0); assert b.check() is None
    b.note_findings(0); assert "stall" in (b.check() or "")


def test_findings_reset_idle():
    b = AutoBudget(max_idle_steps=2).start()
    b.note_findings(0)
    b.note_findings(3)   # progress -> reset
    assert b.idle_steps == 0
    assert b.check() is None


def test_halt_is_sticky():
    b = AutoBudget(max_tokens=10).start()
    b.add_tokens(20)
    r1 = b.check()
    b.tokens_used = 0   # even if counters reset, a halt stays halted
    assert b.check() == r1


def test_from_env(monkeypatch):
    monkeypatch.setenv("HARNESS_AUTO_MAX_TOKENS", "5000")
    monkeypatch.setenv("HARNESS_AUTO_MAX_SWARMS", "7")
    b = AutoBudget.from_env()
    assert b.max_tokens == 5000 and b.max_swarms == 7


def test_default_unattended_ceilings():
    b = AutoBudget()
    assert b.max_tokens == 500_000
    assert b.max_swarms == 20
    assert b.max_seconds == 3600


def test_from_env_defaults_match_dataclass(monkeypatch):
    monkeypatch.delenv("HARNESS_AUTO_MAX_TOKENS", raising=False)
    monkeypatch.delenv("HARNESS_AUTO_MAX_SWARMS", raising=False)
    monkeypatch.delenv("HARNESS_AUTO_MAX_SECONDS", raising=False)
    b = AutoBudget.from_env()
    assert b.max_tokens == 500_000
    assert b.max_swarms == 20
    assert b.max_seconds == 3600


def test_child_exhaustion_visible_to_parent():
    parent = AutoBudget(max_tokens=100).start()
    child = parent.child()
    child.add_tokens(150)
    assert parent.tokens_used == 150
    assert child.check() is not None
    assert parent.check() is not None


def test_child_shares_ceilings_and_tokens_roll_up():
    parent = AutoBudget(max_tokens=500, max_swarms=8, max_seconds=120).start()
    child = parent.child()
    assert child.max_tokens == parent.max_tokens
    assert child.max_swarms == parent.max_swarms
    assert child.max_seconds == parent.max_seconds
    child.add_tokens(75)
    assert child.tokens_used == 75
    assert parent.tokens_used == 75


def test_child_check_returns_parent_halt_reason():
    parent = AutoBudget(max_tokens=50).start()
    parent.add_tokens(60)
    parent_reason = parent.check()
    assert parent_reason is not None
    assert "token ceiling" in parent_reason
    child = parent.child()
    child_reason = child.check()
    assert child_reason is not None
    assert "token ceiling" in child_reason


def test_child_inherits_swarms_used_and_rolls_up():
    parent = AutoBudget(max_swarms=5).start()
    parent.add_swarm()
    parent.add_swarm()
    child = parent.child()
    assert child.swarms_used == 2
    child.add_swarm()
    assert child.swarms_used == 3
    assert parent.swarms_used == 3


def test_ambient_budget_child_sees_parent_spend():
    from harness.worker import ambient_budget, get_ambient_budget

    governor = AutoBudget(max_tokens=1000).start()
    governor.add_tokens(300)
    with ambient_budget(governor):
        assert get_ambient_budget() is governor
        child = governor.child()
        assert child.tokens_used == 300
        child.add_tokens(200)
    assert governor.tokens_used == 500


def test_snapshot_shape():
    b = AutoBudget(max_tokens=100).start()
    b.add_tokens(40); b.add_swarm()
    s = b.snapshot()
    assert s["tokens_used"] == 40 and s["swarms_used"] == 1 and s["halted"] is None
