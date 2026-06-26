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


VALID_ACTION_KINDS = {"run_swarm", "call_mcp", "read_file", "write_file", "run_command", "list_dir", "web_search", "web_fetch", "read_pdf"}


@dataclass
class PilotAction:
    kind: str
    goal: str = ""
    roles: list = field(default_factory=list)
    tool: str = ""              # call_mcp: qualified MCP tool name (server.tool)
    arguments: dict = field(default_factory=dict)  # call_mcp: tool arguments
    path: str = ""
    content: str = ""
    command: str = ""
    query: str = ""
    url: str = ""

    def validate(self) -> "PilotAction":
        if self.kind not in VALID_ACTION_KINDS:
            raise PilotError(f"unknown action kind: {self.kind!r}")
        if self.kind == "run_swarm" and not (self.goal or "").strip():
            raise PilotError("run_swarm action requires a non-empty goal")
        if self.kind == "call_mcp" and not (self.tool or "").strip():
            raise PilotError("call_mcp action requires a 'tool' (server.tool)")
        if self.kind in ("read_file", "write_file") and not (self.path or "").strip():
            raise PilotError(f"{self.kind} action requires a 'path'")
        if self.kind == "run_command" and not (self.command or "").strip():
            raise PilotError("run_command action requires a 'command'")
        if self.kind == "web_search" and not (self.query or "").strip():
            raise PilotError("web_search action requires a 'query'")
        if self.kind == "web_fetch" and not (self.url or "").strip():
            raise PilotError("web_fetch action requires a 'url'")
        if self.kind == "read_pdf" and not (self.path or "").strip() and not (self.url or "").strip():
            raise PilotError("read_pdf action requires a 'path' or 'url'")
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
        kind = a.get("kind") or a.get("action") or "run_swarm"
        # if a bare "tool" + "arguments" shape is given, treat as call_mcp
        tool = a.get("tool") or ""
        arguments = a.get("arguments") or a.get("args") or {}
        if tool and kind in ("run_swarm",) and ("goal" not in a and "instruction" not in a):
            kind = "call_mcp"
        goal = a.get("goal") or a.get("instruction") or a.get("task") or ""
        roles = a.get("roles") or []
        if isinstance(roles, str):
            roles = [roles]
        if not isinstance(arguments, dict):
            arguments = {}
        path = a.get("path") or ""
        content = a.get("content") or ""
        command = a.get("command") or ""
        query = a.get("query") or ""
        url = a.get("url") or ""
        actions.append(PilotAction(kind=str(kind), goal=str(goal), roles=roles,
                                   tool=str(tool), arguments=arguments,
                                   path=str(path), content=str(content), command=str(command),
                                   query=str(query), url=str(url)).validate())
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
You talk directly with the user like a senior engineer pairing with them.

You have direct access to a local CodeGraph-indexed workspace and can explore/edit it using these real actions:
- `read_file`: read a file's contents from the workspace. Requires `path`.
- `write_file`: write/create a file atomically. Requires `path` and `content`.
- `run_command`: run a terminal shell command. Requires `command`.
- `list_dir`: list the files and folders inside a directory. `path` is optional.
- `run_swarm`: dispatch a parallel agent swarm for complex/broad investigations. Requires `goal`.
- `web_search`: search the internet and return top results. Requires `query`.
- `web_fetch`: read a web page's text contents. Requires `url`.
- `read_pdf`: extract plain text from a local PDF file or PDF URL. Requires `path` or `url`.

Respond ONLY with a JSON object:

  {
    "say": "<prose for the user describing your reasoning and plan>",
    "actions": [
      {"kind": "read_file", "path": "src/main.py"}
    ]
  }

Rules:
- `say` is always present: the `say` field is for natural-language explanation to the USER ONLY. It must NOT contain tool output, command results, tracebacks, file listings, or the literal text of tool-result messages. Never echo or quote the "(... completed with exit code ...)" tool-result envelopes -- those are shown to the user automatically in the action chips. Keep say concise: explain what you're doing and why, then act.
- Prefer your direct tools (read_file, write_file, run_command, list_dir) for precise actions and testing.
- Use `run_swarm` when you need a team of workers to analyze a broad issue or scan the codebase.
- Always verify your work by running tests via `run_command` after editing.
- Be concise and concrete. Never invent file contents; read the files first.
"""
