from __future__ import annotations

"""`harness eval`: run the driver-evaluation ladder from the one entrypoint, so
the research is reproducible by anyone with the repo.

  harness eval                      # offline oracle smoke (no keys)
  harness eval --driver glm-5.2 --reach openrouter   # one live driver
  harness eval --stage s4           # which ladder stage (v2|s4)
"""

import argparse
import sys
import tempfile


def run_eval(argv) -> int:
    ap = argparse.ArgumentParser(prog="harness eval",
        description="Run the driver-eval ladder")
    ap.add_argument("--driver", default="stub-oracle-v2",
                    help="driver to evaluate (default: offline oracle)")
    ap.add_argument("--reach", default="openrouter", choices=["openrouter", "native"])
    ap.add_argument("--stage", default="v2", choices=["v2", "s4"],
                    help="v2 = budget-aware battery, s4 = read-decide ranking traps")
    args = ap.parse_args(argv)

    from pmharness import registry as reg
    try:
        if args.driver in ("stub-oracle-v2",):
            driver = reg.build(args.driver)
        else:
            driver = reg.build(args.driver, reach=args.reach)
    except Exception as e:
        print(f"could not build driver {args.driver!r}: {e}", file=sys.stderr)
        return 1

    if args.stage == "v2":
        from pmharness.episode_v2 import EPISODES_V2
        from pmharness.episode_v2_runner import run_episode_v2
        from pmharness.scoring_v2 import score_v2
        episodes, runner, scorer = EPISODES_V2, run_episode_v2, score_v2
    else:
        from pmharness.episode_s4 import EPISODES_S4
        from pmharness.runner_s4 import run_episode_s4
        from pmharness.scoring_v2 import score_v2
        episodes, runner, scorer = EPISODES_S4, run_episode_s4, score_v2

    print(f"eval: driver={args.driver} stage={args.stage} ({len(episodes)} episodes)\n")
    scores = []
    for ep in episodes:
        traj = runner(driver, ep)
        s = scorer(ep, traj)
        scores.append(s)
        # an execute error (a refused or failed swarm) scores like a premature stop: show it
        err = getattr(traj, "error", "") or ""
        print(f"  {ep.id:22s} {s.score*100:5.1f}%  "
              f"term={s.terminated} act={s.got_terminal}/{s.expect_terminal} "
              f"swarms={s.swarms_run} prem={s.premature}"
              + (f"  error: {err[:160]}" if err else ""))
    mean = sum(s.score for s in scores) / len(scores) * 100 if scores else 0.0
    print(f"\nmean: {mean:.1f}%")
    return 0
