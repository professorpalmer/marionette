from __future__ import annotations

"""Headless harness CLI: drive a task from the terminal, no browser.

  harness "Investigate auth across this repo and conclude"
  harness --driver glm-5.2 --budget 4 "Audit for the biggest risk"
  harness --image shot.png "What secret is in this screenshot?"
  harness --json "..."        # machine-readable event stream

Exit codes: 0 terminated cleanly (answer/stop), 1 error, 2 forced stop
(budget/turn cap). Keys load from the environment or HARNESS_KEY_FILE.
"""

import argparse
import json
import os
import sys

from .config import HarnessConfig
from .session import Session


# ANSI helpers (plain words, no emoji/pictographs per house style)
def _c(code, s):
    return f"\033[{code}m{s}\033[0m" if sys.stdout.isatty() else s


def _render(ev) -> None:
    d = ev.data or {}
    if ev.kind == "vision":
        if d.get("error"):
            print(_c("31", f"  vision error ({d.get('path','?')}): {d['error']}"))
        elif "chars" in d:
            print(_c("36", f"  vision: transcribed {d['chars']} chars via {d['model']}"))
        else:
            print(_c("36", f"  vision: transcribing {d.get('count','?')} image(s)..."))
    elif ev.kind == "intent":
        rep = f" (repaired x{d['repairs_used']})" if d.get("repairs_used") else ""
        if d["action"] == "run_swarm":
            print(_c("35", f"[turn {ev.turn}] run_swarm{rep}: ") + (d.get("goal") or ""))
        else:
            print(_c("34", f"[turn {ev.turn}] {d['action']}{rep}: ") + (d.get("rationale") or ""))
    elif ev.kind == "executing":
        print(_c("33", "  -> Puppetmaster executing: ") + (d.get("goal") or ""))
    elif ev.kind == "artifacts":
        print(_c("32", f"  <- {d['num']} artifacts [{', '.join(d.get('types', []))}] "
                       f"job {d['job_id']}"))
        for a in d.get("artifacts", [])[:6]:
            print(f"       [{a['type']}] {a['headline']}")
    elif ev.kind == "final":
        forced = " (forced)" if d.get("forced") else ""
        print(_c("32;1", f"FINAL [{d['action']}]{forced}: ") + (d.get("rationale") or ""))
    elif ev.kind == "error":
        print(_c("31;1", "ERROR: ") + (d.get("error") or ""))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="harness", description="PM-native harness (headless)")
    ap.add_argument("prompt", help="the task to drive")
    ap.add_argument("--driver", default=None, help="driver name (default from config/env)")
    ap.add_argument("--reach", default=None, choices=["openrouter", "native"])
    ap.add_argument("--budget", type=int, default=None, help="orchestration budget")
    ap.add_argument("--image", action="append", default=[], dest="images",
                    help="attach an image (repeatable); transcribed via the vision sidecar")
    ap.add_argument("--state-dir", default=None)
    ap.add_argument("--json", action="store_true", help="emit raw JSON events")
    args = ap.parse_args(argv)

    cfg = HarnessConfig.from_env()
    if args.driver: cfg.driver = args.driver
    if args.reach: cfg.reach = args.reach
    if args.budget is not None: cfg.budget = args.budget
    if args.state_dir: cfg.state_dir = args.state_dir

    try:
        session = Session(cfg)
    except Exception as e:
        print(_c("31;1", f"failed to build session: {e}"), file=sys.stderr)
        return 1

    if not args.json:
        print(_c("90", f"driver={cfg.driver} reach={cfg.reach} budget={cfg.budget}"))
        print(_c("90", f"task: {args.prompt}\n"))

    exit_code = 0
    final_action = None
    for ev in session.run(args.prompt, images=args.images or None):
        if args.json:
            print(json.dumps({"kind": ev.kind, "turn": ev.turn, "data": ev.data}))
        else:
            _render(ev)
        if ev.kind == "final":
            final_action = ev.data.get("action")
            if ev.data.get("forced"):
                exit_code = 2
        elif ev.kind == "error":
            exit_code = 1

    if exit_code == 0 and final_action is None:
        exit_code = 2
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
