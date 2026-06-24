#!/usr/bin/env python3
"""pm-harness eval CLI.

Offline (no keys):
  python scripts/run_eval.py --drivers stub-oracle

Whole open-weights field through OpenRouter (one key, OPENROUTER_API_KEY):
  python scripts/run_eval.py --drivers all --reach openrouter

Just the flagship tier, native endpoints (cost-accurate, per-provider keys):
  python scripts/run_eval.py --tier flagship --reach native

Per-provider native keys: ZAI_API_KEY, MOONSHOT_API_KEY, MINIMAX_API_KEY,
DEEPSEEK_API_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pmharness import registry as reg
from pmharness.ledger import Ledger
from pmharness.runner import run_driver, new_run_id


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--drivers", nargs="+", default=None,
                    help="driver names, or 'all' for the whole catalog + stub")
    ap.add_argument("--tier", default=None,
                    choices=["flagship", "value", "frontier_control"],
                    help="run every model in a tier")
    ap.add_argument("--reach", default="openrouter",
                    choices=["openrouter", "native"],
                    help="how to reach models (default openrouter: one key)")
    ap.add_argument("--ledger", default=str(Path(__file__).resolve().parents[1] / "results" / "ledger.sqlite"))
    ap.add_argument("--no-execute", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.tier:
        names = reg.model_names(args.tier)
    elif args.drivers == ["all"]:
        names = reg.all_driver_names()
    elif args.drivers:
        names = args.drivers
    else:
        names = ["stub-oracle"]

    ledger = Ledger(args.ledger)
    run_id = new_run_id()

    for name in names:
        reach = "openrouter" if name == "stub-oracle" else args.reach
        try:
            driver = reg.build(name, reach=reach)
        except Exception as e:
            print(f"skip {name}: {e}", file=sys.stderr)
            continue
        try:
            scores = run_driver(driver, ledger, run_id=run_id, execute=not args.no_execute)
        except Exception as e:
            print(f"driver {name} FAILED: {e!r}", file=sys.stderr)
            continue
        avg = round(sum(s.score for s in scores) / len(scores) * 100, 1) if scores else 0.0
        print(f"  {name:22s} mean_score={avg:5.1f}%  ({len(scores)} tasks, reach={reach})",
              file=sys.stderr)

    summary = ledger.summary(run_id)
    # attach native cost-per-run estimate from catalog pricing
    for r in summary:
        try:
            pin, pout = reg.price(r["model"])
            r["est_cost_usd"] = round(
                ((r.get("tin") or 0) / 1e6) * (pin or 0)
                + ((r.get("tout") or 0) / 1e6) * (pout or 0), 6)
        except Exception:
            r["est_cost_usd"] = None

    if args.json:
        print(json.dumps({"run_id": run_id, "reach": args.reach, "summary": summary}, indent=2))
    else:
        print(f"\nRun {run_id} (reach={args.reach})")
        hdr = (f"{'model':22s} {'score':>7s} {'json':>6s} {'schema':>7s} "
               f"{'action':>7s} {'tok_out':>8s} {'cost_usd':>9s} {'lat_ms':>7s}")
        print(hdr); print("-" * len(hdr))
        for r in summary:
            cost = r.get("est_cost_usd")
            cost_s = f"{cost:.5f}" if cost is not None else "n/a"
            print(f"{r['model']:22s} {r['avg_score']:>6.1f}% {r['json_pct']:>5.0f}% "
                  f"{r['schema_pct']:>6.0f}% {r['action_pct']:>6.0f}% "
                  f"{r['tout'] or 0:>8d} {cost_s:>9s} {r['avg_latency'] or 0:>7.0f}")
    ledger.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
