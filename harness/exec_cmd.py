"""`harness exec`: run one task through the normal pilot loop with no UI.

This is the turn a GUI user gets (same tools, prompts and compaction),
driven from a shell for benchmarks and scripts. It ends when the pilot ends
its turn. Commands run without approval, as in an interactive session.

An OpenAI-compatible endpoint can be given on the command line. It is saved
as an external endpoint and becomes the pilot (``local:<id>/<model>``).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Optional

from .reasoning_effort import (
    ENDPOINT_REASONING_EFFORT_ENV,
    REASONING_EFFORT_LEVELS,
    SWARM_REASONING_EFFORT_ENV,
)

POLL_SECONDS = 2.5  # the GUI's idle poll interval
WORKER_EFFORTS = ("low", "medium", "high", "xhigh")  # what PUPPETMASTER_WORKER_EFFORT accepts


def _parse(argv) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="harness exec", description=__doc__.splitlines()[0])
    ap.add_argument("task", nargs="?", help="the task text (or use --task-file)")
    ap.add_argument("--task-file", help="read the task text from this file")
    ap.add_argument("--repo", default=os.getcwd(), help="workspace directory (default: cwd)")
    ap.add_argument("--pilot", default=None, help="pilot spec (default: HARNESS_DRIVER)")
    ap.add_argument("--endpoint", default=None, help="OpenAI-compatible base URL to use as the pilot")
    ap.add_argument("--model", default=None, help="model id at --endpoint")
    ap.add_argument("--api-key-env", default="", help="environment variable that holds the --endpoint key")
    ap.add_argument("--context-window", type=int, default=None, help="the model's context window in tokens")
    ap.add_argument("--events", default=None, help="write every event as JSONL to this file")
    ap.add_argument("--reasoning-effort", default=None, choices=REASONING_EFFORT_LEVELS,
                    help="reasoning effort for the pilot and its workers; an --endpoint pilot "
                         "sends it as reasoning_effort on every call")
    return ap.parse_args(argv)


def _set_reasoning_effort(effort: str) -> None:
    """Apply one effort to the pilot, the endpoint wire and the workers."""
    os.environ["HARNESS_CODEX_REASONING_EFFORT"] = effort
    os.environ[ENDPOINT_REASONING_EFFORT_ENV] = effort
    os.environ[SWARM_REASONING_EFFORT_ENV] = effort
    if effort in WORKER_EFFORTS:
        os.environ["PUPPETMASTER_WORKER_EFFORT"] = effort


def _pin_workers(model: str) -> None:
    """Route every Puppetmaster worker to the endpoint model.

    Workers reach the endpoint as OpenRouter (the caller sets
    OPENROUTER_BASE_URL and OPENROUTER_API_KEY). The Models allowlist holds
    only this model, so the registry sync makes one agentic row and the
    router has no other model to pick.
    """
    from . import model_visibility

    if not os.environ.get("PUPPETMASTER_MODELS_PATH"):
        from .marionette_registry import marionette_models_path
        os.environ["PUPPETMASTER_MODELS_PATH"] = str(marionette_models_path().with_name("exec-models.json"))
    model_visibility.set_enabled(["openrouter:%s" % model])


def _endpoint_spec(url: str, model: str, key_env: str, context_window: Optional[int]) -> str:
    from .local_model_manager import get_manager

    if not model:
        raise SystemExit("--endpoint needs --model")
    _pin_workers(model)
    snapshot = get_manager().save_external(
        url,
        api_key=os.environ.get(key_env, "") if key_env else "",
        model=model,
        context_length=context_window,
        accept_lan=True,
        accept_remote=True,
    )
    saved = [e for e in snapshot.get("externals") or [] if e.get("selected_model") == model]
    if not saved:
        raise SystemExit("harness exec: the endpoint did not save: %s" % url)
    return "local:%s/%s" % (saved[-1]["id"], model)


def run_exec(argv) -> int:
    args = _parse(argv)
    task = args.task
    if args.task_file:
        with open(args.task_file, encoding="utf-8") as fh:
            task = fh.read()
    if not (task or "").strip():
        print("harness exec: no task text", file=sys.stderr)
        return 2
    # The per-turn tool budget and the step ceiling hand control back to a
    # person who can say "continue". A headless run has no person, so they
    # only cut the work short. The caller's time limit bounds the run.
    for name in ("HARNESS_PILOT_TOOL_BUDGET", "HARNESS_MAX_PILOT_STEPS"):
        os.environ.setdefault(name, "0")
    if args.context_window:
        os.environ["HARNESS_MAX_CONTEXT_TOKENS"] = str(args.context_window)
    if args.reasoning_effort:
        _set_reasoning_effort(args.reasoning_effort)

    from .config import HarnessConfig
    from .conversation import ConversationalSession

    cfg = HarnessConfig.from_env()
    cfg.repo = os.path.abspath(args.repo)
    if args.endpoint:
        cfg.driver = _endpoint_spec(args.endpoint, args.model, args.api_key_env, args.context_window)
    elif args.pilot:
        cfg.driver = args.pilot

    session = ConversationalSession(cfg)
    sink = open(args.events, "a", encoding="utf-8") if args.events else None
    state = {"failed": False}

    def consume(events) -> bool:
        """Print and log events. True when the pilot must resume."""
        resume = False
        for ev in events:
            if sink is not None:
                sink.write(json.dumps({"kind": ev.kind, "data": ev.data}, default=str) + "\n")
                sink.flush()
            if ev.kind == "message":
                text = (ev.data or {}).get("text") or ""
                if text:
                    print(text, flush=True)
            elif ev.kind == "error":
                state["failed"] = True
                print("error: %s" % ((ev.data or {}).get("error") or ev.data), file=sys.stderr, flush=True)
            elif ev.kind == "pilot_resume":
                resume = True
        return resume

    try:
        resume = consume(session.send(task))
        # Background workers finish after the turn that started them. The GUI
        # polls drain_swarm_results and resumes the pilot on pilot_resume. Do
        # the same until no work is left.
        while True:
            if resume:
                resume = consume(session.send("", resume=True))
                continue
            if not session.has_pending_swarms():
                resume = consume(session.drain_swarm_results())
                if not resume:
                    break
                continue
            time.sleep(POLL_SECONDS)
            resume = consume(session.drain_swarm_results())
    finally:
        if sink is not None:
            sink.close()
    return 1 if state["failed"] else 0
