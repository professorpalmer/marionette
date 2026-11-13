from __future__ import annotations

"""Driver protocol: anything that, given a task prompt, returns raw text the
harness will parse into a DriverIntent. Keeps the model boundary clean -- the
driver returns text + token accounting; parsing/validation/scoring is the
harness's job, identically for every model.
"""

import json
import re
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


_CREDENTIAL_HEADERS = frozenset(("authorization", "x-api-key", "x-goog-api-key", "api-key"))

# Account identifiers a provider puts in an error body, such as OpenRouter's
# "user_id". Error text reaches the chat, the transcript and the logs.
_ACCOUNT_ID_RE = re.compile(
    r"""(?ix)
    (["']?)
    (?:user|account|org|organization|customer|team|workspace)[_-]?id
    \1 \s* [:=] \s*
    (["']?) [^"',}\s]+ \2
    """
)


def scrub_account_ids(text: str) -> str:
    """Replace the value of each account identifier field with REDACTED."""
    if not text:
        return text
    return _ACCOUNT_ID_RE.sub(
        lambda m: m.group(0)[: m.start(2) - m.start(0)] + m.group(2) + "REDACTED" + m.group(2),
        text,
    )


def http_error_excerpt(err: Any, headers: Optional[dict] = None, limit: int = 500) -> str:
    """Body of an HTTP error for the error string, cut to ``limit``.

    Each credential the request sent is replaced before the cut. A provider
    that echoes the key then cannot leave its prefix in the excerpt when the
    key crosses the cut.
    """
    text = err.read().decode("utf-8", "replace")
    for name, value in (headers or {}).items():
        if str(name).lower() not in _CREDENTIAL_HEADERS:
            continue
        secret = str(value or "").strip()
        if secret[:7].lower() == "bearer ":
            secret = secret[7:].strip()
        if len(secret) >= 8:
            text = text.replace(secret, "REDACTED")
    return scrub_account_ids(text)[:limit]


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


# Sent as a user turn after a reply that the output limit cut, so the model
# continues the text. Drivers and the request snapshot share this exact text.
LENGTH_CONTINUE_PROMPT = (
    "[System: Your previous response was truncated by the output length "
    "limit. Continue exactly where you left off. Do not restart or repeat "
    "prior text. Finish the answer directly.]"
)


# Follow-up requests after a text reply that hit the output limit.
MAX_LENGTH_CONTINUES = 3


def stream_with_length_continue(stream_once, messages, *, cut_reasons, model,
                                on_delta=None, is_cancelled=None) -> DriverResponse:
    """Stream one reply. A text reply cut by the output limit is continued.

    ``stream_once(messages, on_delta)`` sends one request. Up to
    ``MAX_LENGTH_CONTINUES`` follow-ups append the cut text and
    ``LENGTH_CONTINUE_PROMPT``. Their text streams through ``on_delta`` and
    joins the first reply. A reply with tool calls is never continued.
    """
    from pmharness.reasoning import strip_think_blocks

    # Each reply's text is stripped at its ends. Join the raw deltas so a cut
    # at a space keeps the space that the user saw stream.
    raw_text = []

    def tee(piece):
        raw_text.append(piece)
        if on_delta is not None:
            on_delta(piece)

    def cut(resp):
        meta = resp.meta or {}
        return (not resp.error and meta.get("finish_reason") in cut_reasons
                and not meta.get("tool_calls") and bool((resp.text or "").strip()))

    first = stream_once(messages, tee)
    attempts = [first]
    working = list(messages)
    while (cut(attempts[-1]) and len(attempts) <= MAX_LENGTH_CONTINUES
           and not (is_cancelled is not None and is_cancelled())):
        working = working + [
            {"role": "assistant", "content": attempts[-1].text},
            {"role": "user", "content": LENGTH_CONTINUE_PROMPT},
        ]
        attempts.append(stream_once(working, tee))
    if len(attempts) == 1:
        return first
    last = attempts[-1]
    meta = length_attempt_meta(attempts, last)
    meta["length_continues"] = len(attempts) - 1
    meta["reasoning"] = "".join((a.meta or {}).get("reasoning") or "" for a in attempts)
    meta["stream_started"] = True
    if last.error and not last.text:
        meta["continuation_error"] = last.error
    return DriverResponse(
        text=strip_think_blocks("".join(raw_text)),
        tokens_in=sum(a.tokens_in for a in attempts),
        tokens_out=sum(a.tokens_out for a in attempts),
        latency_ms=sum(a.latency_ms for a in attempts),
        model=model,
        error=None if last.error and not last.text else last.error,
        meta=meta,
    )


def length_attempt_meta(responses: list[DriverResponse], terminal: DriverResponse) -> dict:
    """Preserve terminal provenance while summing per-request accounting."""
    meta = dict(terminal.meta or {})
    source_meta = [response.meta or {} for response in responses]
    for key in (
        "cache_read_tokens", "cache_write_tokens",
        "cache_write_5m_tokens", "cache_write_1h_tokens",
    ):
        values = [item.get(key) for item in source_meta]
        if any(isinstance(value, int) and not isinstance(value, bool) for value in values):
            meta[key] = sum(
                value for value in values
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0
            )
    costs = [
        float(item["provider_cost_usd"]) for item in source_meta
        if isinstance(item.get("provider_cost_usd"), (int, float))
        and not isinstance(item.get("provider_cost_usd"), bool)
        and float(item["provider_cost_usd"]) >= 0.0
    ]
    if costs:
        meta["provider_cost_usd"] = sum(costs)
    ttl_bases = {
        str(item.get("cache_write_ttl_basis") or "").strip().lower()
        for item in source_meta if str(item.get("cache_write_ttl_basis") or "").strip()
    }
    if ttl_bases:
        meta["cache_write_ttl_basis"] = next(iter(ttl_bases)) if len(ttl_bases) == 1 else "inferred"
    performance = {}
    for item in source_meta:
        for key, value in (item.get("stream_performance") or {}).items():
            if isinstance(value, int) and not isinstance(value, bool):
                performance[key] = performance.get(key, 0) + value
    if performance:
        meta["stream_performance"] = performance
    return meta
