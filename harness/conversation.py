from __future__ import annotations

"""ConversationalSession: the PILOT loop (the product UX).

Difference from Session (the eval loop): Session emits one bare intent per task
and is for measuring drivers. ConversationalSession is the human-facing product:
the pilot CONVERSES (prose) and fires orchestration ACTIONS as collapsible
tool-calls, reacting to the artifacts they return, until it finishes a turn with
no actions and yields back to the user.

Transcript model:
- The pilot carries a running transcript (system + user + pilot prose + compact
  action results) ACROSS turns within a session. This is the conversation the
  user follows.
- Swarm workers receive only the distilled `goal` brief (+ CodeGraph). The
  transcript never enters a worker. Conversation and investigation are decoupled.

Events yielded (for GUI/CLI):
- ("message", {role:"assistant", text})        -> pilot prose (conversation)
- ("action_start", {id, kind, goal, cwd})      -> a collapsible card opens
- ("action_result", {id, job_id, num, types,   -> the card's body (artifacts)
       artifacts, adapter, mode})
- ("assistant_done", {turns, swarms})          -> turn complete, yield to user
- ("error", {error})
"""

import os
import time
from dataclasses import dataclass, field
from typing import Iterator, Optional

from pmharness import registry as reg
from . import providers as prov
from pmharness.intent import DriverIntent
from pmharness.bridge import execute_intent, BridgeResult
from .pilot import (parse_pilot_turn, PilotTurn, PilotError, PILOT_SYSTEM)
from .wiki import WikiClient, session_digest
from .config import HarnessConfig
from .state import DurableState


HARD_PILOT_STEPS = 10  # safety cap on pilot<->swarm round-trips per user message


@dataclass
class ConvEvent:
    kind: str
    data: dict = field(default_factory=dict)


class ConversationalSession:
    def __init__(self, config: HarnessConfig) -> None:
        self.config = config
        import tempfile
        self.state_dir = config.state_dir or tempfile.mkdtemp(prefix="pilot-")
        # Provider-aware pilot: 'provider:model' spans any provider whose key is
        # set; a bare model resolves against available providers, else OpenRouter.
        try:
            self.pilot = prov.build_pilot(config.driver)
        except prov.ProviderError:
            # fall back to the eval registry (OpenRouter field) for known names
            self.pilot = reg.build(config.driver, reach=config.reach)
        # propagate repo/adapter so the bridge runs real analysis when configured
        if config.repo:
            os.environ["HARNESS_REPO"] = config.repo
        if config.swarm_adapter:
            os.environ["HARNESS_SWARM_ADAPTER"] = config.swarm_adapter
        # the running transcript with the pilot (conversation memory)
        self._history: list[dict] = [{"role": "system", "content": PILOT_SYSTEM}]
        # optional durable-knowledge integration (portable-llm-wiki)
        self._wiki = WikiClient()
        self._wiki_auto = os.environ.get("HARNESS_WIKI_AUTO", "").strip() in ("1", "true", "yes")

    @property
    def durable(self) -> DurableState:
        return DurableState(self.state_dir)

    def _render_history(self) -> str:
        """Flatten transcript into a single prompt for completion-style drivers."""
        lines = []
        for m in self._history:
            role = m["role"].upper()
            lines.append(f"{role}: {m['content']}")
        lines.append("ASSISTANT:")
        return "\n\n".join(lines)

    def send(self, user_message: str) -> Iterator[ConvEvent]:
        """Process one user message: drive the pilot loop until it yields back."""
        self._history.append({"role": "user", "content": user_message})
        swarms = 0
        action_seq = 0
        demo_swarms = 0  # count swarms that returned the demo substrate
        turn_findings: list = []   # accumulate real findings for wiki ingest
        turn_prose: list = []      # accumulate pilot prose for the digest

        for step in range(HARD_PILOT_STEPS):
            # 1. Ask the pilot for its next conversational turn.
            sys_prompt = PILOT_SYSTEM
            prompt = self._render_history()
            try:
                resp = self.pilot.complete(prompt, system=sys_prompt)
            except Exception as e:
                yield ConvEvent("error", {"error": f"pilot transport: {e}"})
                return
            if resp.error:
                yield ConvEvent("error", {"error": f"pilot: {resp.error}"})
                return
            try:
                turn = parse_pilot_turn(resp.text)
            except PilotError as e:
                # one lenient retry: tell the pilot to fix its envelope
                self._history.append({"role": "user",
                    "content": f"(system) Your last reply was not valid. {e}. "
                               f"Reply with the JSON envelope {{\"say\":...,\"actions\":[...]}}."})
                continue

            # 2. Emit the pilot's prose to the user.
            if turn.say:
                yield ConvEvent("message", {"role": "assistant", "text": turn.say})
                turn_prose.append(turn.say)
            # record the pilot's turn in transcript (prose only -- the conversation)
            self._history.append({"role": "assistant", "content": turn.say or "(acting)"})

            # 3. No actions => the pilot is done talking; yield back to the user.
            if not turn.has_actions:
                self._maybe_ingest(user_message, turn_prose, turn_findings)
                yield ConvEvent("assistant_done", {"turns": step + 1, "swarms": swarms})
                return

            # 4. Execute each action as a collapsible tool-call.
            for act in turn.actions:
                action_seq += 1
                aid = f"a{action_seq}"
                yield ConvEvent("action_start", {
                    "id": aid, "kind": act.kind, "goal": act.goal,
                    "cwd": self.config.repo or None,
                    "adapter": self.config.swarm_adapter,
                })
                intent = DriverIntent(action="run_swarm", goal=act.goal,
                                      roles=act.roles or None, rationale="pilot")
                try:
                    result: BridgeResult = execute_intent(intent, state_dir=self.state_dir)
                except Exception as e:
                    yield ConvEvent("action_result", {"id": aid, "error": f"execute: {e}"})
                    self._history.append({"role": "user",
                        "content": f"(swarm {aid} failed: {e})"})
                    continue
                swarms += 1
                if result.adapter == "demo":
                    demo_swarms += 1
                yield ConvEvent("action_result", {
                    "id": aid, "job_id": result.job_id, "num": result.num_artifacts,
                    "types": result.artifact_types, "artifacts": result.artifacts[:8],
                    "adapter": result.adapter, "mode": result.mode,
                })
                # collect non-substrate findings for durable knowledge capture
                if result.adapter != "demo":
                    turn_findings.extend(
                        a for a in result.artifacts if a.get("type") != "verification")
                # 5. Feed DISTILLED artifacts back into the transcript (not raw files).
                digest = "\n".join(f"  - [{a['type']}] {a['headline']}"
                                   for a in result.artifacts[:8]) or "  (no artifacts)"
                stall = ""
                if demo_swarms >= 2:
                    stall = ("\n(NOTE: swarms are running on the DEMO substrate, which "
                             "returns generic artifacts -- not real codebase analysis. "
                             "Do NOT keep retrying; explain this to the user and finish "
                             "with no actions. Real analysis needs --repo + "
                             "--swarm-adapter openai.)")
                self._history.append({"role": "user", "content":
                    f"(swarm {aid} '{act.goal}' returned {result.num_artifacts} "
                    f"artifacts via {result.adapter}:\n{digest}\n"
                    f"Explain these findings to the user and either run a narrowed "
                    f"follow-up swarm or finish with no actions.){stall}"})

        # Hit the step cap -- close the turn gracefully.
        self._maybe_ingest(user_message, turn_prose, turn_findings)
        yield ConvEvent("message", {"role": "assistant",
            "text": "(Reached the investigation step limit for this message.)"})
        yield ConvEvent("assistant_done", {"turns": HARD_PILOT_STEPS, "swarms": swarms})

    def _maybe_ingest(self, user_message: str, prose: list, findings: list) -> None:
        """Auto-ingest a session digest to the wiki when enabled and there are
        real findings worth capturing. Never fires the orchestrator (token-spend)."""
        if not (self._wiki_auto and self._wiki.configured and findings):
            return
        try:
            digest = session_digest(user_message, prose, findings)
            slug = f"harness-{_slugify(user_message)}"
            self._wiki.ingest(slug, digest, note="auto-captured by pm-harness",
                              run_orchestrator=False)
        except Exception:
            pass  # wiki capture is best-effort; never break the conversation


def _slugify(s: str) -> str:
    import re
    return (re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-") or "session")[:60]
