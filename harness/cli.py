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
        sub = "  [demo substrate -- not real codebase analysis]" if d.get("adapter") == "demo" else ""
        print(_c("32", f"  <- {d['num']} artifacts [{', '.join(d.get('types', []))}] "
                       f"job {d['job_id']}") + _c("33", sub))
        for a in d.get("artifacts", [])[:6]:
            print(f"       [{a['type']}] {a['headline']}")
    elif ev.kind == "final":
        forced = " (forced)" if d.get("forced") else ""
        print(_c("32;1", f"FINAL [{d['action']}]{forced}: ") + (d.get("rationale") or ""))
    elif ev.kind == "error":
        print(_c("31;1", "ERROR: ") + (d.get("error") or ""))


def _run_gui(argv) -> int:
    ap = argparse.ArgumentParser(prog="harness gui", description="Launch the harness GUI")
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--force", action="store_true", help="Bypass the single-backend reuse check")
    args = ap.parse_args(argv)
    from .server import serve
    try:
        serve(host=args.host, port=args.port, force=args.force)
    except KeyboardInterrupt:
        return 0
    return 0


def _run_auto(argv) -> int:
    """Fully-Auto (unattended) mode: pursue an objective under a budget governor.

    SAFETY: governor ceilings are ALWAYS on (tokens/time/swarms/stall) + a
    killswitch file. Real analysis on an unindexed repo is refused. Designed for
    overnight runs that cannot blow the budget or run blind.
    """
    import argparse
    from .config import HarnessConfig
    from .conversation import ConversationalSession
    from .autobudget import AutoBudget
    ap = argparse.ArgumentParser(prog="harness auto",
        description="Unattended autonomous run, bounded by a safety governor.")
    ap.add_argument("objective", help="what to pursue to completion")
    ap.add_argument("--repo", default=None)
    ap.add_argument("--swarm-adapter", default=None, choices=["demo", "openai"])
    ap.add_argument("--max-tokens", type=int, default=None)
    ap.add_argument("--max-seconds", type=int, default=None)
    ap.add_argument("--max-swarms", type=int, default=None)
    ap.add_argument("--killswitch", default=None, help="touch this file to stop")
    ap.add_argument("--allow-unindexed", action="store_true",
                    help="permit unattended analysis on a repo with no CodeGraph index")
    args = ap.parse_args(argv)

    cfg = HarnessConfig.from_env()
    if args.repo: cfg.repo = args.repo
    if args.swarm_adapter: cfg.swarm_adapter = args.swarm_adapter
    budget = AutoBudget.from_env()
    if args.max_tokens is not None: budget.max_tokens = args.max_tokens
    if args.max_seconds is not None: budget.max_seconds = args.max_seconds
    if args.max_swarms is not None: budget.max_swarms = args.max_swarms
    if args.killswitch: budget.killswitch_path = args.killswitch

    s = ConversationalSession(cfg)
    print(_c("36", f"AUTO: {args.objective}"))
    print(_c("90", f"governor: <= {budget.max_tokens} tok, {budget.max_seconds}s, "
                   f"{budget.max_swarms} swarms"
                   + (f", killswitch={budget.killswitch_path}" if budget.killswitch_path else "")))
    for ev in s.run_auto(args.objective, budget, require_codegraph=not args.allow_unindexed):
        if ev.kind == "message":
            print(_c("0", "  " + ev.data.get("text", "")))
        elif ev.kind == "action_start":
            print(_c("33", f"  -> swarm: {ev.data.get('goal','')[:80]}"))
        elif ev.kind == "action_result":
            n = ev.data.get("num", 0)
            print(_c("32", f"     {n} artifacts ({ev.data.get('adapter')})"))
        elif ev.kind == "auto_status":
            snap = ev.data.get("snapshot", {})
            print(_c("90", f"  [cycle {ev.data.get('cycle')}] "
                           f"{snap.get('swarms_used')}/{snap.get('max_swarms')} swarms, "
                           f"{snap.get('elapsed_s')}s"))
        elif ev.kind == "auto_halt":
            print(_c("35", f"  HALT: {ev.data.get('reason')}"))
            return 0
        elif ev.kind == "error":
            print(_c("31", f"  error: {ev.data.get('error','')}"))
    return 0


def main(argv=None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] in ("--version", "-V"):
        from . import __version__
        print(f"harness {__version__}")
        return 0
    # Subcommand dispatch. Default (no subcommand) = run a task.
    if raw and raw[0] == "pm-exec":
        from puppetmaster.cli import main as _pm_main
        return _pm_main(raw[1:])
    if raw and raw[0] == "gui":
        return _run_gui(raw[1:])
    if raw and raw[0] == "eval":
        from .eval_cmd import run_eval
        return run_eval(raw[1:])
    if raw and raw[0] == "doctor":
        from .doctor import run_doctor
        return run_doctor(raw[1:])
    if raw and raw[0] == "auto":
        return _run_auto(raw[1:])

    ap = argparse.ArgumentParser(prog="harness",
        description="PM-native harness. Run a task, or `harness gui` for the UI.")
    ap.add_argument("prompt", help="the task to drive")
    ap.add_argument("--driver", default=None, help="driver name (default from config/env)")
    ap.add_argument("--reach", default=None, choices=["openrouter", "native"])
    ap.add_argument("--budget", type=int, default=None, help="orchestration budget")
    ap.add_argument("--image", action="append", default=[], dest="images",
                    help="attach an image (repeatable); transcribed via the vision sidecar")
    ap.add_argument("--state-dir", default=None)
    ap.add_argument("--repo", default=None,
                    help="target repo for REAL read-only analysis (sets HARNESS_REPO)")
    ap.add_argument("--swarm-adapter", default=None, choices=["demo", "openai"],
                    help="demo (free/safe substrate) | openai (real read-only code analysis)")
    ap.add_argument("--json", action="store_true", help="emit raw JSON events")
    args = ap.parse_args(raw)

    cfg = HarnessConfig.from_env()
    if args.driver: cfg.driver = args.driver
    if args.reach: cfg.reach = args.reach
    if args.budget is not None: cfg.budget = args.budget
    if args.state_dir: cfg.state_dir = args.state_dir
    if args.repo: cfg.repo = args.repo
    if args.swarm_adapter: cfg.swarm_adapter = args.swarm_adapter

    try:
        session = Session(cfg)
    except Exception as e:
        print(_c("31;1", f"failed to build session: {e}"), file=sys.stderr)
        return 1

    pre = session.preflight()
    if pre:
        print(_c("31;1", pre), file=sys.stderr)
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
