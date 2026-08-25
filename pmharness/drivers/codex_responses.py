"""CodexResponsesDriver: ChatGPT/Codex plan burn via chatgpt.com Responses API.

Uses pooled openai-codex OAuth access tokens. stdlib-only. Headers mirror
Hermes Cloudflare/originator requirements so non-browser hosts are not 403'd.

The ChatGPT Codex backend requires ``stream: true`` on every create — non-stream
POSTs return HTTP 400 ``{"detail":"Stream must be set to true"}``. We always
stream SSE and assemble a final DriverResponse (Hermes-style event consumption:
prefer ``output_item.done`` + text deltas; never rely on terminal
``response.output`` which can be null).
"""

from __future__ import annotations

import base64
import json
import os
import socket
import time
import uuid
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from .base import DriverResponse, SYSTEM_PROMPT
from .retry import with_retry


DEFAULT_CODEX_BASE = "https://chatgpt.com/backend-api/codex"

_TERMINAL_EVENT_TYPES = frozenset({
    "response.completed",
    "response.incomplete",
    "response.failed",
})


def responses_stream_label(*, chatgpt_backend: bool, base_url: str = "") -> str:
    """User-facing host name for Responses SSE errors.

    ChatGPT Codex and OpenCode Go share this driver. The error must name the
    host the picker selected, not the class that implements the wire.
    """
    if chatgpt_backend:
        return "Codex Responses"
    host = str(base_url or "").lower()
    if "opencode.ai" in host:
        return "OpenCode Responses"
    return "OpenAI Responses"


def responses_stream_error(
    label: str,
    kind: str,
    last_event: str = "",
) -> str:
    """Build a stream-death error that names the host, not the driver class."""
    name = str(label or "").strip() or "OpenAI Responses"
    if kind == "timeout":
        msg = f"{name} stream timed out"
    elif kind == "ended":
        msg = f"{name} stream ended without a terminal response"
    elif kind == "failed":
        head = name[:-10] if name.endswith(" Responses") else name
        msg = f"{head} response failed"
    else:
        msg = f"{name} stream did not emit a terminal response"
    event = str(last_event or "").strip()
    if event:
        msg = f"{msg} (last event: {event})"
    return msg

# Hermes-aligned: reasoning-only incomplete turns need a distinct user nudge
# or the retry is byte-identical and fails forever.
_CODEX_INCOMPLETE_NUDGE = (
    "[System: Your previous response contained only internal reasoning and "
    "never produced a visible answer or tool call. Do not keep thinking. "
    "Produce your final answer as plain text now (or make the tool call "
    "you were planning).]"
)
_CODEX_LENGTH_CONTINUE = (
    "[System: Your previous response was truncated by the output length "
    "limit. Continue exactly where you left off. Do not restart or repeat "
    "prior text. Finish the answer directly.]"
)
_CODEX_MAX_INCOMPLETE_RETRIES = 3
# ChatGPT Codex only: after a visible final_answer with no tools, do not
# wait forever for response.completed. Codex keepalives reset a long socket
# timeout, which left Electron on Still working until Stop sealed the
# already-buffered summary. Drain a short idle for in-flight summary tokens,
# then finish. Non-ChatGPT Responses hosts (OpenCode Go Muse, etc.) must
# wait for an authoritative terminal instead of forging completed.
_POST_ANSWER_SLICE_SECONDS = 0.25
_POST_ANSWER_IDLE_SECONDS = 0.5
_POST_ANSWER_MAX_SECONDS = 2.0
_POST_ANSWER_KEEPALIVE_LIMIT = 3
_INCOMPLETE_ANSWER_SUFFIXES = (":", ",", ";", "—", "–", "-", "/", "(", "[", "{")
_CONTENT_FILTER_MSG = (
    "Model declined to respond (content filter). Try rephrasing the request "
    "or narrowing the context."
)


def answer_looks_incomplete(text: str) -> bool:
    """True when idle-drain would cut a clause still being written."""
    t = (text or "").rstrip()
    if not t:
        return False
    if t.endswith(_INCOMPLETE_ANSWER_SUFFIXES):
        return True
    if t.count("```") % 2 == 1:
        return True
    return False


def responses_input_has_tool_results(body: Optional[dict]) -> bool:
    """True when this POST is a tool-result follow-up, not a first-shot chat."""
    items = (body or {}).get("input") if isinstance(body, dict) else None
    if not isinstance(items, list):
        return False
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "")
        if kind in ("function_call_output", "function_call"):
            return True
    return False


def _codex_cloudflare_headers(access_token: str, *, streaming: bool = True) -> Dict[str, str]:
    headers = {
        "User-Agent": "codex_cli_rs/0.0.0 (Marionette)",
        "originator": "codex_cli_rs",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
        "Accept": "text/event-stream" if streaming else "application/json",
    }
    try:
        parts = access_token.split(".")
        if len(parts) >= 2:
            payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload_b64))
            auth = claims.get("https://api.openai.com/auth") or {}
            acct = auth.get("chatgpt_account_id")
            if isinstance(acct, str) and acct:
                headers["ChatGPT-Account-ID"] = acct
    except Exception:
        pass
    return headers


def _codex_session_cache_key(prompt_cache_key: Any) -> Optional[str]:
    """Normalize the durable Codex prompt-cache / session identity."""
    if not isinstance(prompt_cache_key, str):
        return None
    key = prompt_cache_key.strip()
    return key or None


def _codex_logical_thread_id(prompt_cache_key: Any) -> Optional[str]:
    """Logical thread identity for one Marionette chat.

    Native Codex separates prompt-cache session identity (``prompt_cache_key`` /
    ``session_id``) from logical thread identity (``thread_id`` /
    ``x-client-request-id``). Derive a deterministic UUID5 from the durable
    cache key so the thread id is distinct from session id, stable across
    repeated POSTs for the same Marionette chat, and different across chats.
    """
    key = _codex_session_cache_key(prompt_cache_key)
    if not key:
        return None
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"marionette:codex-thread:{key}"))


def _codex_session_affinity_headers(prompt_cache_key: Any) -> Dict[str, str]:
    """Mirror native Codex Responses session routing when cache key is set.

    Native wire (codex-api ``responses.rs`` / ``headers.rs``):
      - ``session-id`` ← session identity
      - ``thread-id`` ← logical thread identity
      - ``x-client-request-id`` ← logical thread identity (stable, not per-POST)

    Marionette keeps ``session-id`` equal to the durable ``prompt_cache_key`` and
    stamps ``thread-id`` / ``x-client-request-id`` with the derived logical
    thread UUID for that chat.
    """
    session_key = _codex_session_cache_key(prompt_cache_key)
    thread_id = _codex_logical_thread_id(prompt_cache_key)
    if not session_key or not thread_id:
        return {}
    return {
        "session-id": session_key,
        "thread-id": thread_id,
        # Native Codex stamps x-client-request-id with thread_id, not a fresh UUID.
        "x-client-request-id": thread_id,
    }


def _codex_client_metadata(prompt_cache_key: Any) -> Dict[str, str]:
    """Build native Codex body identity metadata for one Marionette chat.

    One Marionette chat → durable prompt-cache ``session_id`` plus a distinct
    deterministic logical ``thread_id`` derived from that key.
    """
    session_key = _codex_session_cache_key(prompt_cache_key)
    thread_id = _codex_logical_thread_id(prompt_cache_key)
    if not session_key or not thread_id:
        return {}
    return {
        "session_id": session_key,
        "thread_id": thread_id,
    }


def _content_parts_to_responses(
    content: Any, *, role: str,
) -> List[dict]:
    """Map OpenAI-shaped multimodal parts to Responses input_text / input_image."""
    part_type = "output_text" if role == "assistant" else "input_text"
    if isinstance(content, str):
        return [{"type": part_type, "text": content}]
    if not isinstance(content, list):
        text = json.dumps(content) if content is not None else ""
        return [{"type": part_type, "text": text}]
    parts: List[dict] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype == "text":
            parts.append({"type": part_type, "text": part.get("text") or ""})
        elif ptype == "image_url" and role != "assistant":
            url = ""
            image = part.get("image_url")
            if isinstance(image, dict):
                url = str(image.get("url") or "")
            elif isinstance(image, str):
                url = image
            if url:
                parts.append({"type": "input_image", "image_url": url})
        elif ptype == "input_image" and role != "assistant":
            url = str(part.get("image_url") or "")
            if url:
                parts.append({"type": "input_image", "image_url": url})
    return parts or [{"type": part_type, "text": ""}]


def _messages_to_responses_input(messages: List[dict]) -> List[dict]:
    """Minimal chat → Responses input conversion (text + images + tool stubs)."""
    out: List[dict] = []
    for msg in messages:
        role = str(msg.get("role") or "user")
        content = msg.get("content")
        if role == "system":
            continue
        if role == "tool":
            out.append({
                "type": "function_call_output",
                "call_id": msg.get("tool_call_id") or msg.get("id") or "",
                "output": content if isinstance(content, str) else json.dumps(content),
            })
            continue
        if role == "assistant" and msg.get("tool_calls"):
            text = content if isinstance(content, str) else ""
            if text:
                out.append({
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text}],
                })
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function") or {}
                out.append({
                    "type": "function_call",
                    "call_id": tc.get("id") or "",
                    "name": fn.get("name") or "",
                    "arguments": fn.get("arguments") or "{}",
                })
            continue
        wire_role = "user" if role == "user" else role
        out.append({
            "type": "message",
            "role": wire_role,
            "content": _content_parts_to_responses(content, role=wire_role),
        })
    return out


def _tools_to_responses(tools: Optional[list]) -> Optional[List[dict]]:
    if not tools:
        return None
    out = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        if t.get("type") == "function":
            fn = t.get("function") or {}
            out.append({
                "type": "function",
                "name": fn.get("name") or "",
                "description": fn.get("description") or "",
                "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
            })
        elif t.get("name"):
            out.append({
                "type": "function",
                "name": t.get("name"),
                "description": t.get("description") or "",
                "parameters": t.get("parameters") or {"type": "object", "properties": {}},
            })
    return out or None


def _codex_stream_terminal_fields(raw: dict, finish: str, err_msg: Optional[str]) -> dict:
    """Authoritative stream stamps so receipts do not infer wire_mode=sync."""
    status = str(raw.get("status") or "")
    last_event = str(raw.get("last_provider_event") or "").strip()
    if finish == "content_filter":
        terminal = "content_filter"
        last_event = last_event or "response.incomplete"
    elif err_msg or status == "failed" or finish == "failed":
        terminal = "error"
        last_event = last_event or "response.failed"
    elif status == "incomplete" or finish == "incomplete":
        terminal = "incomplete"
        last_event = last_event or "response.incomplete"
    elif status == "completed" or finish in ("completed", "stop"):
        terminal = "stop"
        last_event = last_event or "response.completed"
    else:
        terminal = "incomplete"
        last_event = last_event or "response.incomplete"
    out = {
        "stream_started": True,
        "stream_terminal": terminal,
    }
    if last_event:
        out["last_provider_event"] = last_event
    return out


def _incomplete_reason(raw: dict) -> str:
    details = raw.get("incomplete_details")
    if isinstance(details, dict):
        return str(details.get("reason") or "").strip().lower()
    return ""


def _extract_text_and_tools(raw: dict) -> Tuple[str, list, str]:
    """Parse a Responses API JSON body into text, openai-shaped tool_calls, finish.

    Maps ``status=incomplete`` + ``incomplete_details.reason=content_filter`` to
    finish_reason ``content_filter`` (Hermes) so callers refuse instead of
    burning continuation retries.

    Message text for ``DriverResponse.text`` prefers ``final_answer`` and
    phase-less (legacy) items. Commentary and analysis are excluded — they
    stream via progress/reasoning callbacks and must not contaminate the
    final answer even when final text is empty.
    """
    text_parts: List[str] = []
    tool_calls: List[dict] = []
    saw_answer_item = False
    status = str(raw.get("status") or "")
    reason = _incomplete_reason(raw)
    if status == "incomplete" and reason == "content_filter":
        finish = "content_filter"
    elif status == "incomplete" and reason in ("max_output_tokens", "length"):
        finish = "incomplete"
    else:
        finish = status
    for item in raw.get("output") or []:
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        if itype == "message":
            # Reuse channel policy: commentary -> progress, analysis ->
            # reasoning, final_answer / phase-less -> answer.
            channel = _codex_channel_for_item("message", item.get("phase"))
            if channel != "answer":
                continue
            saw_answer_item = True
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("type") in (
                    "output_text", "text",
                ):
                    text_parts.append(str(part.get("text") or ""))
        elif itype == "function_call":
            tool_calls.append({
                "id": item.get("call_id") or item.get("id") or "",
                "type": "function",
                "function": {
                    "name": item.get("name") or "",
                    "arguments": item.get("arguments")
                    if isinstance(item.get("arguments"), str)
                    else json.dumps(item.get("arguments") or {}),
                },
            })
    # Legacy / SSE-assembled bodies may only populate output_text. Never fall
    # back to it when answer-phase items existed (even if their text is empty)
    # — that would re-introduce commentary contamination via a mixed blob.
    if not text_parts and not saw_answer_item and isinstance(
        raw.get("output_text"), str
    ):
        text_parts.append(raw["output_text"])
    return "".join(text_parts), tool_calls, finish


def _codex_tool_hint_goal(arguments: Any, name: str) -> str:
    """Best-effort display goal from a function_call arguments blob."""
    args: Any = arguments
    if isinstance(args, str):
        raw = args.strip()
        if not raw:
            return ""
        try:
            args = json.loads(raw)
        except Exception:
            return raw[:200]
    if not isinstance(args, dict):
        return ""
    for key in (
        "goal", "command", "path", "query", "pattern", "url",
        "instruction", "prompt", "file",
    ):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()[:200]
    # Nested arguments bag (native tool shape).
    nested = args.get("arguments")
    if isinstance(nested, dict):
        return _codex_tool_hint_goal(nested, name)
    return ""


def _codex_continuation_kind(finish: str, text: str, tool_calls: list) -> Optional[str]:
    """Return ``nudge`` / ``length`` when the turn should continue, else None."""
    if finish == "content_filter":
        return None
    if finish != "incomplete":
        return None
    if tool_calls:
        return None
    if (text or "").strip():
        return "length"
    return "nudge"


def _user_input_item(text: str) -> dict:
    return {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": text}],
    }


def _usage_ints(usage: Any) -> Tuple[int, int]:
    from .token_usage import coerce_token_usage
    tin, tout, _cost = coerce_token_usage(usage)
    return tin, tout


def _usage_cost(usage: Any) -> Any:
    from .token_usage import coerce_token_usage
    _tin, _tout, cost = coerce_token_usage(usage)
    return cost


def _codex_channel_for_item(itype: str, phase_raw: Any) -> Optional[str]:
    """Map a Codex output item to a stable channel (never by arrival order).

    Routing policy:
      commentary  -> progress (visible assistant/progress stream)
      final_answer -> answer
      analysis / reasoning_* -> reasoning
      function_call -> tool
    """
    kind = (itype or "").strip().lower()
    if "function_call" in kind:
        return "tool"
    if kind == "reasoning" or kind.startswith("reasoning"):
        return "reasoning"
    if kind == "message":
        phase = phase_raw.strip().lower() if isinstance(phase_raw, str) else ""
        if phase == "commentary":
            return "progress"
        if phase == "analysis":
            return "reasoning"
        if phase == "final_answer":
            return "answer"
        # Message without a phase is visible answer prose.
        return "answer"
    return None


def _codex_stream_id(item_id: Any, output_index: Any) -> str:
    if isinstance(item_id, str) and item_id.strip():
        return item_id.strip()
    if output_index is not None:
        try:
            return f"out-{int(output_index)}"
        except (TypeError, ValueError):
            pass
    return ""


def _safe_cb(cb: Optional[Callable[..., None]], payload: Any) -> None:
    if cb is None or payload is None:
        return
    try:
        cb(payload)
    except Exception:
        pass


def _delta_payload(
    text: str,
    *,
    stream_id: str = "",
    output_index: Any = None,
    channel: str = "",
) -> Any:
    """Rich identity payload when stream identity is known; plain str otherwise."""
    # Keep legacy ``on_delta(str)`` callers working for identity-less answer
    # tokens. Progress/reasoning always carry a channel so the send loop can
    # route them without treating arrival order as ownership.
    rich = bool(stream_id) or output_index is not None or channel in {
        "progress", "reasoning",
    }
    if not rich:
        return text
    payload: Dict[str, Any] = {"text": text}
    if stream_id:
        payload["stream_id"] = stream_id
    if channel:
        payload["channel"] = channel
    if output_index is not None:
        try:
            payload["output_index"] = int(output_index)
        except (TypeError, ValueError):
            pass
    return payload


def _arm_post_answer_idle_timeout(resp_fp, seconds=_POST_ANSWER_SLICE_SECONDS):
    """Arm a short read timeout so keepalives cannot hold the runner open.

    Walks urllib / http.client wrappers to the socket. Returns True when a
    timeout was set. Lists and test iterators have no socket (returns False).
    """
    candidates = [resp_fp]
    seen = set()
    while candidates:
        obj = candidates.pop()
        ident = id(obj)
        if ident in seen or obj is None:
            continue
        seen.add(ident)
        setter = getattr(obj, "settimeout", None)
        if callable(setter):
            try:
                setter(seconds)
                return True
            except Exception:
                pass
        for attr in ("fp", "raw", "_sock", "socket"):
            try:
                inner = getattr(obj, attr, None)
            except Exception:
                inner = None
            if inner is not None:
                candidates.append(inner)
    return False


def _is_live_http_fp(resp_fp):
    return hasattr(resp_fp, "fp") or hasattr(resp_fp, "raw")


def _consume_codex_sse(
    resp_fp,
    *,
    on_delta: Optional[Callable[..., None]] = None,
    on_reasoning_delta: Optional[Callable[..., None]] = None,
    on_stream_item_done: Optional[Callable[..., None]] = None,
    chatgpt_backend: bool = True,
    stream_label: Optional[str] = None,
    allow_post_answer_idle: bool = True,
) -> dict:
    """Consume Codex Responses SSE; return a synthetic Responses-shaped dict.

    Mirrors Hermes ``_consume_codex_event_stream``: assemble from
    ``output_item.done`` + ``output_text.delta``; ignore terminal ``response.output``.

    Channel ownership is keyed by ``item_id`` / ``output_index`` — never by the
    most-recently-added item. Commentary is visible progress; analysis/reasoning
    stay on the reasoning stream; final_answer is the answer stream.

    The post-answer idle drain (forge ``completed`` after 0.5s idle / 2.0s
    total) is ChatGPT-only and only for a tool-free first-shot answer that
    already looks finished. Tool-result follow-ups and hanging clauses
    (``Cloudflare's:``) wait for ``response.completed`` / ``incomplete`` /
    ``failed``. Non-ChatGPT Responses hosts never idle-drain.
    """
    label = stream_label or responses_stream_label(
        chatgpt_backend=chatgpt_backend,
    )
    collected_items: List[dict] = []
    text_deltas: List[str] = []
    reasoning_deltas: List[str] = []
    has_tool_calls = False
    phase_by_item_id: Dict[str, str] = {}
    phase_by_output_index: Dict[int, str] = {}
    stream_id_by_output_index: Dict[int, str] = {}
    # Only for identity-less items (fixtures / odd providers). Cleared as soon
    # as an item with item_id/output_index is remembered so arrival order can
    # never own interleaved dual-channel deltas.
    fallback_channel: Optional[str] = None
    terminal_status = "completed"
    terminal_usage: Any = None
    terminal_error: Any = None
    terminal_model: Optional[str] = None
    terminal_incomplete_details: Any = None
    saw_terminal = False
    stream_error: Optional[str] = None
    answer_item_done = False
    last_provider_event = ""

    def _remember_item(
        item: dict,
        *,
        output_index: Any = None,
    ) -> Tuple[str, str]:
        itype = str(item.get("type") or "")
        channel = _codex_channel_for_item(itype, item.get("phase")) or ""
        item_id = item.get("id") or item.get("item_id")
        sid = _codex_stream_id(item_id, output_index)
        if isinstance(item_id, str) and item_id.strip() and channel:
            phase_by_item_id[item_id.strip()] = channel
        oi_int: Optional[int] = None
        if output_index is not None:
            try:
                oi_int = int(output_index)
            except (TypeError, ValueError):
                oi_int = None
        if oi_int is not None:
            if channel:
                phase_by_output_index[oi_int] = channel
            if sid:
                stream_id_by_output_index[oi_int] = sid
        return sid, channel

    def _resolve_channel(
        *,
        item_id: Any = None,
        output_index: Any = None,
    ) -> Tuple[str, str, Any]:
        sid = _codex_stream_id(item_id, output_index)
        channel = ""
        if isinstance(item_id, str) and item_id.strip():
            channel = phase_by_item_id.get(item_id.strip(), "")
        oi_int: Optional[int] = None
        if output_index is not None:
            try:
                oi_int = int(output_index)
            except (TypeError, ValueError):
                oi_int = None
        if not channel and oi_int is not None:
            channel = phase_by_output_index.get(oi_int, "")
        if not sid and oi_int is not None:
            sid = stream_id_by_output_index.get(oi_int, "") or _codex_stream_id(None, oi_int)
        return sid, channel, oi_int

    def _seal_open_channels(channels, except_sid):
        if on_stream_item_done is None:
            return
        want = set(channels)
        for prev_sid, prev_ch in list(phase_by_item_id.items()):
            if prev_ch in want and prev_sid and prev_sid != except_sid:
                _safe_cb(on_stream_item_done, {"stream_id": prev_sid})
        for oi, prev_ch in list(phase_by_output_index.items()):
            if prev_ch not in want:
                continue
            prev_sid = stream_id_by_output_index.get(oi, "")
            if prev_sid and prev_sid != except_sid:
                _safe_cb(on_stream_item_done, {"stream_id": prev_sid})

    post_answer_drain = False
    drain_started = 0.0
    last_meaningful = 0.0
    keepalive_streak = 0

    def _finish_tool_free_answer():
        nonlocal saw_terminal, terminal_status
        if not chatgpt_backend:
            return
        saw_terminal = True
        terminal_status = "completed"
        _seal_open_channels(("progress", "reasoning", "answer"), "")

    def _idle_drain_allowed():
        if not chatgpt_backend or not allow_post_answer_idle:
            return False
        if has_tool_calls:
            return False
        if answer_looks_incomplete("".join(text_deltas)):
            return False
        return True

    def _note_answer_text():
        """Only answer tokens extend the idle clock. Trailing summary must not."""
        nonlocal last_meaningful, keepalive_streak
        last_meaningful = time.monotonic()
        keepalive_streak = 0

    def _should_finish_drain():
        if not _idle_drain_allowed() or not post_answer_drain:
            return False
        now = time.monotonic()
        if now - last_meaningful >= _POST_ANSWER_IDLE_SECONDS:
            return True
        if now - drain_started >= _POST_ANSWER_MAX_SECONDS:
            return True
        return False

    def _drain_tick():
        if _should_finish_drain():
            _finish_tool_free_answer()
            return True
        return False

    def _begin_post_answer_drain():
        """Start the short post-answer drain. False means caller should stop."""
        nonlocal post_answer_drain, drain_started, last_meaningful
        nonlocal keepalive_streak
        if not chatgpt_backend:
            return True
        if not _idle_drain_allowed():
            return True
        if post_answer_drain:
            return True
        post_answer_drain = True
        drain_started = time.monotonic()
        last_meaningful = drain_started
        keepalive_streak = 0
        armed = _arm_post_answer_idle_timeout(resp_fp, _POST_ANSWER_SLICE_SECONDS)
        if not armed and _is_live_http_fp(resp_fp):
            # Cannot wake from keepalives — finish now rather than hang.
            _finish_tool_free_answer()
            return False
        return True

    line_iter = iter(resp_fp)
    while True:
        try:
            raw_line = next(line_iter)
        except StopIteration:
            break
        except (TimeoutError, socket.timeout):
            if (
                _idle_drain_allowed()
                and (answer_item_done or text_deltas or collected_items)
            ):
                _finish_tool_free_answer()
                break
            if not collected_items and not text_deltas:
                return {
                    "status": "failed",
                    "output": [],
                    "output_text": "",
                    "usage": {},
                    "error": responses_stream_error(
                        label, "timeout", last_provider_event,
                    ),
                }
            break

        line = raw_line.decode("utf-8", "replace").strip() if isinstance(raw_line, bytes) else str(raw_line).strip()
        if not line or not line.startswith("data:"):
            if post_answer_drain and _idle_drain_allowed():
                keepalive_streak += 1
                if keepalive_streak >= _POST_ANSWER_KEEPALIVE_LIMIT or _should_finish_drain():
                    _finish_tool_free_answer()
                    break
            continue
        data_str = line[5:].strip()
        if not data_str or data_str == "[DONE]":
            if data_str == "[DONE]":
                break
            if post_answer_drain and _idle_drain_allowed():
                keepalive_streak += 1
                if keepalive_streak >= _POST_ANSWER_KEEPALIVE_LIMIT or _should_finish_drain():
                    _finish_tool_free_answer()
                    break
            continue
        try:
            event = json.loads(data_str)
        except Exception:
            continue
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type") or "")
        if event_type:
            last_provider_event = event_type

        if event_type == "error":
            stream_error = str(
                event.get("message") or event.get("error") or "stream error"
            )[:800]
            break

        if event_type == "response.output_item.added":
            item = event.get("item") or {}
            if isinstance(item, dict):
                out_idx = event.get("output_index")
                if out_idx is None:
                    out_idx = item.get("output_index")
                sid, channel = _remember_item(item, output_index=out_idx)
                if sid:
                    fallback_channel = None
                else:
                    fallback_channel = channel or None
                itype = str(item.get("type") or "")
                if "function_call" in itype:
                    has_tool_calls = True
                # Final-answer item start seals open progress AND reasoning
                # so a short reply cannot leave thinking streaming:true while
                # Codex keeps the SSE open for a trailing summary.
                if channel == "answer":
                    _seal_open_channels(("progress", "reasoning"), sid)
            if _drain_tick():
                break
            continue

        if "output_text.delta" in event_type or event_type == "response.output_text.delta":
            delta_text = event.get("delta") or ""
            if not isinstance(delta_text, str) or not delta_text:
                continue
            item_id = event.get("item_id") or event.get("id")
            out_idx = event.get("output_index")
            sid, channel, oi_int = _resolve_channel(
                item_id=item_id, output_index=out_idx,
            )
            if not channel and fallback_channel:
                channel = fallback_channel
            # Identity-less deltas (legacy fixtures) stay on the answer stream.
            if not channel:
                channel = "answer"
            payload = _delta_payload(
                delta_text,
                stream_id=sid,
                output_index=oi_int if oi_int is not None else out_idx,
                channel=channel,
            )
            if channel == "reasoning":
                _safe_cb(on_reasoning_delta, payload)
            elif channel == "progress":
                # Visible progress prose — not part of final answer assembly.
                _safe_cb(on_delta, payload)
            elif channel == "tool":
                has_tool_calls = True
            else:
                text_deltas.append(delta_text)
                _note_answer_text()
                if not _begin_post_answer_drain():
                    break
                # Suppress anonymous mid-tool answer crumbs (legacy JSON
                # envelopes). Identity-bearing final_answer streams must still
                # paint after function_call items.
                if not has_tool_calls or bool(sid) or channel == "answer":
                    _safe_cb(on_delta, payload)
            if _drain_tick():
                break
            continue

        if "function_call" in event_type:
            has_tool_calls = True

        if "reasoning" in event_type and "delta" in event_type:
            reasoning_text = event.get("delta") or ""
            if isinstance(reasoning_text, str) and reasoning_text:
                reasoning_deltas.append(reasoning_text)
                item_id = event.get("item_id") or event.get("id")
                out_idx = event.get("output_index")
                sid, channel, oi_int = _resolve_channel(
                    item_id=item_id, output_index=out_idx,
                )
                if not channel:
                    channel = "reasoning"
                _safe_cb(
                    on_reasoning_delta,
                    _delta_payload(
                        reasoning_text,
                        stream_id=sid,
                        output_index=oi_int if oi_int is not None else out_idx,
                        channel="reasoning",
                    ),
                )
            if _drain_tick():
                break
            continue

        if event_type in ("response.output_item.done", "response.output_text.done"):
            done_item = event.get("item")
            sid = ""
            _channel = ""
            if isinstance(done_item, dict):
                collected_items.append(done_item)
                out_idx = event.get("output_index")
                if out_idx is None:
                    out_idx = done_item.get("output_index")
                sid, _channel = _remember_item(done_item, output_index=out_idx)
                if not sid:
                    sid = _codex_stream_id(
                        done_item.get("id") or done_item.get("item_id"),
                        out_idx,
                    )
            else:
                item_id = event.get("item_id") or event.get("id")
                out_idx = event.get("output_index")
                sid, _channel, _oi = _resolve_channel(
                    item_id=item_id, output_index=out_idx,
                )
                if not _channel:
                    _channel = "answer"
            if sid:
                _safe_cb(on_stream_item_done, {"stream_id": sid})
            if _channel == "answer" or (
                event_type == "response.output_text.done"
                and bool(text_deltas)
                and _channel not in ("progress", "tool")
            ):
                answer_item_done = True
                if not _begin_post_answer_drain():
                    break
            if _drain_tick():
                break
            continue

        if event_type in _TERMINAL_EVENT_TYPES:
            saw_terminal = True
            resp_obj = event.get("response")
            if isinstance(resp_obj, dict):
                terminal_usage = resp_obj.get("usage")
                rstatus = resp_obj.get("status")
                if isinstance(rstatus, str) and rstatus:
                    terminal_status = rstatus
                mid = resp_obj.get("model")
                if isinstance(mid, str) and mid.strip():
                    terminal_model = mid.strip()
                details = resp_obj.get("incomplete_details")
                if details is not None:
                    terminal_incomplete_details = details
                if event_type == "response.failed":
                    terminal_error = resp_obj.get("error") or resp_obj
            if event_type == "response.completed":
                terminal_status = terminal_status or "completed"
            elif event_type == "response.incomplete":
                terminal_status = terminal_status or "incomplete"
            elif event_type == "response.failed":
                terminal_status = terminal_status or "failed"
            break

        # Luna keeps the SSE open with in_progress / heartbeat JSON after the
        # visible answer. Those are data: events, so comment-streak never fires.
        if _drain_tick():
            break

    if stream_error:
        return {
            "status": "failed",
            "output": [],
            "output_text": "",
            "usage": {},
            "error": stream_error,
            "last_provider_event": last_provider_event or "error",
        }

    if collected_items:
        output = collected_items
    elif text_deltas and not has_tool_calls:
        assembled = "".join(text_deltas)
        output = [{
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": assembled}],
        }]
    else:
        output = []

    if not saw_terminal and not output:
        return {
            "status": "failed",
            "output": [],
            "output_text": "",
            "usage": {},
            "error": responses_stream_error(
                label, "no_terminal", last_provider_event,
            ),
            "last_provider_event": last_provider_event,
        }

    assembled_text = "".join(text_deltas)
    err_msg = None
    if terminal_status == "failed":
        if isinstance(terminal_error, dict):
            err_msg = str(
                terminal_error.get("message")
                or terminal_error.get("detail")
                or terminal_error
            )[:800]
        elif terminal_error:
            err_msg = str(terminal_error)[:800]
        else:
            err_msg = responses_stream_error(label, "failed")
    if not saw_terminal and not chatgpt_backend:
        terminal_status = "incomplete"
        if not err_msg:
            err_msg = responses_stream_error(
                label, "ended", last_provider_event,
            )
    elif not saw_terminal and chatgpt_backend:
        assembled_preview = "".join(text_deltas)
        if (
            not allow_post_answer_idle
            or answer_looks_incomplete(assembled_preview)
        ) and (assembled_preview or output):
            terminal_status = "incomplete"

    out = {
        "status": terminal_status,
        "output": output,
        "output_text": assembled_text,
        "reasoning": "".join(reasoning_deltas),
        "usage": terminal_usage if isinstance(terminal_usage, dict) else {},
        "error": err_msg,
        "model": terminal_model,
        "last_provider_event": last_provider_event,
    }
    if terminal_incomplete_details is not None:
        out["incomplete_details"] = terminal_incomplete_details
    return out


class CodexResponsesDriver:
    # ChatGPT Codex backend requires stream=true; expose real SSE to the pilot.
    supports_streaming = True
    # Network driver: never accept implicit JSON-envelope natural.
    requires_explicit_terminal = True

    def __init__(
        self,
        name: str,
        model: str,
        *,
        base_url: str = DEFAULT_CODEX_BASE,
        api_key_env: str = "OPENAI_CODEX_TOKEN",
        max_tokens: int = 4096,
        timeout: int = 120,
        chatgpt_backend: bool = True,
    ) -> None:
        self.name = name
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.max_tokens = max_tokens
        self.timeout = timeout
        # ChatGPT's Codex backend needs Cloudflare/originator headers and
        # accepts its own session/thread identity fields. A plain OpenAI
        # Responses host (e.g. OpenCode Go's /v1/responses) rejects those
        # extras as unknown parameters, so they are opt-out per host.
        self.chatgpt_backend = chatgpt_backend
        self._pool_provider: Optional[str] = None
        self._pool_entry_id: Optional[str] = None

    def _billing_meta(self) -> Dict[str, str]:
        """Receipt api_mode/billing derived from the host, not the driver class."""
        if self.chatgpt_backend:
            return {"api_mode": "codex_responses", "billing": "plan"}
        return {"api_mode": "responses", "billing": "api"}

    def _key(self) -> str:
        self._pool_provider = None
        self._pool_entry_id = None
        try:
            from harness.credential_pool import provider_for_env_var, resolve_entry
            prov = provider_for_env_var(self.api_key_env) or "openai-codex"
            entry = resolve_entry(prov)
            if entry is not None and entry.runtime_token:
                self._pool_provider = prov
                self._pool_entry_id = entry.id
                if entry.base_url or (entry.extra or {}).get("base_url"):
                    self.base_url = str(
                        entry.base_url or entry.extra.get("base_url")
                    ).rstrip("/")
                return entry.runtime_token
        except Exception:
            pass
        key = os.environ.get(self.api_key_env, "").strip()
        if not key:
            raise RuntimeError(f"missing Codex OAuth token ({self.api_key_env})")
        return key

    def _pool_rotate_on_http_error(self, code: int, detail: str) -> Optional[str]:
        if not self._pool_provider or not self._pool_entry_id:
            return None
        if code not in (401, 402, 429):
            return None
        try:
            from harness.credential_pool import report_failure
            nxt = report_failure(
                self._pool_provider,
                self._pool_entry_id,
                status_code=code,
                message=detail or "",
            )
            if nxt:
                self._key()
                return nxt
        except Exception:
            pass
        return None

    def _request_headers(self, access_token: str) -> Dict[str, str]:
        """Auth + transport headers for this host's Responses endpoint."""
        if self.chatgpt_backend:
            return _codex_cloudflare_headers(access_token, streaming=True)
        return {
            "User-Agent": "pm-harness",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
            "Accept": "text/event-stream",
        }

    def _build_body(
        self,
        messages: list,
        *,
        tools: list | None = None,
        system: str | None = None,
        session_id: str | None = None,
    ) -> Dict[str, Any]:
        instructions = system or SYSTEM_PROMPT
        payload_messages = list(messages or [])
        if payload_messages and payload_messages[0].get("role") == "system":
            instructions = str(payload_messages[0].get("content") or instructions)
            payload_messages = payload_messages[1:]
        # ChatGPT Codex backend rejects max_output_tokens (HTTP 400
        # "Unsupported parameter"); Hermes omits it when is_codex_backend.
        # Non-ChatGPT Responses hosts (OpenCode Go) honor the stored cap.
        #
        # Request a reasoning summary so the pilot UI can leave
        # "Waiting on provider…" and paint Thought while gpt-5.x thinks.
        # Effort comes from HARNESS_CODEX_REASONING_EFFORT (settings JSON);
        # default is low. None omits the reasoning block entirely.
        from harness.reasoning_effort import codex_api_effort, current_reasoning_effort

        api_effort = codex_api_effort(current_reasoning_effort())
        body: Dict[str, Any] = {
            "model": self.model,
            "instructions": instructions,
            "input": _messages_to_responses_input(payload_messages),
            "store": False,
            "stream": True,  # required by chatgpt.com/backend-api/codex
        }
        if (
            not self.chatgpt_backend
            and isinstance(self.max_tokens, int)
            and self.max_tokens > 0
        ):
            body["max_output_tokens"] = int(self.max_tokens)
        if api_effort:
            body["reasoning"] = {"effort": api_effort, "summary": "auto"}
        resp_tools = _tools_to_responses(tools)
        if resp_tools:
            body["tools"] = resp_tools
            body["tool_choice"] = "auto"
            body["parallel_tool_calls"] = True
        # Durable UUID prompt_cache_key + capability-gated GPT-5.6 explicit
        # breakpoint at the end of the stable instructions/tools prefix.
        # See pmharness.drivers.prompt_cache.apply_codex_responses_prompt_cache.
        try:
            from .prompt_cache import apply_codex_responses_prompt_cache

            apply_codex_responses_prompt_cache(
                body,
                model=self.model,
                session_id=session_id,
                base_url=self.base_url,
            )
        except Exception:
            # Keep the same durable key as the primary path — never fall
            # back to the raw Marionette session id (that splits the cache).
            # Honor the global kill switch even on the exception path.
            try:
                from .prompt_cache import (
                    durable_codex_prompt_cache_key,
                    prompt_cache_enabled,
                )

                if prompt_cache_enabled():
                    key = durable_codex_prompt_cache_key(session_id)
                    if key:
                        body["prompt_cache_key"] = key
                else:
                    body.pop("prompt_cache_key", None)
            except Exception:
                pass
        client_metadata = (
            _codex_client_metadata(body.get("prompt_cache_key"))
            if self.chatgpt_backend
            else {}
        )
        if client_metadata:
            body["client_metadata"] = client_metadata
        else:
            body.pop("client_metadata", None)
        return body

    def _one_stream_attempt(
        self,
        body: dict,
        data: bytes,
        *,
        on_delta: Optional[Callable[..., None]],
        on_reasoning_delta: Optional[Callable[..., None]],
        on_stream_item_done: Optional[Callable[..., None]] = None,
        t0: float,
    ) -> Tuple[Optional[dict], Optional[DriverResponse], bytes]:
        """POST once (with reasoning-strip / pool rotate). Returns (raw, err_resp, data)."""
        for attempt in range(3):
            token = self._key()
            headers = self._request_headers(token)
            if self.chatgpt_backend:
                headers.update(
                    _codex_session_affinity_headers(body.get("prompt_cache_key"))
                )
            try:
                req = urllib.request.Request(
                    f"{self.base_url}/responses",
                    data=data,
                    headers=headers,
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = _consume_codex_sse(
                        resp,
                        on_delta=on_delta,
                        on_reasoning_delta=on_reasoning_delta,
                        on_stream_item_done=on_stream_item_done,
                        chatgpt_backend=self.chatgpt_backend,
                        stream_label=responses_stream_label(
                            chatgpt_backend=self.chatgpt_backend,
                            base_url=self.base_url,
                        ),
                        allow_post_answer_idle=not responses_input_has_tool_results(body),
                    )
                return raw, None, data
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:800]
                low = detail.lower()
                if (
                    attempt < 2
                    and e.code == 400
                    and "reasoning" in low
                    and body.get("reasoning") is not None
                ):
                    body.pop("reasoning", None)
                    data = json.dumps(body).encode("utf-8")
                    continue
                # ChatGPT Codex may reject public OpenAI GPT-5.6 breakpoint
                # fields while still accepting prompt_cache_key. Strip the
                # extensions and retry once without inventing cache hits.
                # Only strip when the error names those fields — unrelated
                # HTTP 400s must surface unchanged.
                if attempt < 2 and e.code == 400:
                    try:
                        from .prompt_cache import (
                            body_has_codex_prompt_cache_extensions,
                            codex_prompt_cache_unsupported_error,
                            mark_codex_prompt_cache_breakpoint_unsupported,
                            strip_codex_prompt_cache_extensions,
                        )

                        if (
                            body_has_codex_prompt_cache_extensions(body)
                            and codex_prompt_cache_unsupported_error(detail)
                        ):
                            strip_codex_prompt_cache_extensions(body)
                            mark_codex_prompt_cache_breakpoint_unsupported(
                                self.base_url, self.model,
                            )
                            data = json.dumps(body).encode("utf-8")
                            continue
                    except Exception:
                        pass
                if attempt == 0:
                    nxt = self._pool_rotate_on_http_error(e.code, detail)
                    if nxt:
                        continue
                return None, DriverResponse(
                    text="", model=self.name,
                    error=f"HTTP {e.code}: {detail}",
                    latency_ms=(time.time() - t0) * 1000.0,
                ), data
            except Exception as e:
                return None, DriverResponse(
                    text="", model=self.name, error=repr(e),
                    latency_ms=(time.time() - t0) * 1000.0,
                ), data
        return None, DriverResponse(
            text="", model=self.name, error="empty response",
            latency_ms=(time.time() - t0) * 1000.0,
        ), data

    def _response_from_raw(
        self,
        raw: dict,
        *,
        t0: float,
        incomplete_retries: int = 0,
    ) -> DriverResponse:
        text, tool_calls, finish = _extract_text_and_tools(raw)
        if not text and isinstance(raw.get("output_text"), str):
            text = raw["output_text"]
        if finish == "content_filter":
            meta = {
                **self._billing_meta(),
                "finish_reason": "content_filter",
                "requested_model": self.model,
            }
            meta.update(_codex_stream_terminal_fields(raw, finish, _CONTENT_FILTER_MSG))
            return DriverResponse(
                text="",
                model=self.name,
                error=_CONTENT_FILTER_MSG,
                latency_ms=(time.time() - t0) * 1000.0,
                meta=meta,
            )
        usage = raw.get("usage") or {}
        tin, tout = _usage_ints(usage)
        from .openai_compat import OpenAICompatDriver
        from .token_usage import attach_modality_fields, coerce_token_usage_record

        usage_detail = coerce_token_usage_record(usage)
        meta = {
            "tool_calls": tool_calls,
            "finish_reason": finish,
            "raw_usage": usage,
            **self._billing_meta(),
            "requested_model": self.model,
            "incomplete_retries": incomplete_retries,
        }
        reasoning = raw.get("reasoning")
        if isinstance(reasoning, str) and reasoning.strip():
            meta["reasoning"] = reasoning
        attach_modality_fields(meta, usage_detail)
        # Match OpenAI-compat: keep explicit provider zeros; omit absent fields.
        # Never infer cache hits or writes from totals alone.
        if OpenAICompatDriver._usage_reports_cache_read(usage):
            meta["cache_read_tokens"] = int(usage_detail.cache_read)
        if OpenAICompatDriver._usage_reports_cache_write(usage):
            meta["cache_write_tokens"] = int(usage_detail.cache_write)
        reason = _incomplete_reason(raw)
        if reason:
            meta["incomplete_reason"] = reason
        cost = _usage_cost(usage)
        if cost is not None:
            meta["provider_cost_usd"] = cost
        served = raw.get("model")
        if isinstance(served, str) and served.strip():
            meta["served_model"] = served.strip()
        raw_error = raw.get("error")
        err_msg = str(raw_error) if raw_error else None
        meta.update(_codex_stream_terminal_fields(raw, finish, err_msg))
        return DriverResponse(
            text=text,
            tokens_in=tin,
            tokens_out=tout,
            latency_ms=(time.time() - t0) * 1000.0,
            model=self.name,
            error=err_msg,
            meta=meta,
        )

    def _post_stream(
        self,
        body: dict,
        *,
        on_delta: Optional[Callable[..., None]] = None,
        on_reasoning_delta: Optional[Callable[..., None]] = None,
        on_stream_item_done: Optional[Callable[..., None]] = None,
        on_wait_notice: Optional[Callable[[str], None]] = None,
    ) -> DriverResponse:
        # Enforce stream even if a caller mutated the body.
        body = dict(body)
        body["stream"] = True
        try:
            from .prompt_cache import codex_request_cache_snapshot

            initial_request_cache = codex_request_cache_snapshot(body)
        except Exception:
            initial_request_cache = {
                "prompt_cache_key_present": False,
                "prompt_cache_key": None,
                "prompt_cache_options": None,
                "explicit_breakpoint": False,
            }
        data = json.dumps(body).encode("utf-8")

        def _attach_request_cache_diagnostics(resp: DriverResponse) -> DriverResponse:
            """Stamp sanitized initial/final cache wire state onto meta/receipt."""
            try:
                from .prompt_cache import codex_request_cache_snapshot

                final_request_cache = codex_request_cache_snapshot(body)
            except Exception:
                final_request_cache = {
                    "prompt_cache_key_present": False,
                    "prompt_cache_key": None,
                    "prompt_cache_options": None,
                    "explicit_breakpoint": False,
                }
            meta = resp.meta if isinstance(resp.meta, dict) else {}
            existing = meta.get("prompt_cache")
            pc = dict(existing) if isinstance(existing, dict) else {}
            pc["initial"] = initial_request_cache
            pc["final"] = final_request_cache
            meta["prompt_cache"] = pc
            key = final_request_cache.get("prompt_cache_key")
            if isinstance(key, str) and key:
                meta["prompt_cache_key"] = key
            resp.meta = meta
            return resp

        def _call() -> DriverResponse:
            t0 = time.time()
            nonlocal data, body
            length_parts: List[str] = []
            incomplete_retries = 0
            # Provider-reported usage across incomplete/length continuations.
            # Sum only what the backend returned — never invent cache hits.
            # Presence flags preserve explicit zeros vs absent fields.
            acc_tin = 0
            acc_tout = 0
            acc_cache_read = 0
            acc_cache_write = 0
            have_cache_read = False
            have_cache_write = False
            acc_cost = 0.0
            have_cost = False

            def _accumulate_attempt(resp: DriverResponse) -> None:
                nonlocal acc_tin, acc_tout, acc_cache_read, acc_cache_write
                nonlocal have_cache_read, have_cache_write
                nonlocal acc_cost, have_cost
                acc_tin += int(resp.tokens_in or 0)
                acc_tout += int(resp.tokens_out or 0)
                meta = resp.meta or {}
                if "cache_read_tokens" in meta:
                    have_cache_read = True
                    acc_cache_read += int(meta.get("cache_read_tokens") or 0)
                if "cache_write_tokens" in meta:
                    have_cache_write = True
                    acc_cache_write += int(meta.get("cache_write_tokens") or 0)
                cost = meta.get("provider_cost_usd")
                if cost is not None:
                    try:
                        acc_cost += float(cost)
                        have_cost = True
                    except (TypeError, ValueError):
                        pass

            def _final_meta(base: dict, *, retries: Optional[int] = None) -> dict:
                meta = dict(base or {})
                meta["incomplete_retries"] = (
                    incomplete_retries if retries is None else int(retries)
                )
                if have_cache_read:
                    meta["cache_read_tokens"] = acc_cache_read
                else:
                    meta.pop("cache_read_tokens", None)
                if have_cache_write:
                    meta["cache_write_tokens"] = acc_cache_write
                else:
                    meta.pop("cache_write_tokens", None)
                if have_cost:
                    meta["provider_cost_usd"] = acc_cost
                return meta

            def _has_continuation_progress() -> bool:
                # Prior incomplete/length attempts already contributed usage
                # or partial text — a later failure must not drop that.
                return (
                    incomplete_retries > 0
                    or bool(length_parts)
                    or acc_tin > 0
                    or acc_tout > 0
                    or have_cache_read
                    or have_cache_write
                    or have_cost
                )

            def _error_preserving_progress(
                *,
                error: str,
                latency_ms: float,
                base_meta: Optional[dict] = None,
            ) -> DriverResponse:
                meta = {
                    **self._billing_meta(),
                    "requested_model": self.model,
                    # Prevent with_retry from replaying a body already mutated
                    # with continuation nudges as if it were the original turn.
                    "stream_started": True,
                }
                if base_meta:
                    for key in ("finish_reason", "incomplete_reason", "served_model"):
                        if key in base_meta and base_meta[key] is not None:
                            meta[key] = base_meta[key]
                return DriverResponse(
                    text="".join(length_parts),
                    tokens_in=acc_tin,
                    tokens_out=acc_tout,
                    model=self.name,
                    error=error,
                    latency_ms=latency_ms,
                    meta=_final_meta(meta),
                )

            while True:
                raw, err_resp, data = self._one_stream_attempt(
                    body,
                    data,
                    on_delta=on_delta,
                    on_reasoning_delta=on_reasoning_delta,
                    on_stream_item_done=on_stream_item_done,
                    t0=t0,
                )
                if err_resp is not None:
                    if _has_continuation_progress():
                        return _attach_request_cache_diagnostics(
                            _error_preserving_progress(
                                error=err_resp.error or "stream error",
                                latency_ms=err_resp.latency_ms
                                or (time.time() - t0) * 1000.0,
                                base_meta=err_resp.meta,
                            )
                        )
                    return _attach_request_cache_diagnostics(err_resp)
                if raw is None:
                    if _has_continuation_progress():
                        return _attach_request_cache_diagnostics(
                            _error_preserving_progress(
                                error="empty response",
                                latency_ms=(time.time() - t0) * 1000.0,
                            )
                        )
                    return _attach_request_cache_diagnostics(
                        DriverResponse(
                            text="", model=self.name, error="empty response",
                            latency_ms=(time.time() - t0) * 1000.0,
                        )
                    )
                resp = self._response_from_raw(
                    raw, t0=t0, incomplete_retries=incomplete_retries,
                )
                if resp.error:
                    # Do not accumulate this attempt — its usage was never a
                    # successful provider turn — but keep prior sums/text.
                    if _has_continuation_progress():
                        return _attach_request_cache_diagnostics(
                            _error_preserving_progress(
                                error=resp.error,
                                latency_ms=resp.latency_ms
                                or (time.time() - t0) * 1000.0,
                                base_meta=resp.meta,
                            )
                        )
                    return _attach_request_cache_diagnostics(resp)
                _accumulate_attempt(resp)
                text = resp.text or ""
                tool_calls = (resp.meta or {}).get("tool_calls") or []
                finish = str((resp.meta or {}).get("finish_reason") or "")
                kind = _codex_continuation_kind(finish, text, tool_calls)
                # ChatGPT-only: incomplete continuation re-POSTs a nudge.
                # Non-ChatGPT Muse (and other Responses hosts) must return the
                # first preserved partial and never auto-POST a second stream.
                if kind is not None and not self.chatgpt_backend:
                    meta = _final_meta(resp.meta or {})
                    reason = str(meta.get("incomplete_reason") or finish or "incomplete")
                    err = resp.error or (
                        f"Codex response incomplete ({reason})"
                    )
                    if "stream_started" not in meta:
                        meta["stream_started"] = True
                    if "stream_terminal" not in meta:
                        meta["stream_terminal"] = "incomplete"
                    return _attach_request_cache_diagnostics(
                        DriverResponse(
                            text=text,
                            tokens_in=acc_tin,
                            tokens_out=acc_tout,
                            latency_ms=resp.latency_ms,
                            model=self.name,
                            error=err,
                            meta=meta,
                        )
                    )
                if kind is None:
                    final_text = "".join(length_parts) + text if length_parts else text
                    if incomplete_retries == 0 and final_text == text:
                        return _attach_request_cache_diagnostics(resp)
                    return _attach_request_cache_diagnostics(
                        DriverResponse(
                            text=final_text,
                            tokens_in=acc_tin,
                            tokens_out=acc_tout,
                            latency_ms=resp.latency_ms,
                            model=self.name,
                            meta=_final_meta(resp.meta or {}),
                        )
                    )

                incomplete_retries += 1
                if kind == "length" and text.strip():
                    length_parts.append(text)
                if incomplete_retries > _CODEX_MAX_INCOMPLETE_RETRIES:
                    # Report completed continuation attempts (MAX), not the
                    # post-increment sentinel that tripped exhaustion.
                    completed_retries = incomplete_retries - 1
                    meta = {
                        **self._billing_meta(),
                        "finish_reason": "incomplete",
                        "requested_model": self.model,
                    }
                    return _attach_request_cache_diagnostics(
                        DriverResponse(
                            text="".join(length_parts),
                            tokens_in=acc_tin,
                            tokens_out=acc_tout,
                            model=self.name,
                            error=(
                                "Codex response remained incomplete after "
                                f"{_CODEX_MAX_INCOMPLETE_RETRIES} continuation attempts"
                            ),
                            latency_ms=(time.time() - t0) * 1000.0,
                            meta=_final_meta(meta, retries=completed_retries),
                        )
                    )

                nudge = (
                    _CODEX_INCOMPLETE_NUDGE if kind == "nudge" else _CODEX_LENGTH_CONTINUE
                )
                if on_wait_notice is not None:
                    try:
                        why = (
                            "reasoning with no final answer"
                            if kind == "nudge"
                            else "a truncated answer"
                        )
                        on_wait_notice(
                            f"model returned {why} — asking it to continue "
                            f"({incomplete_retries}/{_CODEX_MAX_INCOMPLETE_RETRIES})"
                        )
                    except Exception:
                        pass
                inp = list(body.get("input") or [])
                last_text = ""
                last = inp[-1] if inp else None
                if isinstance(last, dict):
                    for part in last.get("content") or []:
                        if isinstance(part, dict):
                            last_text += str(part.get("text") or "")
                if last_text.strip() != nudge:
                    if kind == "length" and text.strip():
                        inp.append({
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": text}],
                        })
                    inp.append(_user_input_item(nudge))
                    body["input"] = inp
                    data = json.dumps(body).encode("utf-8")

        return with_retry(_call)

    def complete(
        self,
        task_prompt: str,
        *,
        system: str = SYSTEM_PROMPT,
        session_id: str | None = None,
    ) -> DriverResponse:
        # Optional session_id preserves prompt-cache affinity for callers that
        # represent the same pilot chat. Compaction / synthetic summarizers
        # must omit it — their system prefix differs from the main session.
        body = self._build_body(
            [{"role": "user", "content": task_prompt}],
            system=system,
            session_id=session_id,
        )
        return self._post_stream(body)

    def chat(
        self,
        messages: list,
        *,
        tools: list | None = None,
        system: str | None = None,
        session_id: str | None = None,
    ) -> DriverResponse:
        body = self._build_body(
            messages, tools=tools, system=system, session_id=session_id,
        )
        return self._post_stream(body)

    def chat_stream(
        self,
        messages: list,
        *,
        tools: list | None = None,
        system: str | None = None,
        on_delta: Callable[..., None],
        session_id: str | None = None,
        on_reasoning_delta: Callable[..., None] | None = None,
        on_stream_item_done: Callable[..., None] | None = None,
        on_tool_hint: Callable[[Any], None] | None = None,
        on_wait_notice: Callable[[str], None] | None = None,
    ) -> DriverResponse:
        body = self._build_body(
            messages, tools=tools, system=system, session_id=session_id,
        )
        # Tool names are available only after output_item.done; hint then.
        def _delta_and_hint(piece: Any) -> None:
            if on_delta is not None:
                on_delta(piece)

        resp = self._post_stream(
            body,
            on_delta=_delta_and_hint,
            on_reasoning_delta=on_reasoning_delta,
            on_stream_item_done=on_stream_item_done,
            on_wait_notice=on_wait_notice,
        )
        if on_tool_hint is not None:
            for tc in (resp.meta or {}).get("tool_calls") or []:
                fn = tc.get("function") or {}
                name = (fn.get("name") or "").strip()
                if not name:
                    continue
                # Prefer structured hints with call_id so tool_prep promotes
                # into the matching action_start instead of leaving anonymous
                # tool-prep:<kind> orphans that settle as "missing action_result".
                call_id = str(tc.get("id") or tc.get("call_id") or "").strip()
                goal = _codex_tool_hint_goal(fn.get("arguments"), name)
                try:
                    if call_id or goal:
                        hint: dict = {"name": name}
                        if call_id:
                            hint["id"] = call_id
                        if goal:
                            hint["goal"] = goal
                        on_tool_hint(hint)
                    else:
                        on_tool_hint(name)
                except Exception:
                    pass
        return resp
