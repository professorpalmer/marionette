"""Stage 3.5 E2E: budget-aware oracle drives real Puppetmaster, substantive
substrate feedback, sharper scoring with budget/premature penalties. Offline."""
import pytest
pytestmark = pytest.mark.swarm

from pmharness.registry import build
from pmharness.episode_v2 import EPISODES_V2
from pmharness.episode_v2_runner import run_episode_v2, HARD_TURN_CAP
from pmharness.scoring_v2 import score_v2


def test_investigate_episode_grounds_and_respects_budget():
    drv = build("stub-oracle-v2")
    ep = [e for e in EPISODES_V2 if e.id == "invest_vague"][0]
    traj = run_episode_v2(drv, ep)
    assert traj.terminated and traj.terminal_action == "stop"
    assert traj.swarms_run == 1 and not traj.over_budget
    # real PM executed at least one swarm
    assert any(t.executed and t.executed.num_artifacts > 0 for t in traj.turns)
    s = score_v2(ep, traj)
    assert s.grounded is True and s.within_budget and not s.premature
    assert s.score == 1.0


def test_trivial_zero_swarms_under_budget1():
    drv = build("stub-oracle-v2")
    ep = [e for e in EPISODES_V2 if e.id == "trivial_explicit"][0]
    traj = run_episode_v2(drv, ep)
    assert traj.terminal_action == "answer" and traj.swarms_run == 0


def test_budget_overrun_is_penalized():
    """A synthetic over-budget trajectory must score below a clean one."""
    from pmharness.episode_v2_runner import TrajectoryV2, TurnV2
    ep = [e for e in EPISODES_V2 if e.id == "invest_vague"][0]  # budget 3, max_swarms 2
    # fake a trajectory that ran 5 swarms and never terminated
    over = TrajectoryV2(ep.id, ep.scenario, ep.variant, "fake",
                        turns=[], terminated=False, terminal_action=None,
                        swarms_run=5, budget=ep.budget, over_budget=True,
                        total_tokens_out=0, total_latency_ms=0.0)
    s = score_v2(ep, over)
    assert not s.within_budget and not s.terminated
    assert s.score < 0.5


def test_premature_stop_flagged():
    """Stopping with zero swarms on an investigate episode is premature."""
    from pmharness.episode_v2_runner import TrajectoryV2
    ep = [e for e in EPISODES_V2 if e.id == "invest_vague"][0]  # min_swarms 1
    early = TrajectoryV2(ep.id, ep.scenario, ep.variant, "fake",
                         turns=[], terminated=True, terminal_action="stop",
                         swarms_run=0, budget=ep.budget, over_budget=False,
                         total_tokens_out=0, total_latency_ms=0.0)
    s = score_v2(ep, early)
    assert s.premature is True
    assert s.score < 1.0


def test_hard_turn_cap_respected():
    drv = build("stub-oracle-v2")
    for ep in EPISODES_V2:
        traj = run_episode_v2(drv, ep)
        assert len(traj.turns) <= HARD_TURN_CAP
