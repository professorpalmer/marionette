"""Count reasoning-budget force-closes per session from stream receipts.

A llama.cpp server with the budget report sets
``timings.reasoning_budget_exhausted`` on each response. Marionette stores it
per provider step in ``<state>/stream_performance/<session>.json``. A step
without the field is "unknown", not "not exhausted", so the rate divides by
reported steps only.

Usage: python scripts/reasoning_budget_report.py [STATE_DIR] [--json]
"""

from __future__ import annotations

import json
import os
import sys


def session_rows(state_dir: str) -> list:
    root = os.path.join(state_dir, "stream_performance")
    rows = []
    for name in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(root, name), encoding="utf-8") as fh:
                receipts = json.load(fh).get("receipts") or []
        except (OSError, ValueError, AttributeError):
            continue
        row = {"session_id": name[:-5], "steps": len(receipts), "reported": 0,
               "exhausted": 0, "reasoning_tokens": 0, "models": set()}
        for receipt in receipts:
            perf = receipt.get("stream_performance") or {}
            if not perf.get("reasoning_budget_reported_count"):
                continue
            row["reported"] += perf["reasoning_budget_reported_count"]
            row["exhausted"] += perf.get("reasoning_budget_exhausted_count", 0)
            row["reasoning_tokens"] += perf.get("reasoning_tokens", 0)
            row["models"].add(receipt.get("served_model") or receipt.get("model") or "")
        if row["reported"]:
            row["models"] = sorted(m for m in row["models"] if m)
            rows.append(row)
    return rows


def main(argv: list) -> int:
    args = [a for a in argv if a != "--json"]
    state_dir = args[0] if args else os.path.expanduser("~/.pmharness/state")
    rows = session_rows(state_dir)
    reported = sum(r["reported"] for r in rows)
    exhausted = sum(r["exhausted"] for r in rows)
    if "--json" in argv:
        print(json.dumps({"sessions": rows, "reported_steps": reported,
                          "exhausted_steps": exhausted}, indent=2))
        return 0
    for r in rows:
        print(f"{r['session_id']}  {r['exhausted']}/{r['reported']} steps force-closed"
              f"  ({r['steps']} steps total)  {', '.join(r['models'])}")
    rate = f"{exhausted / reported:.1%}" if reported else "n/a"
    print(f"total: {exhausted}/{reported} reported steps force-closed ({rate}),"
          f" {len(rows)} session(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
