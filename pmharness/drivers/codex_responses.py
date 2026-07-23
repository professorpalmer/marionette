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
import time
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
_CONTENT_FILTER_MSG = (
    "Model declined to respond (content filter). Try rephrasing the request "
    "or narrowing the context."
)


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


def _messages_to_responses_input(messages: List[dict]) -> List[dict]:
    """Minimal chat → Responses input conversion (text + tool stubs)."""
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
        text = content if isinstance(content, str) else (
            json.dumps(content) if content is not None else ""
        )
        part_type = "output_text" if role == "assistant" else "input_text"
        out.append({
            "type": "message",
            "role": "user" if role == "user" else role,
            "content": [{"type": part_type, "text": text}],
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


def _consume_codex_sse(
    resp_fp,
    *,
    on_delta: Optional[Callable[..., None]] = None,
    on_reasoning_delta: Optional[Callable[..., None]] = None,
    on_stream_item_done: Optional[Callable[..., None]] = None,
) -> dict:
    """Consume Codex Responses SSE; return a synthetic Responses-shaped dict.

    Mirrors Hermes ``_consume_codex_event_stream``: assemble from
    ``output_item.done`` + ``output_text.delta``; ignore terminal ``response.output``.

    Channel ownership is keyed by ``item_id`` / ``output_index`` — never by the
    most-recently-added item. Commentary is visible progress; analysis/reasoning
    stay on the reasoning stream; final_answer is the answer stream.
    """
    collected_items: List[dict] = []
    text_deltas: List[str] = []
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

    for raw_line in resp_fp:
        line = raw_line.decode("utf-8", "replace").strip() if isinstance(raw_line, bytes) else str(raw_line).strip()
        if not line or not line.startswith("data:"):
            continue
        data_str = line[5:].strip()
        if not data_str or data_str == "[DONE]":
            if data_str == "[DONE]":
                break
            continue
        try:
            event = json.loads(data_str)
        except Exception:
            continue
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type") or "")

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
                # Final-answer item start is a lifecycle barrier for open
                # progress streams — seal them before answer deltas land.
                if channel == "answer" and on_stream_item_done is not None:
                    for prev_sid, prev_ch in list(phase_by_item_id.items()):
                        if prev_ch == "progress" and prev_sid and prev_sid != sid:
                            _safe_cb(on_stream_item_done, {"stream_id": prev_sid})
                    for oi, prev_ch in list(phase_by_output_index.items()):
                        if prev_ch != "progress":
                            continue
                        prev_sid = stream_id_by_output_index.get(oi, "")
                        if prev_sid and prev_sid != sid:
                            _safe_cb(on_stream_item_done, {"stream_id": prev_sid})
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
                # Suppress anonymous mid-tool answer crumbs (legacy JSON
                # envelopes). Identity-bearing final_answer streams must still
                # paint after function_call items.
                if not has_tool_calls or bool(sid) or channel == "answer":
                    _safe_cb(on_delta, payload)
            continue

        if "function_call" in event_type:
            has_tool_calls = True

        if "reasoning" in event_type and "delta" in event_type:
            reasoning_text = event.get("delta") or ""
            if isinstance(reasoning_text, str) and reasoning_text:
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
            continue

        if event_type == "response.output_item.done":
            done_item = event.get("item")
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
                if sid:
                    _safe_cb(on_stream_item_done, {"stream_id": sid})
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

    if stream_error:
        return {
            "status": "failed",
            "output": [],
            "output_text": "",
            "usage": {},
            "error": stream_error,
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
            "error": "Codex Responses stream did not emit a terminal response",
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
            err_msg = "Codex response failed"

    out = {
        "status": terminal_status,
        "output": output,
        "output_text": assembled_text,
        "usage": terminal_usage if isinstance(terminal_usage, dict) else {},
        "error": err_msg,
        "model": terminal_model,
    }
    if terminal_incomplete_details is not None:
        out["incomplete_details"] = terminal_incomplete_details
    return out


class CodexResponsesDriver:
    # ChatGPT Codex backend requires stream=true; expose real SSE to the pilot.
    supports_streaming = True

    def __init__(
        self,
        name: str,
        model: str,
        *,
        base_url: str = DEFAULT_CODEX_BASE,
        api_key_env: str = "OPENAI_CODEX_TOKEN",
        max_tokens: int = 4096,
        timeout: int = 120,
    ) -> None:
        self.name = name
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.max_tokens = max_tokens
        self.timeout = timeout
        self._pool_provider: Optional[str] = None
        self._pool_entry_id: Optional[str] = None

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
        if api_effort:
            body["reasoning"] = {"effort": api_effort, "summary": "auto"}
        resp_tools = _tools_to_responses(tools)
        if resp_tools:
            body["tools"] = resp_tools
            body["tool_choice"] = "auto"
            body["parallel_tool_calls"] = True
        if session_id:
            body["prompt_cache_key"] = session_id
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
            headers = _codex_cloudflare_headers(token, streaming=True)
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
        if raw.get("error"):
            return DriverResponse(
                text="", model=self.name, error=str(raw["error"]),
                latency_ms=(time.time() - t0) * 1000.0,
                meta={
                    "api_mode": "codex_responses",
                    "finish_reason": raw.get("status"),
                },
            )
        text, tool_calls, finish = _extract_text_and_tools(raw)
        if not text and isinstance(raw.get("output_text"), str):
            text = raw["output_text"]
        if finish == "content_filter":
            return DriverResponse(
                text="",
                model=self.name,
                error=_CONTENT_FILTER_MSG,
                latency_ms=(time.time() - t0) * 1000.0,
                meta={
                    "api_mode": "codex_responses",
                    "finish_reason": "content_filter",
                    "billing": "plan",
                    "requested_model": self.model,
                },
            )
        usage = raw.get("usage") or {}
        tin, tout = _usage_ints(usage)
        meta = {
            "tool_calls": tool_calls,
            "finish_reason": finish,
            "raw_usage": usage,
            "api_mode": "codex_responses",
            "billing": "plan",
            "requested_model": self.model,
            "incomplete_retries": incomplete_retries,
        }
        reason = _incomplete_reason(raw)
        if reason:
            meta["incomplete_reason"] = reason
        cost = _usage_cost(usage)
        if cost is not None:
            meta["provider_cost_usd"] = cost
        served = raw.get("model")
        if isinstance(served, str) and served.strip():
            meta["served_model"] = served.strip()
        return DriverResponse(
            text=text,
            tokens_in=tin,
            tokens_out=tout,
            latency_ms=(time.time() - t0) * 1000.0,
            model=self.name,
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
        data = json.dumps(body).encode("utf-8")

        def _call() -> DriverResponse:
            t0 = time.time()
            nonlocal data, body
            length_parts: List[str] = []
            incomplete_retries = 0

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
                    return err_resp
                if raw is None:
                    return DriverResponse(
                        text="", model=self.name, error="empty response",
                        latency_ms=(time.time() - t0) * 1000.0,
                    )
                resp = self._response_from_raw(
                    raw, t0=t0, incomplete_retries=incomplete_retries,
                )
                if resp.error:
                    return resp
                text = resp.text or ""
                tool_calls = (resp.meta or {}).get("tool_calls") or []
                finish = str((resp.meta or {}).get("finish_reason") or "")
                kind = _codex_continuation_kind(finish, text, tool_calls)
                if kind is None:
                    final_text = "".join(length_parts) + text if length_parts else text
                    if final_text == text:
                        return resp
                    meta = dict(resp.meta or {})
                    return DriverResponse(
                        text=final_text,
                        tokens_in=resp.tokens_in,
                        tokens_out=resp.tokens_out,
                        latency_ms=resp.latency_ms,
                        model=self.name,
                        meta=meta,
                    )

                incomplete_retries += 1
                if kind == "length" and text.strip():
                    length_parts.append(text)
                if incomplete_retries > _CODEX_MAX_INCOMPLETE_RETRIES:
                    return DriverResponse(
                        text="".join(length_parts),
                        model=self.name,
                        error=(
                            "Codex response remained incomplete after "
                            f"{_CODEX_MAX_INCOMPLETE_RETRIES} continuation attempts"
                        ),
                        latency_ms=(time.time() - t0) * 1000.0,
                        meta={
                            "api_mode": "codex_responses",
                            "finish_reason": "incomplete",
                            "billing": "plan",
                            "incomplete_retries": incomplete_retries - 1,
                            "requested_model": self.model,
                        },
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

    def complete(self, task_prompt: str, *, system: str = SYSTEM_PROMPT) -> DriverResponse:
        body = self._build_body(
            [{"role": "user", "content": task_prompt}],
            system=system,
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
