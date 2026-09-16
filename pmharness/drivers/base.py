from __future__ import annotations

"""Driver protocol: anything that, given a task prompt, returns raw text the
harness will parse into a DriverIntent. Keeps the model boundary clean -- the
driver returns text + token accounting; parsing/validation/scoring is the
harness's job, identically for every model.
"""

import json
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol
from .reasoning_envelope import ReasoningEnvelope

KNOWN_ASSISTANT_PHASES = frozenset(("commentary", "final_answer"))


def known_assistant_phase(value: Any) -> Optional[str]:
    """Return commentary|final_answer when OpenAI supplied that phase.

    Absent, blank, and unknown values stay None so legacy Chat Completions
    history is never inferred as final_answer.
    """
    if not isinstance(value, str):
        return None
    phase = value.strip().lower()
    if phase in KNOWN_ASSISTANT_PHASES:
        return phase
    return None


def stamp_assistant_phase(msg: dict, value: Any) -> dict:
    """Copy a known assistant phase onto a history message. No-op otherwise."""
    phase = known_assistant_phase(value)
    if phase and isinstance(msg, dict) and msg.get("role") == "assistant":
        msg["phase"] = phase
    return msg


_SUCCESS_STATUSES = frozenset({"ok", "success", "completed", "done", "no_op", "native_image"})
_FAILURE_STATUSES = frozenset({
    "error", "failed", "failure", "exception", "timeout", "timed_out",
    "cancelled", "canceled", "blocked", "denied", "interrupted", "aborted",
    "validation_error", "stale_anchor", "repo_not_open", "path_traversal",
    "invalid_arguments", "stale_generation", "read_only_role", "disabled",
    "not_found", "is_directory", "not_a_directory", "filenotfound",
    "corrupt_store", "cap_exceeded", "verification_failed", "ambiguous",
    "internal_uri_error",
})


def tool_result_semantics(content: str, *, ok: Optional[bool] = None) -> dict:
    """Capture receipt truth before compaction; absent outcome stays absent."""
    try:
        receipt = json.loads(content)
    except (ValueError, TypeError):
        receipt = None
    receipt = receipt if isinstance(receipt, dict) else {}
    status = receipt.get("status")
    explicit_error = (
        ok is False or receipt.get("ok") is False
        or receipt.get("is_error") is True or receipt.get("isError") is True
        or bool(receipt.get("error"))
    )
    if not isinstance(status, str) or not status.strip():
        if explicit_error:
            status = "error"
        elif (
            ok is True or receipt.get("ok") is True
            or receipt.get("is_error") is False or receipt.get("isError") is False
        ):
            status = "ok"
        else:
            return {}
    fields = {"status": status}
    normalized = status.strip().lower()
    if explicit_error or normalized in _FAILURE_STATUSES:
        fields["is_error"] = True
    elif normalized in _SUCCESS_STATUSES:
        fields["is_error"] = False
    return fields


def tool_result_content(msg: dict):
    """Expose otherwise lost outcome metadata using provider-supported content."""
    content = msg.get("content") or ""
    original = tool_result_semantics(content)
    canonical = tool_result_semantics(json.dumps({
        key: msg[key] for key in ("status", "is_error") if key in msg
    }))
    semantics = {**original, **canonical}
    if original.get("is_error") is True or canonical.get("is_error") is True:
        semantics["is_error"] = True
    if not semantics or (
        semantics.get("is_error") is not True
        and semantics.get("status", "").strip().lower() in _SUCCESS_STATUSES
    ):
        return content
    if all(original.get(key) == value for key, value in semantics.items()):
        return content
    try:
        receipt = json.loads(content)
    except (ValueError, TypeError):
        receipt = None
    # Preserve the original object and its output instead of nesting receipts.
    payload = dict(receipt) if isinstance(receipt, dict) else {"output": content}
    payload.update(semantics)
    return json.dumps(payload, ensure_ascii=False)


def chat_completions_messages(messages: list) -> list:
    """Drop canonical metadata unsupported by Chat Completions, without mutation."""
    out = []
    for msg in messages:
        if isinstance(msg, dict) and (
            "phase" in msg or "reasoning_envelope" in msg or (msg.get("role") == "tool" and (
                "status" in msg or "is_error" in msg
            ))
        ):
            copy = dict(msg)
            copy.pop("phase", None)
            copy.pop("reasoning_envelope", None)
            if msg.get("role") == "tool":
                copy["content"] = tool_result_content(msg)
                copy.pop("status", None)
                copy.pop("is_error", None)
            out.append(copy)
        else:
            out.append(msg)
    return out


@dataclass
class DriverResponse:
    text: str
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: float = 0.0
    model: str = ""
    error: Optional[str] = None
    meta: dict = field(default_factory=dict)
    assistant_phase: Optional[str] = None
    reasoning_envelope: Optional[ReasoningEnvelope] = None


SYSTEM_PROMPT = """You are the driver loop for Puppetmaster, an orchestration engine.
You do NOT write prose explanations or narrate your actions. For each task you
emit exactly ONE JSON object -- a DriverIntent -- and nothing else.

Schema:
  action:      one of "run_swarm" | "answer" | "stop"   (required)
  goal:        string; REQUIRED when action=run_swarm; the swarm objective
  roles:       optional array; subset of
               ["explore","pipeline-mapper","decision-explainer",
                "conflict-auditor","test-coverage-reviewer"]
  worker_mode: optional; "subprocess" | "inline" | "daemon"  (default subprocess)
  rationale:   one short sentence on why this action

Decision policy:
  - Use action="run_swarm" for tasks that require investigating, auditing,
    refactoring, or analyzing a codebase across multiple files.
  - Use action="answer" for trivial questions you can answer directly with no
    orchestration (definitions, one-line facts). Do not waste a swarm on these.
  - Use action="stop" when the work is already complete or no action is needed.

Output ONLY the JSON object."""


class Driver(Protocol):
    name: str

    def complete(self, task_prompt: str, *, system: str = SYSTEM_PROMPT) -> DriverResponse:
        ...

    def chat(self, messages: list, *, tools: Optional[list] = None, system: Optional[str] = None) -> DriverResponse:
        ...
