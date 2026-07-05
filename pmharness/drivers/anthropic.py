from __future__ import annotations

"""AnthropicDriver: Claude's native Messages API (/v1/messages) is NOT
OpenAI-compatible (different auth header, request shape, and response shape), so
it gets a dedicated driver. stdlib-only. Key read from env at call time.
"""

import json
import os
import time
import urllib.request
import urllib.error

from .base import DriverResponse, SYSTEM_PROMPT
from .retry import with_retry
from pmharness.reasoning import extract_reasoning, strip_think_blocks


class AnthropicDriver:
    supports_streaming = True

    def __init__(self, name: str, model: str, *,
                 base_url: str = "https://api.anthropic.com/v1",
                 api_key_env: str = "ANTHROPIC_API_KEY",
                 version: str = "2023-06-01",
                 max_tokens: int = 1024, temperature: float = 0.0, timeout: int = 90,
                 send_temperature: bool = False,
                 enable_prompt_cache: bool = True) -> None:
        self.name = name
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.version = version
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.send_temperature = send_temperature
        self.timeout = timeout
        self.enable_prompt_cache = enable_prompt_cache

    def _key(self) -> str:
        key = os.environ.get(self.api_key_env, "").strip()
        if not key:
            raise RuntimeError(f"missing API key in env var {self.api_key_env}")
        return key

    def complete(self, task_prompt: str, *, system: str = SYSTEM_PROMPT) -> DriverResponse:
        url = f"{self.base_url}/messages"
        body = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "user", "content": task_prompt}],
        }
        if self.enable_prompt_cache:
            body["system"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        else:
            body["system"] = system

        # Some Anthropic models (Opus 4.x) reject an explicit temperature.
        if self.temperature is not None and self.send_temperature:
            body["temperature"] = self.temperature
        data = json.dumps(body).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self._key(),
            "anthropic-version": self.version,
        }
        if self.enable_prompt_cache:
            headers["anthropic-beta"] = "prompt-caching-2024-07-31"

        def _call() -> DriverResponse:
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            t0 = time.time()
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:500]
                return DriverResponse(text="", model=self.name, error=f"HTTP {e.code}: {detail}",
                                      latency_ms=(time.time() - t0) * 1000.0)
            except Exception as e:
                return DriverResponse(text="", model=self.name, error=repr(e),
                                      latency_ms=(time.time() - t0) * 1000.0)
            latency = (time.time() - t0) * 1000.0
            try:
                # content is a list of blocks; take the first text block
                blocks = raw.get("content", [])
                text = ""
                for b in blocks:
                    if b.get("type") == "text":
                        text = b.get("text", "")
                        break
            except (AttributeError, TypeError):
                return DriverResponse(text="", model=self.name,
                                      error=f"unexpected response: {str(raw)[:300]}", latency_ms=latency)
            usage = raw.get("usage", {}) or {}
            cache_write = int(usage.get("cache_creation_input_tokens", 0) or 0)
            cache_read = int(usage.get("cache_read_input_tokens", 0) or 0)
            return DriverResponse(
                text=text,
                tokens_in=int(usage.get("input_tokens", 0) or 0),
                tokens_out=int(usage.get("output_tokens", 0) or 0),
                latency_ms=latency, model=self.name,
                meta={
                    "stop_reason": raw.get("stop_reason"),
                    "cache_write_tokens": cache_write,
                    "cache_read_tokens": cache_read,
                },
            )

        return with_retry(_call)

    def _build_body(self, messages: list, tools: list | None, system: str | None) -> dict:
        """Build the Anthropic /v1/messages request body (messages + tools +
        system). Shared by chat() and chat_stream() so both speak the same
        native tool-calling protocol -- only streaming vs blocking differs."""
        anthropic_msgs = []
        for msg in messages:
            role = msg.get("role")
            if role == "system":
                continue

            blocks = []
            if role == "assistant":
                text = msg.get("content") or ""
                if text:
                    blocks.append({"type": "text", "text": text})
                tool_calls = msg.get("tool_calls") or []
                for tc in tool_calls:
                    tc_id = tc.get("id") or ""
                    func = tc.get("function") or {}
                    name = func.get("name") or ""
                    raw_args = func.get("arguments") or {}
                    if isinstance(raw_args, str):
                        try:
                            args = json.loads(raw_args)
                        except Exception:
                            args = {}
                    else:
                        args = raw_args
                    blocks.append({
                        "type": "tool_use",
                        "id": tc_id,
                        "name": name,
                        "input": args
                    })
                anth_role = "assistant"

            elif role == "tool":
                tc_id = msg.get("tool_call_id") or ""
                content_val = msg.get("content") or ""
                blocks.append({
                    "type": "tool_result",
                    "tool_use_id": tc_id,
                    "content": content_val
                })
                anth_role = "user"

            else:
                text = msg.get("content") or ""
                blocks.append({"type": "text", "text": text})
                anth_role = "user"

            if anthropic_msgs and anthropic_msgs[-1]["role"] == anth_role:
                anthropic_msgs[-1]["content"].extend(blocks)
            else:
                if not blocks:
                    blocks = [{"type": "text", "text": ""}]
                anthropic_msgs.append({
                    "role": anth_role,
                    "content": blocks
                })

        anthropic_tools = []
        if tools:
            for t in tools:
                if not isinstance(t, dict):
                    continue
                func = t.get("function") or {}
                name = func.get("name") or ""
                desc = func.get("description") or ""
                schema = func.get("parameters") or {"type": "object", "properties": {}, "required": []}
                anthropic_tools.append({
                    "name": name,
                    "description": desc,
                    "input_schema": schema
                })

        body = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": anthropic_msgs,
        }

        if system:
            if self.enable_prompt_cache:
                body["system"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
            else:
                body["system"] = system

        if self.temperature is not None and self.send_temperature:
            body["temperature"] = self.temperature

        if anthropic_tools:
            # Prompt-cache the tool schema too. It is large (~15 rich tool defs)
            # and IDENTICAL every turn, so without a breakpoint here we re-bill the
            # whole schema on every call. Anthropic caches the prefix up to the
            # LAST cache_control marker, so marking the final tool caches
            # system + tools together -- the big-dog multi-turn cost win.
            if self.enable_prompt_cache and anthropic_tools:
                anthropic_tools[-1] = {**anthropic_tools[-1],
                                       "cache_control": {"type": "ephemeral"}}
            body["tools"] = anthropic_tools
            body["tool_choice"] = {"type": "auto"}

        # Cache the stable conversation-history PREFIX. Anthropic caches the
        # prefix up to each cache_control marker and allows up to 4 markers; we
        # use system + last-tool, leaving room for TWO history breakpoints.
        #
        # Why two, not one: a single marker on the LAST message moves to a new
        # position every turn, so the marker never sits at a point that is stable
        # across turns -- each turn writes a fresh cache entry (a 25% write
        # surcharge) and the longest reusable prefix from the PREVIOUS turn ended
        # one message earlier, which this turn's single moving marker does not
        # reuse. Placing a SECOND marker on the second-to-last message pins a
        # breakpoint that WAS the last message last turn (already cached) so the
        # bulk of the growing transcript is served as a cache READ (~10%) this
        # turn, while the moving last-message marker extends the cache to include
        # the newest suffix for next turn. This is the multi-turn cache pattern
        # top agents (Cursor/Claude Code) rely on to keep input cost near-flat as
        # the conversation grows.
        if self.enable_prompt_cache and anthropic_msgs:
            def _mark(msg: dict) -> None:
                content = msg.get("content")
                if isinstance(content, list) and content:
                    if isinstance(content[-1], dict):
                        content[-1] = {**content[-1], "cache_control": {"type": "ephemeral"}}
                elif isinstance(content, str):
                    msg["content"] = [{"type": "text", "text": content,
                                       "cache_control": {"type": "ephemeral"}}]
            # Stable prefix breakpoint (second-to-last message): this position was
            # the moving marker last turn, so its prefix is already cached and
            # reused as a READ this turn.
            if len(anthropic_msgs) >= 2:
                _mark(anthropic_msgs[-2])
            # Moving breakpoint (last message): extends the cached prefix to cover
            # the newest turn so NEXT turn's stable marker can reuse it.
            _mark(anthropic_msgs[-1])

        return body

    def _headers(self) -> dict:
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self._key(),
            "anthropic-version": self.version,
        }
        if self.enable_prompt_cache:
            headers["anthropic-beta"] = "prompt-caching-2024-07-31"
        return headers

    def chat(self, messages: list, *, tools: list | None = None, system: str | None = None) -> DriverResponse:
        url = f"{self.base_url}/messages"
        body = self._build_body(messages, tools, system)
        data = json.dumps(body).encode("utf-8")
        headers = self._headers()

        def _call() -> DriverResponse:
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            t0 = time.time()
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:500]
                return DriverResponse(
                    text="", model=self.name, error=f"HTTP {e.code}: {detail}",
                    latency_ms=(time.time() - t0) * 1000.0,
                )
            except Exception as e:
                return DriverResponse(
                    text="", model=self.name, error=repr(e),
                    latency_ms=(time.time() - t0) * 1000.0,
                )

            latency = (time.time() - t0) * 1000.0

            try:
                blocks = raw.get("content") or []
                text_pieces = []
                tool_calls = []

                for b in blocks:
                    if not isinstance(b, dict):
                        continue
                    b_type = b.get("type")
                    if b_type == "text":
                        text_pieces.append(b.get("text") or "")
                    elif b_type == "tool_use":
                        tc_id = b.get("id") or ""
                        name = b.get("name") or ""
                        input_dict = b.get("input") or {}
                        tool_calls.append({
                            "id": tc_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(input_dict)
                            }
                        })

                full_text = "".join(text_pieces)

            except (AttributeError, TypeError):
                return DriverResponse(
                    text="", model=self.name,
                    error=f"unexpected response: {str(raw)[:300]}", latency_ms=latency
                )

            message_obj = {"content": blocks}
            reasoning = extract_reasoning(message_obj)
            if not reasoning:
                reasoning = extract_reasoning({"content": full_text})

            pure_text = strip_think_blocks(full_text)

            usage = raw.get("usage", {}) or {}
            cache_write = int(usage.get("cache_creation_input_tokens", 0) or 0)
            cache_read = int(usage.get("cache_read_input_tokens", 0) or 0)

            return DriverResponse(
                text=pure_text,
                tokens_in=int(usage.get("input_tokens", 0) or 0),
                tokens_out=int(usage.get("output_tokens", 0) or 0),
                latency_ms=latency,
                model=self.name,
                meta={
                    "tool_calls": tool_calls,
                    "reasoning": reasoning,
                    "finish_reason": raw.get("stop_reason") or "",
                    "cache_write_tokens": cache_write,
                    "cache_read_tokens": cache_read,
                }
            )

        return with_retry(_call)

    def chat_stream(self, messages: list, *, tools: list | None = None,
                    system: str | None = None, on_delta=None) -> DriverResponse:
        """Streaming counterpart of chat() over Anthropic's SSE Messages API.
        Emits text deltas via on_delta(str) as they arrive, while assembling the
        full text + native tool_use calls so the return value matches chat()
        exactly. This is what makes Claude prose render token-by-token instead of
        dumping after the full response, and it preserves native tool calling."""
        url = f"{self.base_url}/messages"
        body = self._build_body(messages, tools, system)
        body["stream"] = True
        data = json.dumps(body).encode("utf-8")
        headers = self._headers()
        if on_delta is None:
            on_delta = lambda _t: None

        t0 = time.time()
        full_text_pieces = []
        # tool_use blocks assembled by content-block index.
        tool_blocks: dict = {}
        tokens_in = 0
        tokens_out = 0
        cache_write = 0
        cache_read = 0
        stop_reason = ""

        try:
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                for raw_line in resp:
                    line = raw_line.decode("utf-8", "replace").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if not payload:
                        continue
                    try:
                        evt = json.loads(payload)
                    except Exception:
                        continue
                    etype = evt.get("type")

                    if etype == "message_start":
                        usage = (evt.get("message") or {}).get("usage") or {}
                        tokens_in = int(usage.get("input_tokens", 0) or 0)
                        cache_write = int(usage.get("cache_creation_input_tokens", 0) or 0)
                        cache_read = int(usage.get("cache_read_input_tokens", 0) or 0)

                    elif etype == "content_block_start":
                        idx = evt.get("index")
                        block = evt.get("content_block") or {}
                        if block.get("type") == "tool_use":
                            tool_blocks[idx] = {
                                "id": block.get("id") or "",
                                "name": block.get("name") or "",
                                "args": "",
                            }

                    elif etype == "content_block_delta":
                        idx = evt.get("index")
                        delta = evt.get("delta") or {}
                        dtype = delta.get("type")
                        if dtype == "text_delta":
                            piece = delta.get("text") or ""
                            if piece:
                                full_text_pieces.append(piece)
                                on_delta(piece)
                        elif dtype == "input_json_delta":
                            # Streamed tool-call arguments (partial JSON).
                            if idx in tool_blocks:
                                tool_blocks[idx]["args"] += delta.get("partial_json") or ""

                    elif etype == "message_delta":
                        usage = evt.get("usage") or {}
                        tokens_out = int(usage.get("output_tokens", tokens_out) or tokens_out)
                        sr = (evt.get("delta") or {}).get("stop_reason")
                        if sr:
                            stop_reason = sr

                    elif etype == "error":
                        err = evt.get("error") or {}
                        return DriverResponse(
                            text="", model=self.name,
                            error=f"{err.get('type','error')}: {err.get('message','')}",
                            latency_ms=(time.time() - t0) * 1000.0,
                            meta={"stream_started": bool(full_text_pieces)},
                        )

        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            return DriverResponse(
                text="", model=self.name, error=f"HTTP {e.code}: {detail}",
                latency_ms=(time.time() - t0) * 1000.0,
                meta={"stream_started": bool(full_text_pieces)},
            )
        except Exception as e:
            return DriverResponse(
                text="", model=self.name, error=repr(e),
                latency_ms=(time.time() - t0) * 1000.0,
                meta={"stream_started": bool(full_text_pieces)},
            )

        latency = (time.time() - t0) * 1000.0
        full_text = "".join(full_text_pieces)

        tool_calls = []
        for idx in sorted(tool_blocks.keys()):
            tb = tool_blocks[idx]
            args = tb["args"] or "{}"
            # Validate the assembled JSON. If it's malformed (truncated mid-stream,
            # e.g. the response hit max_tokens during a large edit_file), KEEP the
            # raw partial string rather than silently replacing it with "{}" --
            # the harness's parse_tool_calls detects the broken JSON and asks the
            # model to retry with smaller args, instead of the args just vanishing.
            try:
                json.loads(args)
            except Exception:
                # leave `args` as the raw (broken) partial so downstream parsing
                # flags it as truncated/invalid rather than an empty call.
                pass
            tool_calls.append({
                "id": tb["id"],
                "type": "function",
                "function": {"name": tb["name"], "arguments": args},
            })

        reasoning = extract_reasoning({"content": full_text})
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
                "stream_started": bool(full_text_pieces),
            },
        )
