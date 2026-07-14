from __future__ import annotations

"""GeminiDriver: Google Gemini's native generateContent REST API is NOT
OpenAI-compatible, so it gets a dedicated driver. stdlib-only.
"""

import json
import os
import time
from typing import Any, Callable, Optional
import urllib.request
import urllib.error

from .base import DriverResponse, SYSTEM_PROMPT
from .retry import with_retry


class GeminiDriver:
    supports_streaming = True

    def __init__(self, name: str, model: str, *,
                 base_url: str = "https://generativelanguage.googleapis.com/v1beta",
                 api_key_env: str = "GEMINI_API_KEY",
                 max_tokens: int = 8000,
                 temperature: float = 0.0,
                 timeout: int = 90,
                 send_temperature: bool = False,
                 sleep=None) -> None:
        self.name = name
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout = timeout
        self.send_temperature = send_temperature
        self._sleep = sleep or time.sleep

    def _key(self) -> str:
        key = os.environ.get(self.api_key_env, "").strip()
        if not key:
            raise RuntimeError(f"missing API key in env var {self.api_key_env}")
        return key

    def _sanitize_schema(self, schema: dict) -> dict:
        if not isinstance(schema, dict):
            return schema
        new_schema = {}
        for k, v in schema.items():
            if k in ("$schema", "additionalProperties"):
                continue
            if isinstance(v, dict):
                new_schema[k] = self._sanitize_schema(v)
            elif isinstance(v, list):
                new_schema[k] = [self._sanitize_schema(item) if isinstance(item, dict) else item for item in v]
            else:
                new_schema[k] = v
        return new_schema

    def _generation_config(self, *, include_thoughts: bool = False) -> dict[str, Any]:
        gen_config: dict[str, Any] = {"maxOutputTokens": self.max_tokens}
        if self.send_temperature and self.temperature is not None:
            gen_config["temperature"] = self.temperature
        if include_thoughts:
            # Surface thought summaries so chat_stream can emit on_reasoning_delta
            # mid-turn (Gemini 2.5+/3 thinking models).
            gen_config["thinkingConfig"] = {"includeThoughts": True}
        return gen_config

    def _build_contents(self, messages: list) -> list:
        gemini_contents = []
        tool_id_to_name = {}

        for msg in messages:
            role = msg.get("role")
            if role == "system":
                continue

            parts = []
            if role == "assistant":
                text = msg.get("content") or ""
                if text:
                    parts.append({"text": text})

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
                    if tc_id and name:
                        tool_id_to_name[tc_id] = name
                    fc_part = {
                        "functionCall": {
                            "name": name,
                            "args": args
                        }
                    }
                    # Echo back the thoughtSignature Gemini gave us for this call
                    # (required by Gemini 3+ or the request 400s). Stored on the
                    # tool_call when we parsed the model's response.
                    sig = tc.get("thought_signature")
                    if sig:
                        fc_part["thoughtSignature"] = sig
                    parts.append(fc_part)
                gemini_role = "model"

            elif role == "tool":
                tc_id = msg.get("tool_call_id") or ""
                content_val = msg.get("content") or ""
                name = tool_id_to_name.get(tc_id, tc_id)
                parts.append({
                    "functionResponse": {
                        "name": name,
                        "response": {
                            "content": content_val
                        }
                    }
                })
                gemini_role = "user"

            else:
                text = msg.get("content") or ""
                parts.append({"text": text})
                gemini_role = "user"

            if not parts:
                continue

            if gemini_contents and gemini_contents[-1]["role"] == gemini_role:
                gemini_contents[-1]["parts"].extend(parts)
            else:
                gemini_contents.append({
                    "role": gemini_role,
                    "parts": parts
                })

        return gemini_contents

    def _build_gemini_tools(self, tools: list | None) -> list:
        gemini_tools = []
        if not tools:
            return gemini_tools
        declarations = []
        for t in tools:
            if not isinstance(t, dict):
                continue
            func = t.get("function") or {}
            name = func.get("name") or ""
            desc = func.get("description") or ""
            params = func.get("parameters") or {"type": "object", "properties": {}, "required": []}
            sanitized_params = self._sanitize_schema(params)
            declarations.append({
                "name": name,
                "description": desc,
                "parameters": sanitized_params
            })
        if declarations:
            gemini_tools.append({
                "function_declarations": declarations
            })
        return gemini_tools

    def _chat_body(
        self,
        messages: list,
        tools: list | None,
        system: str | None,
        *,
        include_thoughts: bool = False,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "contents": self._build_contents(messages),
            "generationConfig": self._generation_config(include_thoughts=include_thoughts),
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        gemini_tools = self._build_gemini_tools(tools)
        if gemini_tools:
            body["tools"] = gemini_tools
            body["tool_config"] = {
                "function_calling_config": {
                    "mode": "AUTO"
                }
            }
        return body

    def _ingest_parts(
        self,
        parts: list,
        *,
        text_pieces: list,
        reasoning_pieces: list,
        tool_calls: list,
        part_offset: int,
        on_delta: Optional[Callable[[str], None]] = None,
        on_reasoning_delta: Optional[Callable[[str], None]] = None,
        on_tool_hint: Optional[Callable[[str], None]] = None,
    ) -> int:
        """Parse Gemini content parts; return next part_offset for stable tool ids."""
        for i, p in enumerate(parts):
            if not isinstance(p, dict):
                continue
            idx = part_offset + i
            if p.get("thought") and "text" in p:
                piece = p.get("text") or ""
                if piece:
                    reasoning_pieces.append(piece)
                    if on_reasoning_delta is not None:
                        on_reasoning_delta(piece)
            elif "text" in p:
                piece = p.get("text") or ""
                if piece:
                    text_pieces.append(piece)
                    if on_delta is not None:
                        on_delta(piece)
            elif "functionCall" in p:
                fc = p["functionCall"] or {}
                name = fc.get("name") or ""
                args = fc.get("args") or {}
                tc_id = f"call_{name}_{idx}"
                tc_entry = {
                    "id": tc_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(args)
                    }
                }
                # Gemini 3+ returns a thoughtSignature with each functionCall
                # and REQUIRES it echoed back in the next turn's history.
                sig = p.get("thoughtSignature") or fc.get("thoughtSignature")
                if sig:
                    tc_entry["thought_signature"] = sig
                tool_calls.append(tc_entry)
                if name and on_tool_hint is not None:
                    on_tool_hint(name)
        return part_offset + len(parts)

    def complete(self, task_prompt: str, *, system: str = SYSTEM_PROMPT) -> DriverResponse:
        url = f"{self.base_url}/models/{self.model}:generateContent?key={self._key()}"

        gen_config = self._generation_config()

        body = {
            "contents": [{"role": "user", "parts": [{"text": task_prompt}]}],
            "generationConfig": gen_config
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}

        data = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"}

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
                    latency_ms=(time.time() - t0) * 1000.0
                )
            except Exception as e:
                return DriverResponse(
                    text="", model=self.name, error=repr(e),
                    latency_ms=(time.time() - t0) * 1000.0
                )

            latency = (time.time() - t0) * 1000.0

            try:
                candidates = raw.get("candidates") or []
                text_pieces = []
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts") or []
                    for p in parts:
                        if isinstance(p, dict) and "text" in p and not p.get("thought"):
                            text_pieces.append(p["text"])
                text = "".join(text_pieces)
            except (AttributeError, TypeError, IndexError):
                return DriverResponse(
                    text="", model=self.name,
                    error=f"unexpected response: {str(raw)[:300]}", latency_ms=latency
                )

            usage = raw.get("usageMetadata", {}) or {}
            tokens_in = int(usage.get("promptTokenCount", 0) or 0)
            tokens_out = int(usage.get("candidatesTokenCount", 0) or 0)
            # Gemini 2.5 caches stable prompt prefixes IMPLICITLY (no request
            # flag needed). Surface the cached-token count so the cost meter can
            # credit the savings, matching the OpenAI driver's cache_read_tokens.
            cache_read = int(usage.get("cachedContentTokenCount", 0) or 0)

            return DriverResponse(
                text=text,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                latency_ms=latency,
                model=self.name,
                meta={"cache_read_tokens": cache_read} if cache_read else {}
            )

        return with_retry(_call, sleep=self._sleep)

    def chat(self, messages: list, *, tools: list | None = None, system: str | None = None) -> DriverResponse:
        url = f"{self.base_url}/models/{self.model}:generateContent?key={self._key()}"
        body = self._chat_body(messages, tools, system)
        data = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"}

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
                    latency_ms=(time.time() - t0) * 1000.0
                )
            except Exception as e:
                return DriverResponse(
                    text="", model=self.name, error=repr(e),
                    latency_ms=(time.time() - t0) * 1000.0
                )

            latency = (time.time() - t0) * 1000.0

            try:
                candidates = raw.get("candidates") or []
                text_pieces: list = []
                reasoning_pieces: list = []
                tool_calls: list = []
                finish_reason = ""

                if candidates:
                    cand = candidates[0] or {}
                    finish_reason = cand.get("finishReason") or ""
                    parts = cand.get("content", {}).get("parts") or []
                    self._ingest_parts(
                        parts,
                        text_pieces=text_pieces,
                        reasoning_pieces=reasoning_pieces,
                        tool_calls=tool_calls,
                        part_offset=0,
                    )
                full_text = "".join(text_pieces)
            except (AttributeError, TypeError, IndexError):
                return DriverResponse(
                    text="", model=self.name,
                    error=f"unexpected response: {str(raw)[:300]}", latency_ms=latency
                )

            usage = raw.get("usageMetadata", {}) or {}
            tokens_in = int(usage.get("promptTokenCount", 0) or 0)
            tokens_out = int(usage.get("candidatesTokenCount", 0) or 0)
            # Implicit-cache credit (see chat()): surface cached prefix tokens.
            cache_read = int(usage.get("cachedContentTokenCount", 0) or 0)

            return DriverResponse(
                text=full_text,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                latency_ms=latency,
                model=self.name,
                meta={
                    "tool_calls": tool_calls,
                    "finish_reason": finish_reason,
                    "reasoning": "".join(reasoning_pieces),
                    "cache_read_tokens": cache_read,
                }
            )

        return with_retry(_call, sleep=self._sleep)

    def chat_stream(
        self,
        messages: list,
        *,
        tools: list | None = None,
        system: str | None = None,
        on_delta=None,
        on_reasoning_delta=None,
        on_tool_hint=None,
    ) -> DriverResponse:
        """Streaming counterpart of chat() over Gemini's streamGenerateContent SSE.

        Uses ``:streamGenerateContent?alt=sse`` so each chunk is a full GenerateContent
        JSON object on a ``data:`` line. Emits text via on_delta, thought summaries
        (parts with ``thought: true``) via on_reasoning_delta, and functionCall names
        via on_tool_hint — matching ConversationalSession's stream thread contract.
        """
        url = (
            f"{self.base_url}/models/{self.model}:streamGenerateContent"
            f"?alt=sse&key={self._key()}"
        )
        body = self._chat_body(messages, tools, system, include_thoughts=True)
        data = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if on_delta is None:
            on_delta = lambda _t: None

        t0 = time.time()
        text_pieces: list = []
        reasoning_pieces: list = []
        tool_calls: list = []
        finish_reason = ""
        tokens_in = 0
        tokens_out = 0
        cache_read = 0
        part_offset = 0
        stream_started = False

        try:
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                for raw_line in resp:
                    line = raw_line.decode("utf-8", "replace").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if not payload or payload == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(payload)
                    except Exception:
                        continue

                    usage = chunk.get("usageMetadata") or {}
                    if usage:
                        tokens_in = int(usage.get("promptTokenCount", tokens_in) or tokens_in)
                        tokens_out = int(usage.get("candidatesTokenCount", tokens_out) or tokens_out)
                        cache_read = int(
                            usage.get("cachedContentTokenCount", cache_read) or cache_read
                        )

                    candidates = chunk.get("candidates") or []
                    if not candidates:
                        continue
                    cand = candidates[0] or {}
                    fr = cand.get("finishReason") or ""
                    if fr:
                        finish_reason = fr
                    parts = (cand.get("content") or {}).get("parts") or []
                    if not parts:
                        continue

                    before_text = len(text_pieces)
                    before_reason = len(reasoning_pieces)
                    before_tools = len(tool_calls)
                    part_offset = self._ingest_parts(
                        parts,
                        text_pieces=text_pieces,
                        reasoning_pieces=reasoning_pieces,
                        tool_calls=tool_calls,
                        part_offset=part_offset,
                        on_delta=on_delta,
                        on_reasoning_delta=on_reasoning_delta,
                        on_tool_hint=on_tool_hint,
                    )
                    if (
                        len(text_pieces) > before_text
                        or len(reasoning_pieces) > before_reason
                        or len(tool_calls) > before_tools
                    ):
                        stream_started = True

        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            return DriverResponse(
                text="".join(text_pieces),
                model=self.name,
                error=f"HTTP {e.code}: {detail}",
                latency_ms=(time.time() - t0) * 1000.0,
                meta={"stream_started": stream_started},
            )
        except Exception as e:
            return DriverResponse(
                text="".join(text_pieces),
                model=self.name,
                error=repr(e),
                latency_ms=(time.time() - t0) * 1000.0,
                meta={"stream_started": stream_started},
            )

        latency = (time.time() - t0) * 1000.0
        return DriverResponse(
            text="".join(text_pieces),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency,
            model=self.name,
            meta={
                "tool_calls": tool_calls,
                "finish_reason": finish_reason,
                "reasoning": "".join(reasoning_pieces),
                "cache_read_tokens": cache_read,
                "stream_started": stream_started,
            },
        )
