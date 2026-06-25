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


from .skill_store import SkillStore
from .skill_distiller import distill_session, distill_rules
from .rule_store import RuleStore


def _mcp_result_text(out: dict) -> str:
    """Flatten an MCP tools/call result into plain text for the transcript."""
    if not isinstance(out, dict):
        return str(out)
    parts = []
    for block in out.get("content", []) or []:
        if isinstance(block, dict):
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif "text" in block:
                parts.append(str(block["text"]))
    return "\n".join(parts) if parts else str(out)
from .autobudget import AutoBudget
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
        # self-learning: load ACTIVE skills into the pilot's system context so
        # the loop compounds (procedural memory). Pending skills are NOT loaded.
        self._skills = SkillStore()
        system = PILOT_SYSTEM
        active = self._skills.list("active")
        if active:
            skills_block = "\n\n".join(
                f"## Skill: {s.name}\n{s.description}\n{s.body}" for s in active)
            system = (system + "\n\n# Learned skills (apply when relevant)\n"
                      + skills_block)
        # standing conventions (always-on, terse) -- distinct from task skills
        self._rules = RuleStore()
        active_rules = self._rules.list("active")
        if active_rules:
            rules_block = "\n".join(f"- {r.text}" for r in active_rules)
            system = (system + "\n\n# Standing rules (ALWAYS honor)\n" + rules_block)
        # the running transcript with the pilot (conversation memory)
        self._history: list[dict] = [{"role": "system", "content": system}]
        # optional durable-knowledge integration (portable-llm-wiki)
        self._wiki = WikiClient()
        self._wiki_auto = os.environ.get("HARNESS_WIKI_AUTO", "").strip() in ("1", "true", "yes")
        # optional MCP integration -- set by the server so the pilot can call MCP tools
        self._mcp = None
        # self-learning: accumulate this session's real findings for distillation
        self._session_findings: list = []
        self._first_objective: str = ""
        # token accounting for the autobudget governor (real metering, not a stub)
        self._tokens_used: int = 0

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
            # real token metering: prompt + completion (drivers report tokens_out;
            # estimate tokens_in from prompt length when not provided).
            self._tokens_used += int(getattr(resp, "tokens_out", 0) or 0)
            self._tokens_used += int(getattr(resp, "tokens_in", 0) or len(prompt) // 4)
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
                    "id": aid, "kind": act.kind, "goal": act.goal or act.tool,
                    "cwd": self.config.repo or None,
                    "adapter": self.config.swarm_adapter,
                })
                # ---- MCP tool call branch -------------------------------------
                if act.kind == "call_mcp":
                    if self._mcp is None:
                        yield ConvEvent("action_result", {"id": aid, "error": "MCP not available"})
                        self._history.append({"role": "user",
                            "content": f"(mcp {aid} unavailable)"})
                        continue
                    try:
                        out = self._mcp.call(act.tool, act.arguments)
                        text = _mcp_result_text(out)
                    except Exception as e:
                        yield ConvEvent("action_result", {"id": aid, "error": f"mcp: {e}"})
                        self._history.append({"role": "user",
                            "content": f"(mcp {act.tool} failed: {e})"})
                        continue
                    yield ConvEvent("action_result", {
                        "id": aid, "tool": act.tool, "num": 1,
                        "types": ["mcp"], "adapter": "mcp", "mode": "tool",
                        "artifacts": [{"type": "mcp", "headline": f"{act.tool}: {text[:120]}"}],
                    })
                    self._history.append({"role": "user",
                        "content": f"(mcp {act.tool} returned)\n{text[:2000]}"})
                    continue
                # ---- swarm branch --------------------------------------------
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
        # accumulate for self-learning distillation (independent of wiki config)
        if findings:
            self._session_findings.extend(findings)
            if not self._first_objective:
                self._first_objective = user_message
        if not (self._wiki_auto and self._wiki.configured and findings):
            return
        try:
            digest = session_digest(user_message, prose, findings)
            slug = f"harness-{_slugify(user_message)}"
            self._wiki.ingest(slug, digest, note="auto-captured by pm-harness",
                              run_orchestrator=False)
        except Exception:
            pass  # wiki capture is best-effort; never break the conversation


    def distill(self) -> dict:
        """Propose PENDING candidate skill(s) AND rule(s) from this session's
        accumulated findings. Human approval required before either loads into
        context. Returns a combined status dict."""
        out = {}
        try:
            out["skill"] = distill_session(self.pilot, self._first_objective or "(session)",
                                           self._session_findings, self._skills)
        except Exception as e:
            out["skill"] = {"status": "error", "reason": str(e)}
        try:
            out["rules"] = distill_rules(self.pilot, self._first_objective or "(session)",
                                         self._session_findings, self._rules)
        except Exception as e:
            out["rules"] = {"status": "error", "reason": str(e)}
        return out

    def run_auto(self, objective: str, budget: "AutoBudget" = None,
                 *, require_codegraph: bool = True):
        """FULLY-AUTO (unattended) mode: pursue an objective across many pilot
        turns WITHOUT user re-prompting, bounded by an AutoBudget governor. Yields
        the same ConvEvents as send(), plus 'auto_status' (governor snapshots) and
        a terminal 'auto_halt' with the reason.

        SAFETY PRECONDITIONS (refused otherwise):
          - a governor is required (no ceilings == no unattended run)
          - if real analysis is configured, the repo MUST be CodeGraph-indexed
            (the accuracy benchmark proved unindexed -> ~30% blind guessing, which
            is exactly the confident-garbage failure mode you must not run all
            night). Override only with require_codegraph=False.
        """
        budget = (budget or AutoBudget.from_env()).start()

        # Precondition: real analysis on an unindexed repo is refused unattended.
        if (require_codegraph and self.config.swarm_adapter == "openai"
                and self.config.repo):
            import os.path as _op
            if not _op.isdir(_op.join(self.config.repo, ".codegraph")):
                yield ConvEvent("auto_halt", {"reason":
                    f"REFUSED: {self.config.repo} has no .codegraph index. Unattended "
                    f"analysis would run blind (~30% accuracy). Run: python -m "
                    f"puppetmaster codegraph init --index", "snapshot": budget.snapshot()})
                return

        # Seed the objective + an instruction to self-continue until done.
        message = (f"{objective}\n\n(AUTONOMOUS MODE: pursue this objective to "
                   f"completion across multiple investigation rounds. After each "
                   f"round, if more investigation is warranted and useful, continue "
                   f"with another swarm; finish with no actions only when the "
                   f"objective is genuinely met or no further progress is possible.)")

        cycle = 0
        while True:
            halt = budget.check()
            if halt:
                yield ConvEvent("auto_halt", {"reason": halt, "snapshot": budget.snapshot()})
                self._maybe_ingest(objective, [], [])
                return
            cycle += 1
            findings_before = 0
            tokens_at_cycle_start = self._tokens_used
            # one pilot turn (send() drives say->act->react until it yields back)
            turn_findings_count = 0
            tripped = None
            for ev in self.send(message if cycle == 1 else
                                 "(continue toward the objective, or finish if met)"):
                # meter the governor off the stream
                if ev.kind == "action_result" and not ev.data.get("error"):
                    budget.add_swarm()
                    turn_findings_count += int(ev.data.get("num", 0) or 0)
                yield ev
                if ev.kind == "assistant_done":
                    break
                # CHECK THE CEILING MID-STREAM: a never-stopping pilot fires swarms
                # inside one send() call; without this the governor only catches it
                # between cycles and burns the whole inner budget first.
                tripped = budget.check()
                if tripped:
                    break
            if tripped:
                yield ConvEvent("auto_halt", {"reason": tripped, "snapshot": budget.snapshot()})
                self._maybe_ingest(objective, [], [])
                return
            # account for stall + emit a governor heartbeat
            budget.note_findings(turn_findings_count)
            # REAL token metering: feed the delta consumed this cycle into the
            # governor so the documented token ceiling actually trips.
            delta = self._tokens_used - tokens_at_cycle_start
            if delta > 0:
                budget.add_tokens(delta)
            yield ConvEvent("auto_status", {"cycle": cycle, "snapshot": budget.snapshot()})
            # if the pilot finished a turn with no swarms at all, it considers the
            # objective met -> stop the autonomous loop.
            if turn_findings_count == 0 and budget.idle_steps >= 1:
                yield ConvEvent("auto_halt", {"reason": "pilot reports objective met "
                    "(no further investigation)", "snapshot": budget.snapshot()})
                self._maybe_ingest(objective, [], [])
                return


def _slugify(s: str) -> str:
    import re
    return (re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-") or "session")[:60]
