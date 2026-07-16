"""BedrockDriver: interactive pilot over AWS Bedrock Converse / ConverseStream.

Wraps ``puppetmaster.bedrock.bedrock_chat`` (and ``bedrock_chat_stream`` when
present) — imported at call time, no new pip deps. Auth comes from process env
(Marionette injects AWS_* / BEDROCK_* from Settings BYOK). Converse stamps
prompt-cache ``cachePoint``s and reports ``cache_read_tokens`` /
``cache_write_tokens`` so session cost meters and ``cache_savings_usd`` match
other providers.

``chat_stream`` prefers ``puppetmaster.bedrock.bedrock_chat_stream`` when that
symbol exists; otherwise POSTs ConverseStream (``.../converse-stream``), parses
AWS eventstream chunks, and emits mid-invoke ``on_delta`` /
``on_reasoning_delta`` / ``on_tool_hint`` callbacks as text, reasoning, and tool
names arrive. On stream failure it falls back to non-stream ``chat()`` and
best-effort fires those callbacks once so the turn still completes.
"""

from __future__ import annotations

import json
import os
import struct
import time
import urllib.request
import zlib
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from .base import DriverResponse, SYSTEM_PROMPT
from .retry import with_retry
from pmharness.reasoning import extract_reasoning, strip_think_blocks


def _crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def _parse_eventstream_headers(raw: bytes) -> Dict[str, str]:
    """Parse AWS eventstream headers into a name→string map (string values only)."""
    headers: Dict[str, str] = {}
    pos = 0
    length = len(raw)
    while pos < length:
        name_len = raw[pos]
        pos += 1
        if pos + name_len > length:
            break
        name = raw[pos : pos + name_len].decode("utf-8", "replace")
        pos += name_len
        if pos >= length:
            break
        value_type = raw[pos]
        pos += 1
        if value_type == 7:  # string
            if pos + 2 > length:
                break
            (value_len,) = struct.unpack_from(">H", raw, pos)
            pos += 2
            if pos + value_len > length:
                break
            headers[name] = raw[pos : pos + value_len].decode("utf-8", "replace")
            pos += value_len
        elif value_type == 6:  # byte_array — skip
            if pos + 2 > length:
                break
            (value_len,) = struct.unpack_from(">H", raw, pos)
            pos += 2 + value_len
        elif value_type in (0, 1):  # bool true/false
            headers[name] = "true" if value_type == 0 else "false"
        elif value_type == 2:  # byte
            pos += 1
        elif value_type == 3:  # short
            pos += 2
        elif value_type == 4:  # integer
            pos += 4
        elif value_type in (5, 8):  # long / timestamp
            pos += 8
        elif value_type == 9:  # uuid
            pos += 16
        else:
            break
    return headers


def _take_eventstream_message(
    buffer: bytes,
) -> Tuple[Optional[Tuple[Dict[str, str], bytes]], bytes]:
    """Pull one AWS eventstream message from ``buffer``.

    Returns ``((headers, payload), remainder)`` or ``(None, buffer)`` when more
    bytes are needed. Raises ``ValueError`` on corrupt framing/CRC.
    """
    if len(buffer) < 12:
        return None, buffer
    total_len, headers_len = struct.unpack_from(">II", buffer, 0)
    if total_len < 16 or headers_len < 0 or headers_len > total_len - 16:
        raise ValueError("invalid eventstream prelude")
    if len(buffer) < total_len:
        return None, buffer
    message = buffer[:total_len]
    remainder = buffer[total_len:]
    prelude = message[:8]
    (prelude_crc,) = struct.unpack_from(">I", message, 8)
    if _crc32(prelude) != prelude_crc:
        raise ValueError("eventstream prelude CRC mismatch")
    (message_crc,) = struct.unpack_from(">I", message, total_len - 4)
    if _crc32(message[:-4]) != message_crc:
        raise ValueError("eventstream message CRC mismatch")
    headers_raw = message[12 : 12 + headers_len]
    payload = message[12 + headers_len : total_len - 4]
    return (_parse_eventstream_headers(headers_raw), payload), remainder


def iter_eventstream_messages(readable) -> Iterator[Tuple[Dict[str, str], bytes]]:
    """Yield ``(headers, payload)`` from an AWS eventstream readable."""
    buffer = b""
    while True:
        chunk = readable.read(4096)
        if not chunk:
            break
        buffer += chunk
        while True:
            taken, buffer = _take_eventstream_message(buffer)
            if taken is None:
                break
            yield taken
    if buffer:
        # Trailing bytes that never formed a full message — ignore empty pad.
        if buffer.strip(b"\x00"):
            raise ValueError("truncated eventstream buffer (%d bytes)" % len(buffer))


def encode_eventstream_message(headers: Dict[str, str], payload: bytes) -> bytes:
    """Encode one AWS eventstream message (used by hermetic tests)."""
    header_blob = bytearray()
    for name, value in headers.items():
        name_b = name.encode("utf-8")
        value_b = value.encode("utf-8")
        header_blob.append(len(name_b))
        header_blob.extend(name_b)
        header_blob.append(7)  # string
        header_blob.extend(struct.pack(">H", len(value_b)))
        header_blob.extend(value_b)
    headers_bytes = bytes(header_blob)
    total_len = 12 + len(headers_bytes) + len(payload) + 4
    prelude = struct.pack(">II", total_len, len(headers_bytes))
    body = prelude + struct.pack(">I", _crc32(prelude)) + headers_bytes + payload
    return body + struct.pack(">I", _crc32(body))


def _converse_stream_url(base_url: str, model_id: str) -> str:
    from puppetmaster.bedrock import converse_model_url

    url = converse_model_url(base_url, model_id)
    if url.endswith("/converse"):
        return url[: -len("/converse")] + "/converse-stream"
    return url + "-stream"


def _usage_from_converse_usage(usage: Optional[dict]) -> dict:
    """Fold Converse usage into the same shape ``_assistant_turn_from_converse`` uses."""
    usage = usage or {}
    uncached_in = int(usage.get("inputTokens") or usage.get("input_tokens") or 0)
    cache_read = int(
        usage.get("cacheReadInputTokens")
        or usage.get("cacheReadInputTokenCount")
        or 0
    )
    cache_write = int(
        usage.get("cacheWriteInputTokens")
        or usage.get("cacheWriteInputTokenCount")
        or 0
    )
    prompt_tokens = uncached_in + cache_read + cache_write
    completion_tokens = int(
        usage.get("outputTokens") or usage.get("output_tokens") or 0
    )
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cached_tokens": cache_read,
        "cache_write_tokens": cache_write,
    }


def _safe_callback(cb: Optional[Callable], *args) -> None:
    if cb is None:
        return
    try:
        cb(*args)
    except Exception:
        pass


class BedrockDriver:
    # Real mid-invoke deltas via ConverseStream (or bedrock_chat_stream).
    supports_streaming = True

    def __init__(
        self,
        name: str,
        model: str,
        *,
        max_tokens: int = 8000,
        temperature: float = 0.0,
        timeout: int = 300,
        send_temperature: bool = False,
    ) -> None:
        self.name = name
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout = timeout
        self.send_temperature = send_temperature

    def _ensure_auth(self) -> None:
        """Raise with an actionable message when AWS Bedrock creds are absent."""
        from puppetmaster.bedrock import (
            missing_bedrock_credentials_message,
            resolve_bedrock_credentials,
        )

        if resolve_bedrock_credentials(os.environ) is None:
            raise RuntimeError(missing_bedrock_credentials_message())

    def _extra(self) -> dict:
        extra: dict[str, Any] = {"max_tokens": self.max_tokens}
        if self.send_temperature and self.temperature is not None:
            extra["temperature"] = self.temperature
        # Claude-on-Bedrock extended thinking (same effort knob as Codex/Anthropic).
        # Forwarded via Puppetmaster build_converse_body when present; also
        # stamped post-build in _chat_stream_converse for older PM installs.
        try:
            from harness.reasoning_effort import (
                anthropic_thinking_budget,
                model_supports_anthropic_thinking,
            )
            if model_supports_anthropic_thinking(self.model):
                budget = anthropic_thinking_budget()
                if budget is not None and budget > 0:
                    if int(extra.get("max_tokens") or 0) <= budget:
                        extra["max_tokens"] = budget + 1024
                    extra["additionalModelRequestFields"] = {
                        "thinking": {"type": "enabled", "budget_tokens": int(budget)},
                    }
                    extra.pop("temperature", None)
        except Exception:
            pass
        return extra

    def _stamp_thinking_fields(self, body: dict) -> dict:
        """Attach Claude extended thinking onto a Converse body (PM passthrough gap).

        ``puppetmaster.bedrock.build_converse_body`` does not yet forward
        ``additionalModelRequestFields`` from ``extra``, so we stamp after build.
        """
        if not isinstance(body, dict):
            return body
        try:
            from harness.reasoning_effort import (
                anthropic_thinking_budget,
                model_supports_anthropic_thinking,
            )
            if not model_supports_anthropic_thinking(self.model):
                return body
            budget = anthropic_thinking_budget()
            if budget is None or budget <= 0:
                return body
            fields = dict(body.get("additionalModelRequestFields") or {})
            fields["thinking"] = {"type": "enabled", "budget_tokens": int(budget)}
            body["additionalModelRequestFields"] = fields
            # Thinking rejects custom temperature.
            cfg = body.get("inferenceConfig")
            if isinstance(cfg, dict):
                cfg.pop("temperature", None)
        except Exception:
            pass
        return body

    def _wire_messages(
        self, messages: list, system: Optional[str]
    ) -> list:
        wire = list(messages or [])
        if system:
            wire = [{"role": "system", "content": system}] + [
                m for m in wire if m.get("role") != "system"
            ]
        return wire

    def _turn_to_response(self, turn: Any, *, latency_ms: float) -> DriverResponse:
        """Map Puppetmaster ``AssistantTurn`` → DriverResponse (OpenAI tool_calls)."""
        text = getattr(turn, "text", "") or ""
        usage = getattr(turn, "usage", None) or {}
        raw_calls = getattr(turn, "tool_calls", None) or []
        tool_calls = []
        for call in raw_calls:
            if not isinstance(call, dict):
                continue
            args = call.get("arguments")
            if isinstance(args, dict):
                args_str = json.dumps(args)
            elif isinstance(args, str):
                args_str = args
            else:
                args_str = "{}"
            tool_calls.append({
                "id": call.get("id") or "",
                "type": "function",
                "function": {
                    "name": call.get("name") or "",
                    "arguments": args_str,
                },
            })

        reasoning = extract_reasoning({"content": text})
        pure_text = strip_think_blocks(text)
        tokens_in = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        tokens_out = int(
            usage.get("completion_tokens") or usage.get("output_tokens") or 0
        )
        cache_read = int(usage.get("cached_tokens") or 0)
        cache_write = int(usage.get("cache_write_tokens") or 0)

        return DriverResponse(
            text=pure_text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
            model=self.name,
            meta={
                "tool_calls": tool_calls,
                "reasoning": reasoning,
                "finish_reason": getattr(turn, "finish_reason", "") or "",
                "cache_write_tokens": cache_write,
                "cache_read_tokens": cache_read,
                # Bedrock Converse does not split 5m/1h; bill writes at 1.25x.
                "cache_write_5m_tokens": cache_write,
                "cache_write_1h_tokens": 0,
            },
        )

    def _invoke(
        self,
        messages: list,
        *,
        tools: Optional[list] = None,
        on_delta: Optional[Callable] = None,
    ) -> DriverResponse:
        self._ensure_auth()
        from puppetmaster.bedrock import bedrock_chat
        from puppetmaster.providers import ProviderError

        t0 = time.time()
        try:
            turn = bedrock_chat(
                model=self.model,
                messages=messages,
                tools=tools,
                extra=self._extra(),
                timeout=self.timeout,
                on_delta=on_delta,
            )
        except ProviderError as e:
            return DriverResponse(
                text="",
                model=self.name,
                error=str(e),
                latency_ms=(time.time() - t0) * 1000.0,
            )
        except Exception as e:
            return DriverResponse(
                text="",
                model=self.name,
                error=repr(e),
                latency_ms=(time.time() - t0) * 1000.0,
            )
        return self._turn_to_response(turn, latency_ms=(time.time() - t0) * 1000.0)

    def complete(self, task_prompt: str, *, system: str = SYSTEM_PROMPT) -> DriverResponse:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": task_prompt})

        def _call() -> DriverResponse:
            return self._invoke(messages)

        return with_retry(_call)

    def chat(
        self,
        messages: list,
        *,
        tools: Optional[list] = None,
        system: Optional[str] = None,
    ) -> DriverResponse:
        wire = self._wire_messages(messages, system)

        def _call() -> DriverResponse:
            return self._invoke(wire, tools=tools)

        return with_retry(_call)

    def _fallback_chat_stream(
        self,
        messages: list,
        *,
        tools: Optional[list],
        system: Optional[str],
        on_delta,
        on_reasoning_delta,
        on_tool_hint,
    ) -> DriverResponse:
        """Non-stream chat + best-effort one-shot callbacks (never raises)."""
        try:
            resp = self.chat(messages, tools=tools, system=system)
        except Exception as e:
            return DriverResponse(
                text="",
                model=self.name,
                error=repr(e),
                meta={"stream_started": False, "stream_fallback": True},
            )
        if resp.error:
            if resp.meta is None:
                resp.meta = {}
            resp.meta["stream_fallback"] = True
            resp.meta["stream_started"] = False
            return resp
        if on_delta is not None and resp.text:
            _safe_callback(on_delta, resp.text)
        reasoning = ""
        if resp.meta:
            reasoning = resp.meta.get("reasoning") or ""
        if on_reasoning_delta is not None and reasoning:
            _safe_callback(on_reasoning_delta, reasoning)
        if on_tool_hint is not None and resp.meta:
            for tc in resp.meta.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                name = (fn.get("name") or tc.get("name") or "").strip()
                if name:
                    _safe_callback(on_tool_hint, name)
        if resp.meta is None:
            resp.meta = {}
        resp.meta["stream_started"] = bool(resp.text)
        resp.meta["stream_fallback"] = True
        return resp

    def _chat_stream_via_pm(
        self,
        stream_fn: Callable,
        wire: list,
        *,
        tools: Optional[list],
        on_delta,
        on_reasoning_delta,
        on_tool_hint,
    ) -> DriverResponse:
        """Adapter for ``puppetmaster.bedrock.bedrock_chat_stream`` when present.

        PM's stream uses ``on_delta(kind, text)`` with kind ``\"text\"`` /
        ``\"reasoning\"``. Only kwargs accepted by ``stream_fn`` are forwarded
        so a leaner sibling signature never TypeErrors into fallback.
        """
        import inspect

        t0 = time.time()
        stream_started = False
        hinted_tools = set()  # type: set

        def _pm_on_delta(*args) -> None:
            nonlocal stream_started
            # Support both on_delta(text) and on_delta(kind, text).
            if len(args) == 1:
                piece = args[0] or ""
                if piece:
                    stream_started = True
                    _safe_callback(on_delta, piece)
                return
            if len(args) >= 2:
                kind, text = args[0], args[1] or ""
                if not text:
                    return
                if kind in ("text", "content"):
                    stream_started = True
                    _safe_callback(on_delta, text)
                elif kind in ("reasoning", "thinking", "reasoning_content"):
                    stream_started = True
                    _safe_callback(on_reasoning_delta, text)
                elif kind in ("tool_hint", "tool"):
                    stream_started = True
                    hinted_tools.add(str(text))
                    _safe_callback(on_tool_hint, text)

        def _pm_reasoning(piece: str) -> None:
            nonlocal stream_started
            if piece:
                stream_started = True
                _safe_callback(on_reasoning_delta, piece)

        def _pm_tool_hint(name: str) -> None:
            nonlocal stream_started
            if name:
                stream_started = True
                hinted_tools.add(str(name))
                _safe_callback(on_tool_hint, name)

        kwargs = {
            "model": self.model,
            "messages": wire,
            "tools": tools,
            "extra": self._extra(),
            "timeout": self.timeout,
            "on_delta": _pm_on_delta,
        }  # type: dict
        try:
            params = inspect.signature(stream_fn).parameters
        except (TypeError, ValueError):
            params = {}
        if "on_reasoning_delta" in params and on_reasoning_delta is not None:
            kwargs["on_reasoning_delta"] = _pm_reasoning
        if "on_tool_hint" in params and on_tool_hint is not None:
            kwargs["on_tool_hint"] = _pm_tool_hint

        turn = stream_fn(**kwargs)
        resp = self._turn_to_response(turn, latency_ms=(time.time() - t0) * 1000.0)
        if resp.meta is None:
            resp.meta = {}
        resp.meta["stream_started"] = stream_started or bool(
            resp.text and not resp.error
        )
        # PM stream may assemble tools without a live hint — fire once after.
        if on_tool_hint is not None:
            for tc in resp.meta.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                name = (fn.get("name") or "").strip()
                if name and name not in hinted_tools:
                    _safe_callback(on_tool_hint, name)
        return resp

    def _chat_stream_converse(
        self,
        wire: list,
        *,
        tools: Optional[list],
        on_delta,
        on_reasoning_delta,
        on_tool_hint,
    ) -> DriverResponse:
        """POST ConverseStream and parse eventstream deltas in-process."""
        from puppetmaster.bedrock import (
            bedrock_runtime_base_url,
            build_converse_body,
            require_bedrock_model_id,
            resolve_bedrock_region,
            _auth_headers_for_request,
            _resolve_call_credentials,
        )

        t0 = time.time()
        region = resolve_bedrock_region(os.environ)
        url_base = bedrock_runtime_base_url(region).rstrip("/")
        model_id = require_bedrock_model_id(self.model)
        body = self._stamp_thinking_fields(
            build_converse_body(messages=wire, tools=tools, extra=self._extra())
        )
        url = _converse_stream_url(url_base, model_id)
        payload = json.dumps(body).encode("utf-8")
        creds = _resolve_call_credentials(api_key=None, env=os.environ)
        try:
            headers = _auth_headers_for_request(
                method="POST",
                url=url,
                body_bytes=payload,
                region=region,
                creds=creds,
                content_type="application/json",
                accept="application/vnd.amazon.eventstream",
            )
        except TypeError:
            # Older puppetmaster.bedrock without accept= kwarg.
            headers = _auth_headers_for_request(
                method="POST",
                url=url,
                body_bytes=payload,
                region=region,
                creds=creds,
                content_type="application/json",
            )
            headers = dict(headers)
            headers["Accept"] = "application/vnd.amazon.eventstream"

        text_pieces: List[str] = []
        reasoning_pieces: List[str] = []
        tool_blocks: Dict[int, dict] = {}
        tokens_in = 0
        tokens_out = 0
        cache_read = 0
        cache_write = 0
        stop_reason = ""
        stream_started = False

        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            for hdrs, event_payload in iter_eventstream_messages(resp):
                msg_type = (hdrs.get(":message-type") or "event").lower()
                if msg_type == "exception":
                    detail = event_payload.decode("utf-8", "replace")[:500]
                    exc_type = hdrs.get(":exception-type") or "error"
                    raise RuntimeError("%s: %s" % (exc_type, detail))

                event_type = hdrs.get(":event-type") or ""
                if not event_payload:
                    continue
                try:
                    evt = json.loads(event_payload.decode("utf-8"))
                except Exception:
                    continue
                if not isinstance(evt, dict):
                    continue

                # Some gateways wrap as {"contentBlockDelta": {...}}.
                if event_type and event_type in evt and isinstance(evt[event_type], dict):
                    evt = evt[event_type]

                if event_type == "contentBlockStart" or "start" in evt:
                    idx = int(evt.get("contentBlockIndex") or 0)
                    start = evt.get("start") or {}
                    tool_use = start.get("toolUse") if isinstance(start, dict) else None
                    if isinstance(tool_use, dict):
                        name = (tool_use.get("name") or "").strip()
                        tool_blocks[idx] = {
                            "id": tool_use.get("toolUseId") or "",
                            "name": name,
                            "args": "",
                        }
                        if name:
                            stream_started = True
                            _safe_callback(on_tool_hint, name)

                elif event_type == "contentBlockDelta" or "delta" in evt:
                    idx = int(evt.get("contentBlockIndex") or 0)
                    delta = evt.get("delta") or {}
                    if not isinstance(delta, dict):
                        continue
                    text_piece = delta.get("text")
                    if text_piece:
                        stream_started = True
                        text_pieces.append(str(text_piece))
                        _safe_callback(on_delta, str(text_piece))
                    reasoning = delta.get("reasoningContent")
                    if isinstance(reasoning, dict):
                        rtext = reasoning.get("text") or ""
                        if rtext:
                            stream_started = True
                            reasoning_pieces.append(str(rtext))
                            _safe_callback(on_reasoning_delta, str(rtext))
                    elif isinstance(reasoning, str) and reasoning:
                        stream_started = True
                        reasoning_pieces.append(reasoning)
                        _safe_callback(on_reasoning_delta, reasoning)
                    tool_delta = delta.get("toolUse")
                    if isinstance(tool_delta, dict):
                        if idx not in tool_blocks:
                            tool_blocks[idx] = {
                                "id": tool_delta.get("toolUseId") or "",
                                "name": tool_delta.get("name") or "",
                                "args": "",
                            }
                            name = (tool_blocks[idx]["name"] or "").strip()
                            if name:
                                stream_started = True
                                _safe_callback(on_tool_hint, name)
                        partial = tool_delta.get("input")
                        if partial is None:
                            pass
                        elif isinstance(partial, dict):
                            # Rare: full object mid-stream — replace.
                            tool_blocks[idx]["args"] = json.dumps(partial)
                        else:
                            tool_blocks[idx]["args"] += str(partial)

                elif event_type == "messageStop" or "stopReason" in evt:
                    stop_reason = str(evt.get("stopReason") or stop_reason or "")

                elif event_type == "metadata" or "usage" in evt:
                    usage_fields = _usage_from_converse_usage(evt.get("usage") or {})
                    tokens_in = int(usage_fields["prompt_tokens"] or tokens_in)
                    tokens_out = int(usage_fields["completion_tokens"] or tokens_out)
                    cache_read = int(usage_fields["cached_tokens"] or cache_read)
                    cache_write = int(usage_fields["cache_write_tokens"] or cache_write)

                elif event_type in (
                    "internalServerException",
                    "modelStreamErrorException",
                    "validationException",
                    "throttlingException",
                    "serviceUnavailableException",
                ) or event_type.endswith("Exception"):
                    msg = evt.get("message") or json.dumps(evt)[:300]
                    raise RuntimeError("%s: %s" % (event_type, msg))

        latency = (time.time() - t0) * 1000.0
        full_text = "".join(text_pieces)
        tool_calls = []
        for idx in sorted(tool_blocks.keys()):
            tb = tool_blocks[idx]
            args = tb["args"] or "{}"
            try:
                json.loads(args)
            except Exception:
                pass
            tool_calls.append({
                "id": tb["id"],
                "type": "function",
                "function": {"name": tb["name"], "arguments": args},
            })

        reasoning_text = "".join(reasoning_pieces)
        message_obj: dict = {"content": full_text}
        if reasoning_text:
            message_obj["reasoning"] = reasoning_text
            message_obj["reasoning_content"] = reasoning_text
        reasoning = extract_reasoning(message_obj)
        pure_text = strip_think_blocks(full_text)

        return DriverResponse(
            text=pure_text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency,
            model=self.name,
            meta={
                "tool_calls": tool_calls,
                "reasoning": reasoning,
                "finish_reason": stop_reason,
                "cache_write_tokens": cache_write,
                "cache_read_tokens": cache_read,
                "cache_write_5m_tokens": cache_write,
                "cache_write_1h_tokens": 0,
                "stream_started": stream_started,
            },
        )

    def chat_stream(
        self,
        messages: list,
        *,
        tools: Optional[list] = None,
        system: Optional[str] = None,
        on_delta=None,
        on_reasoning_delta=None,
        on_tool_hint=None,
    ) -> DriverResponse:
        """Stream ConverseStream deltas; fall back to ``chat()`` on failure."""
        wire = self._wire_messages(messages, system)
        try:
            self._ensure_auth()
        except Exception:
            return self._fallback_chat_stream(
                messages,
                tools=tools,
                system=system,
                on_delta=on_delta,
                on_reasoning_delta=on_reasoning_delta,
                on_tool_hint=on_tool_hint,
            )

        import puppetmaster.bedrock as bedrock_mod

        stream_fn = getattr(bedrock_mod, "bedrock_chat_stream", None)
        try:
            if callable(stream_fn):
                return self._chat_stream_via_pm(
                    stream_fn,
                    wire,
                    tools=tools,
                    on_delta=on_delta,
                    on_reasoning_delta=on_reasoning_delta,
                    on_tool_hint=on_tool_hint,
                )
            return self._chat_stream_converse(
                wire,
                tools=tools,
                on_delta=on_delta,
                on_reasoning_delta=on_reasoning_delta,
                on_tool_hint=on_tool_hint,
            )
        except Exception:
            # Mid-invoke or connect failure: non-stream chat + one-shot callbacks.
            return self._fallback_chat_stream(
                messages,
                tools=tools,
                system=system,
                on_delta=on_delta,
                on_reasoning_delta=on_reasoning_delta,
                on_tool_hint=on_tool_hint,
            )
