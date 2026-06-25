from __future__ import annotations

"""Pilot contract: the conversational driver envelope.

The pilot is the model the USER talks to (swappable: qwen/deepseek/opus/...).
Unlike the bare DriverIntent (one action, used by the eval harness), the pilot
emits a CONVERSATIONAL TURN: prose for the human + zero-or-more orchestration
actions it wants to fire under the hood. This mirrors how Cursor/Hermes models
emit text + tool calls -- run_swarm is just a tool the pilot calls.

Envelope (what the pilot model must emit):

    {
      "say": "I'll map the auth flow first, then dig into the middleware.",
      "actions": [
        {"kind": "run_swarm", "goal": "Map authentication across the codebase",
         "roles": ["explore"]}
      ]
    }

- `say` is prose shown to the user (the conversation / transcript channel).
- `actions` is zero or more orchestration calls. Empty actions => the pilot is
  just talking (answering / explaining findings / asking) and the turn yields
  back to the user.
- When actions are present, the loop executes them, feeds the resulting
  artifacts back to the pilot, and the pilot reacts (more prose, maybe more
  actions) until it emits a turn with no actions.

The transcript lives in the user<->pilot channel. Swarm workers receive only the
DISTILLED `goal` brief (plus CodeGraph), never the transcript -- decoupling
conversation from the token-heavy investigation.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Optional


VALID_ACTION_KINDS = {"run_swarm"}


@dataclass
class PilotAction:
    kind: str
    goal: str = ""
    roles: list = field(default_factory=list)

    def validate(self) -> "PilotAction":
        if self.kind not in VALID_ACTION_KINDS:
            raise PilotError(f"unknown action kind: {self.kind!r}")
        if self.kind == "run_swarm" and not (self.goal or "").strip():
            raise PilotError("run_swarm action requires a non-empty goal")
        if self.roles and not isinstance(self.roles, list):
            raise PilotError("roles must be a list")
        return self


@dataclass
class PilotTurn:
    say: str = ""
    actions: list = field(default_factory=list)  # list[PilotAction]

    @property
    def has_actions(self) -> bool:
        return bool(self.actions)


class PilotError(ValueError):
    """Raised when a pilot envelope cannot be parsed/validated."""


def _coerce_actions(raw_actions) -> list:
    actions = []
    if not raw_actions:
        return actions
    if isinstance(raw_actions, dict):
        raw_actions = [raw_actions]
    if not isinstance(raw_actions, list):
        raise PilotError("actions must be a list")
    for a in raw_actions:
        if not isinstance(a, dict):
            raise PilotError("each action must be an object")
        kind = a.get("kind") or a.get("action") or a.get("tool") or "run_swarm"
        goal = a.get("goal") or a.get("instruction") or a.get("task") or ""
        roles = a.get("roles") or []
        if isinstance(roles, str):
            roles = [roles]
        actions.append(PilotAction(kind=str(kind), goal=str(goal), roles=roles).validate())
    return actions


def parse_pilot_turn(text: str) -> PilotTurn:
    """Lenient parse of a pilot envelope from model output. Accepts:
    - a clean JSON object {say, actions}
    - JSON wrapped in prose or ```json fences
    - bare prose with no JSON  => treated as say-only (no actions)
    """
    if text is None:
        raise PilotError("empty pilot output")
    raw = text.strip()
    if not raw:
        raise PilotError("empty pilot output")

    obj = _extract_json_object(raw)
    if obj is None:
        # No JSON at all -> the model just talked. Treat the whole thing as prose.
        return PilotTurn(say=raw, actions=[])

    if not isinstance(obj, dict):
        return PilotTurn(say=raw, actions=[])

    say = obj.get("say") or obj.get("message") or obj.get("text") or ""
    actions = _coerce_actions(obj.get("actions") or obj.get("tool_calls"))
    # If there's prose outside the JSON and no `say`, keep the prose.
    if not say:
        outside = _prose_outside_json(raw)
        say = outside or ""
    return PilotTurn(say=str(say).strip(), actions=actions)


def _extract_json_object(text: str):
    """Find the first balanced top-level {...} and json.loads it. Tolerates code
    fences and surrounding prose. Returns None if no parseable object."""
    # strip ```json ... ``` fences first
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidates = []
    if fence:
        candidates.append(fence.group(1))
    # also try the first balanced brace span
    span = _first_balanced_braces(text)
    if span:
        candidates.append(span)
    for c in candidates:
        try:
            return json.loads(c, strict=False)
        except Exception:
            continue
    return None


def _first_balanced_braces(text: str) -> Optional[str]:
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _prose_outside_json(text: str) -> str:
    span = _first_balanced_braces(text)
    if not span:
        return text
    return text.replace(span, "").replace("```json", "").replace("```", "").strip()


PILOT_SYSTEM = """You are the pilot of a Puppetmaster-orchestrated coding harness.
You talk directly with the user like a senior engineer pairing with them: explain
your plan in plain prose, and when you need to investigate the codebase, fire an
orchestration swarm.

You drive a fan-out engine. You do NOT read files yourself -- you dispatch swarms
that read the real code (with CodeGraph) and return structured artifacts
(findings/risks/decisions). Your job is to converse, decide WHEN to investigate,
write a sharp swarm brief, and explain what comes back.

Respond ONLY with a JSON object:

  {"say": "<prose for the user>", "actions": [ {"kind":"run_swarm","goal":"<a
  sharp, self-contained investigation brief>","roles":["explore"]} ]}

Rules:
- `say` is always present: talk to the user in natural prose.
- Use actions ONLY when real codebase investigation is needed. For greetings,
  trivia, or explaining prior findings, emit actions: [].
- Each run_swarm goal is a DISTILLED brief: state exactly what to investigate and
  what to report. The worker never sees this conversation, so make the goal
  self-contained.
- After a swarm returns artifacts, you'll be called again: explain the findings to
  the user in `say`, and either fire a narrowed follow-up swarm or finish (no
  actions) if the objective is met.
- Be concise and concrete. Never invent file contents; rely on the artifacts.
"""
