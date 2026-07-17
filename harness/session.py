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
from typing import Iterator, Literal, Optional, get_args

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
Stop (with a grounded rationale) once the objective is met. Do NOT repeat a
near-identical swarm goal -- if the prior artifacts answered the question, stop;
if a NEW sub-question remains, narrow to it; never re-run the same investigation.
Answer trivia
directly with zero swarms. Never loop the same swarm. Emit ONLY the JSON object."""


# Classic /run stream kinds. Distinct from ConvEventKind — SessionEvent carries
# ``turn``; chat ConvEvent framing omits it. Do not unify the wire shapes.
SessionEventKind = Literal[
    "intent",
    "executing",
    "artifacts",
    "final",
    "error",
    "vision",
]

VALID_SESSION_EVENT_KINDS: frozenset[str] = frozenset(get_args(SessionEventKind))


@dataclass
class SessionEvent:
    kind: SessionEventKind
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
        # Build the driver. Try the eval registry first (it owns stub drivers and
        # bare catalog names like 'stub-oracle-v2', 'gpt-frontier'); on a KeyError
        # (e.g. a 'provider:model' picker spec the catalog does not know, like
        # 'openrouter:openai/gpt-5.4') fall through to the provider-aware builder.
        # This mirrors ConversationalSession and means a spec the picker can offer
        # never crashes here with an uncaught KeyError.
        try:
            self.driver = reg.build(self.config.driver, reach=self.config.reach)
        except Exception:
            from . import providers as _prov
            self.driver = _prov.build_pilot(self.config.driver)
        # Propagate repo/adapter to env so the bridge's execute_intent picks them
        # up. Real analysis requires BOTH a repo and the openai adapter.
        import os as _os
        if self.config.repo:
            _os.environ["HARNESS_REPO"] = self.config.repo
        if self.config.swarm_adapter:
            _os.environ["HARNESS_SWARM_ADAPTER"] = self.config.swarm_adapter

    def state(self) -> DurableState:
        return DurableState(self.state_dir)

    def preflight(self) -> Optional[str]:
        """Return an actionable error string if the driver cannot run (missing
        key), else None. Lets surfaces fail clearly instead of stack-tracing on
        the first API call.

        OAuth / multi-key credentials live in the auth pool and may not be
        exported as env vars yet -- treat a healthy pool entry as satisfying
        the driver's ``api_key_env`` (e.g. OPENAI_CODEX_TOKEN via openai-codex).
        """
        import os
        d = self.driver
        env = getattr(d, "api_key_env", None)
        if not env:
            return None
        try:
            from .credential_pool import (
                credential_satisfied,
                peek_token_for_env,
                providers_for_env_var,
                _mirror_pool_token_to_env,
            )
            # Refresh all mirrored aliases from the pool first (set-only).
            # Anthropic OAuth may leave ANTHROPIC_TOKEN set while
            # ANTHROPIC_API_KEY was cleared — remirror restores both.
            for prov in providers_for_env_var(env):
                _mirror_pool_token_to_env(prov)
        except Exception:
            pass
        if os.environ.get(env, "").strip():
            return None
        try:
            from .credential_pool import (
                credential_satisfied,
                peek_token_for_env,
                providers_for_env_var,
                _mirror_pool_token_to_env,
            )
            if credential_satisfied(env):
                # Mirror so subsequent env-only checks / child tools see it
                # (includes OAuth sibling pools, e.g. xai-oauth → XAI_API_KEY).
                tok = peek_token_for_env(env)
                if tok:
                    os.environ[env] = tok
                    for prov in providers_for_env_var(env):
                        _mirror_pool_token_to_env(prov)
                return None
        except Exception:
            pass
        reach = self.config.reach
        hint = ("set OPENROUTER_API_KEY" if reach == "openrouter"
                else f"set {env}")
        return (f"Driver '{self.config.driver}' needs an API key but {env} is "
                f"not set. {hint}, or use HARNESS_DRIVER=stub-oracle-v2 for a "
                f"no-key demo.")

    def run(self, prompt: str, images: Optional[list] = None) -> Iterator[SessionEvent]:
        """Drive the loop, yielding an event per step. The GUI consumes this.

        If images are supplied, the vision sidecar transcribes them to text once
        and prepends the transcription so a text-only driver can reason over the
        image content. The driver never receives pixels."""
        budget = self.config.budget
        system = _system(budget)
        context = prompt
        # This eval Session is gated by SWARM COUNT (`swarms >= budget`), not by
        # tokens -- driver eval measures driving, so the brake is "how many swarms
        # did it dispatch," not spend. Per-turn token counts are surfaced on each
        # `intent` event (and re-derived by run_collect); there is deliberately no
        # local token accumulator here. (ConversationalSession, the product loop,
        # is the token-metered path via AutoBudget.add_tokens.)
        swarms = 0
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
            else:
                # Every transcription failed. The driver is text-only, so with no
                # blocks it would silently answer as if no images were attached --
                # a wrong answer dressed up as a normal turn. Fail loudly instead
                # of pretending this was a text-only request.
                yield SessionEvent("final", -1, {
                    "action": "error",
                    "rationale": (f"All {len(images)} image transcription(s) failed; "
                                  "cannot answer an image request as text-only."),
                    "forced": True,
                })
                return

        for i in range(HARD_TURN_CAP):
            intent, resp, repairs = drive_with_repair(self.driver, context, system)
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
                result: BridgeResult = execute_intent(
                    intent,
                    state_dir=self.state_dir,
                    worker_mode=self.config.worker_mode,
                    cwd=self.config.repo or None,
                )
            except Exception as e:
                yield SessionEvent("error", i, {"error": f"execute: {e}"})
                return
            swarms += 1
            jobs.append(result.job_id)

            yield SessionEvent("artifacts", i, {
                "job_id": result.job_id, "num": result.num_artifacts,
                "types": result.artifact_types,
                "artifacts": result.artifacts[:8], "mode": result.mode,
                "adapter": result.adapter,
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
