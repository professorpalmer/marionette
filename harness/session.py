from __future__ import annotations

"""Session: the PM-native driver loop, productized.

A user types a prompt. The Session drives the configured open-weights model to
emit a DriverIntent, executes run_swarm intents against the real in-process
Puppetmaster Orchestrator, feeds the REAL resulting artifacts back as the next
turn, respects a budget, and terminates on answer/stop. Every turn is yielded as
a structured event so a GUI (or CLI) can render the loop live.

This reuses the validated primitives from the research package (intent, bridge,
registry) -- it is the productization of what Stage 1-3.5 proved, not a rewrite.
"""

import tempfile
import time
from dataclasses import dataclass, field
from typing import Iterator, Optional

from pmharness.intent import validate_intent, parse_intent_text, IntentError, DriverIntent
from pmharness.bridge import execute_intent, BridgeResult
from pmharness import registry as reg
from pmharness.drivers.base import SYSTEM_PROMPT
from .repair import drive_with_repair

from .config import HarnessConfig
from .state import DurableState


HARD_TURN_CAP = 8


def _system(budget: int) -> str:
    return SYSTEM_PROMPT + f"""

You drive a multi-turn loop with an orchestration budget of {budget} swarm
step(s). Each run_swarm consumes one step; afterward you see its real artifacts.
Stop (with a grounded rationale) once the objective is met. Answer trivia
directly with zero swarms. Never loop the same swarm. Emit ONLY the JSON object."""


@dataclass
class SessionEvent:
    kind: str                  # "intent" | "executing" | "artifacts" | "final" | "error"
    turn: int
    data: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)


@dataclass
class SessionResult:
    terminal_action: Optional[str]
    answer: Optional[str]
    turns: int
    swarms_run: int
    jobs: list
    tokens_out: int
    error: str = ""


class Session:
    """One conversational task driven to termination. Stateless across tasks;
    durable state lives in Puppetmaster's store (read via DurableState)."""

    def __init__(self, config: Optional[HarnessConfig] = None) -> None:
        self.config = config or HarnessConfig.from_env()
        self.state_dir = self.config.state_dir or tempfile.mkdtemp(prefix="harness-")
        self.driver = reg.build(self.config.driver, reach=self.config.reach)

    def state(self) -> DurableState:
        return DurableState(self.state_dir)

    def run(self, prompt: str, images: Optional[list] = None) -> Iterator[SessionEvent]:
        """Drive the loop, yielding an event per step. The GUI consumes this.

        If images are supplied, the vision sidecar transcribes them to text once
        and prepends the transcription so a text-only driver can reason over the
        image content. The driver never receives pixels."""
        budget = self.config.budget
        system = _system(budget)
        context = prompt
        swarms = 0
        tok = 0
        jobs: list = []

        if images:
            from .vision import transcribe_images
            yield SessionEvent("vision", -1, {"count": len(images), "status": "transcribing"})
            results = transcribe_images(images)
            blocks = []
            for path, r in zip(images, results):
                if r.error:
                    yield SessionEvent("vision", -1, {"path": path, "error": r.error})
                else:
                    blocks.append(f"[Image: {path}]\n{r.text}")
                    yield SessionEvent("vision", -1, {"path": path,
                        "chars": len(r.text), "model": r.model,
                        "preview": r.text[:200]})
            if blocks:
                context = ("The user attached image(s). Transcription(s) below "
                           "(you cannot see the image, only this text):\n\n"
                           + "\n\n".join(blocks) + "\n\n---\n" + prompt)

        for i in range(HARD_TURN_CAP):
            intent, resp, repairs = drive_with_repair(self.driver, context, system)
            tok += resp.tokens_out
            if intent is None:
                yield SessionEvent("error", i, {"error": resp.error or "invalid intent",
                                                "raw": resp.text[:300],
                                                "repairs_used": repairs})
                return

            yield SessionEvent("intent", i, {
                "action": intent.action, "goal": intent.goal,
                "roles": intent.roles, "rationale": intent.rationale,
                "tokens_out": resp.tokens_out, "latency_ms": resp.latency_ms,
                "repairs_used": repairs,
            })

            if intent.action == "answer":
                yield SessionEvent("final", i, {"action": "answer",
                                                "rationale": intent.rationale})
                return
            if intent.action == "stop":
                yield SessionEvent("final", i, {"action": "stop",
                                                "rationale": intent.rationale})
                return

            # run_swarm: budget check, then execute against REAL Puppetmaster
            if swarms >= budget:
                yield SessionEvent("final", i, {"action": "stop",
                    "rationale": "budget exhausted", "forced": True})
                return

            yield SessionEvent("executing", i, {"goal": intent.goal})
            try:
                result: BridgeResult = execute_intent(intent, state_dir=self.state_dir,
                                                      worker_mode=self.config.worker_mode)
            except Exception as e:
                yield SessionEvent("error", i, {"error": f"execute: {e}"})
                return
            swarms += 1
            jobs.append(result.job_id)

            yield SessionEvent("artifacts", i, {
                "job_id": result.job_id, "num": result.num_artifacts,
                "types": result.artifact_types,
                "artifacts": result.artifacts[:8], "mode": result.mode,
            })

            # Feed REAL artifacts back (product loop -- no eval fixtures here)
            steps_left = budget - swarms
            digest = "\n".join(f"  - [{a['type']}] {a['headline']}"
                               for a in result.artifacts[:8])
            context = (
                f"{prompt}\n\nPuppetmaster ran your swarm (job {result.job_id}, "
                f"{result.num_artifacts} artifacts):\n{digest}\n\n"
                f"Budget remaining: {steps_left} step(s). Stop with a grounded "
                f"rationale if the objective is met, else run_swarm with a "
                f"narrowed goal."
            )

        yield SessionEvent("final", HARD_TURN_CAP,
                           {"action": "stop", "rationale": "turn cap reached",
                            "forced": True})

    def run_collect(self, prompt: str) -> SessionResult:
        """Drive to termination and return a summary (non-streaming callers)."""
        terminal = None
        answer = None
        turns = 0
        swarms = 0
        jobs: list = []
        tok = 0
        err = ""
        for ev in self.run(prompt):
            turns = ev.turn + 1
            if ev.kind == "intent":
                tok += ev.data.get("tokens_out", 0)
            elif ev.kind == "artifacts":
                jobs.append(ev.data["job_id"]); swarms += 1
            elif ev.kind == "final":
                terminal = ev.data["action"]
                answer = ev.data.get("rationale")
            elif ev.kind == "error":
                err = ev.data["error"]
        return SessionResult(terminal, answer, turns, swarms, jobs, tok, err)
