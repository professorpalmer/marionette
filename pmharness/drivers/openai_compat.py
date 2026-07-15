from __future__ import annotations

"""OpenAICompatDriver: drives any OpenAI-compatible chat endpoint. Kimi
(Moonshot), GLM (z.ai), OpenAI, and most open-weights providers all expose this
schema, so one driver covers the whole registry. stdlib-only (urllib) to keep
the rig dependency-light and auditable.

Keys are read from the environment at call time and never logged.
"""

import json
import os
import time
import urllib.request
import urllib.error
from typing import Callable

from .base import DriverResponse, SYSTEM_PROMPT
from .prompt_cache import (
    apply_openai_compat_cache_control,
    maybe_attach_openrouter_session_id,
)
from .retry import with_retry
from pmharness.reasoning import extract_reasoning, strip_think_blocks


class OpenAICompatDriver:
    # Explicit capability flag the conversation loop checks (is True) before using the
    # streaming path -- prevents MagicMock test doubles from accidentally streaming.
    supports_streaming = True

    def __init__(
        self,
        name: str,
        model: str,
        base_url: str,
        api_key_env: str,
        *,
        temperature: float = 0.0,
        max_tokens: int = 1500,
        timeout: int = 90,
        extra_headers: dict | None = None,
        enable_reasoning: bool = False,
        session_id: str | None = None,
    ) -> None:
        self.name = name
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.extra_headers = extra_headers or {}
        self.enable_reasoning = enable_reasoning
        self.session_id = session_id

    def _key(self) -> str:
        key = os.environ.get(self.api_key_env, "").strip()
        if not key:
            raise RuntimeError(f"missing API key in env var {self.api_key_env}")
        return key

    def _reasoning_unsupported(self, code: int, detail: str) -> bool:
        """True when an endpoint rejected the OpenRouter-style `reasoning` field.

        Many OpenAI-compatible endpoints/models do not accept the `reasoning`
        parameter and return a 400 'Unknown parameter: reasoning'. When we see
        that, we disable reasoning for the rest of the session and let the caller
        retry once -- so a model that lacks reasoning support self-heals instead
        of hard-failing every pilot turn.
        """
        if code != 400 or not self.enable_reasoning:
            return False
        d = (detail or "").lower()
        return "reasoning" in d and ("unknown parameter" in d or "unsupported" in d
                                     or "invalid_request" in d or "not supported" in d
                                     or "unexpected" in d)

    def _prepare_body(
        self,
        body: dict,
        *,
        messages: list | None = None,
        system: str | None = None,
        session_id: str | None = None,
    ) -> dict:
        """Stamp explicit cache_control (Claude/Qwen) and OpenRouter session_id.

        Best-effort: never raises; automatic-cache models are left untouched.
        """
        try:
            if "openrouter.ai" in (self.base_url or "").lower():
                # Ask OpenRouter for prompt_tokens_details (cached / cache_write).
                body.setdefault("usage", {"include": True})
            apply_openai_compat_cache_control(body, model=self.model)
            maybe_attach_openrouter_session_id(
                body,
                base_url=self.base_url,
                session_id=session_id if session_id is not None else self.session_id,
                messages=messages if messages is not None else body.get("messages"),
                system=system,
            )
        except Exception:
            pass
        return body

    @staticmethod
    def _cache_fields_from_usage(usage: dict) -> tuple[int, int]:
        """Return (cache_read_tokens, cache_write_tokens) from an OpenAI-style usage blob."""
        usage = usage or {}
        details = usage.get("prompt_tokens_details") or {}
        cached = int(
            details.get("cached_tokens")
            or usage.get("cache_read_input_tokens")
            or 0
        )
        written = int(
            details.get("cache_write_tokens")
            or details.get("cache_write")
            or usage.get("cache_creation_input_tokens")
            or 0
        )
        return cached, written

    @staticmethod
    def _cost_from_usage(usage: dict):
        """Return provider-billed USD from a usage blob, or None if absent.

        OpenRouter always includes ``usage.cost`` (credits charged to the
        account). Prefer this over token*catalog math -- cache-read multipliers
        and registry prices drift from what was actually billed.
        """
        usage = usage or {}
        raw = usage.get("cost")
        if raw is None:
            return None
        try:
            val = float(raw)
        except (TypeError, ValueError):
            return None
        if val != val or val < 0.0:  # NaN or negative
            return None
        return val

    @classmethod
    def _usage_meta(cls, usage: dict) -> dict:
        """Shared cache + billed-cost fields for DriverResponse.meta."""
        usage = usage or {}
        cached_tokens, cache_write_tokens = cls._cache_fields_from_usage(usage)
        meta = {
            "cache_read_tokens": cached_tokens,
            "cache_write_tokens": cache_write_tokens,
            "raw_usage": usage,
        }
        cost = cls._cost_from_usage(usage)
        if cost is not None:
            meta["provider_cost_usd"] = cost
        return meta

    def complete(
        self,
        task_prompt: str,
        *,
        system: str = SYSTEM_PROMPT,
        session_id: str | None = None,
    ) -> DriverResponse:
        url = f"{self.base_url}/chat/completions"
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": task_prompt},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        self._prepare_body(
            body,
            messages=body["messages"],
            system=system,
            session_id=session_id,
        )
        data = json.dumps(body).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._key()}",
        }
        headers.update(self.extra_headers)

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
            except Exception as e:  # network, timeout, json
                return DriverResponse(
                    text="", model=self.name, error=repr(e),
                    latency_ms=(time.time() - t0) * 1000.0,
                )

            latency = (time.time() - t0) * 1000.0
            try:
                text = raw["choices"][0]["message"]["content"] or ""
            except (KeyError, IndexError, TypeError):
                return DriverResponse(
                    text="", model=self.name, error=f"unexpected response shape: {str(raw)[:300]}",
                    latency_ms=latency,
                )
            usage = raw.get("usage", {}) or {}
            meta = self._usage_meta(usage)
            meta["raw_finish"] = (
                raw["choices"][0].get("finish_reason") if raw.get("choices") else None
            )
            return DriverResponse(
                text=text,
                tokens_in=int(usage.get("prompt_tokens", 0) or 0),
                tokens_out=int(usage.get("completion_tokens", 0) or 0),
                latency_ms=latency,
                model=self.name,
                meta=meta,
            )

        return with_retry(_call)

    def chat(
        self,
        messages: list,
        *,
        tools: list | None = None,
        system: str | None = None,
        session_id: str | None = None,
    ) -> DriverResponse:
        url = f"{self.base_url}/chat/completions"
        full_messages = []
        if system:
            full_messages.append({"role": "system", "content": system})
        full_messages.extend(messages)

        body = {
            "model": self.model,
            "messages": full_messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if self.enable_reasoning:
            body["reasoning"] = {"max_tokens": 1024}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        self._prepare_body(
            body,
            messages=full_messages,
            system=system,
            session_id=session_id,
        )

        data = json.dumps(body).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._key()}",
        }
        headers.update(self.extra_headers)

        def _call() -> DriverResponse:
            nonlocal data
            t0 = time.time()
            try:
                req = urllib.request.Request(url, data=data, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:500]
                if self._reasoning_unsupported(e.code, detail) and body.get("reasoning") is not None:
                    # Drop the unsupported reasoning field for the rest of the
                    # session and retry once so the pilot turn succeeds.
                    self.enable_reasoning = False
                    body.pop("reasoning", None)
                    data = json.dumps(body).encode("utf-8")
                    try:
                        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
                        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                            raw = json.loads(resp.read().decode("utf-8"))
                    except urllib.error.HTTPError as e2:
                        d2 = e2.read().decode("utf-8", "replace")[:500]
                        return DriverResponse(text="", model=self.name,
                                              error=f"HTTP {e2.code}: {d2}",
                                              latency_ms=(time.time() - t0) * 1000.0)
                    except Exception as e2:
                        return DriverResponse(text="", model=self.name, error=repr(e2),
                                              latency_ms=(time.time() - t0) * 1000.0)
                else:
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
                choice = raw["choices"][0]
                message_obj = choice["message"]
                text = message_obj.get("content") or ""
                tool_calls = message_obj.get("tool_calls") or []
                finish_reason = choice.get("finish_reason") or ""
            except (KeyError, IndexError, TypeError):
                return DriverResponse(
                    text="", model=self.name, error=f"unexpected response shape: {str(raw)[:300]}",
                    latency_ms=latency,
                )

            reasoning = extract_reasoning(message_obj)
            pure_text = strip_think_blocks(text)

            usage = raw.get("usage", {}) or {}
            meta = self._usage_meta(usage)
            meta.update({
                "tool_calls": tool_calls,
                "reasoning": reasoning,
                "finish_reason": finish_reason,
            })
            return DriverResponse(
                text=pure_text,
                tokens_in=int(usage.get("prompt_tokens", 0) or 0),
                tokens_out=int(usage.get("completion_tokens", 0) or 0),
                latency_ms=latency,
                model=self.name,
                meta=meta,
            )

        return with_retry(_call)

    def chat_stream(
        self,
        messages: list,
        *,
        tools: list | None = None,
        system: str | None = None,
        on_delta: Callable[[str], None],
        session_id: str | None = None,
        on_reasoning_delta: Callable[[str], None] | None = None,
        on_tool_hint: Callable[[str], None] | None = None,
    ) -> DriverResponse:
        url = f"{self.base_url}/chat/completions"
        full_messages = []
        if system:
            full_messages.append({"role": "system", "content": system})
        full_messages.extend(messages)

        body = {
            "model": self.model,
            "messages": full_messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if self.enable_reasoning:
            body["reasoning"] = {"max_tokens": 1024}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        self._prepare_body(
            body,
            messages=full_messages,
            system=system,
            session_id=session_id,
        )

        data = json.dumps(body).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._key()}",
        }
        headers.update(self.extra_headers)

        def _call() -> DriverResponse:
            t0 = time.time()
            full_text = ""
            reasoning_pieces = []
            assembled_tool_calls = {}
            finish_reason = ""
            tokens_in = 0
            tokens_out = 0
            cached_tokens = 0
            cache_write_tokens = 0
            provider_cost_usd = None
            stream_started = False

            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    for line in resp:
                        line_str = line.decode("utf-8", "replace").strip()
                        if not line_str:
                            continue
                        if line_str.startswith("data: "):
                            data_str = line_str[6:].strip()
                            if data_str == "[DONE]":
                                break
                            try:
                                chunk = json.loads(data_str)
                            except Exception:
                                continue

                            # Process token usage if present
                            chunk_usage = chunk.get("usage")
                            if chunk_usage:
                                tokens_in = int(chunk_usage.get("prompt_tokens", 0) or 0)
                                tokens_out = int(chunk_usage.get("completion_tokens", 0) or 0)
                                cached_tokens, cache_write_tokens = self._cache_fields_from_usage(
                                    chunk_usage
                                )
                                step_cost = self._cost_from_usage(chunk_usage)
                                if step_cost is not None:
                                    provider_cost_usd = step_cost

                            choices = chunk.get("choices") or []
                            if choices:
                                choice = choices[0]
                                delta = choice.get("delta") or {}

                                # Content text delta
                                content_delta = delta.get("content") or ""
                                if content_delta:
                                    stream_started = True
                                    on_delta(content_delta)
                                    full_text += content_delta

                                # Reasoning delta -- forward live so the UI can
                                # paint Thought while tokens climb (GLM/OR
                                # often stream reasoning before content).
                                reasoning_delta = (
                                    delta.get("reasoning")
                                    or delta.get("reasoning_content")
                                    or ""
                                )
                                if reasoning_delta:
                                    stream_started = True
                                    reasoning_pieces.append(reasoning_delta)
                                    if on_reasoning_delta is not None:
                                        on_reasoning_delta(reasoning_delta)

                                # Tool calls delta
                                delta_tool_calls = delta.get("tool_calls") or []
                                for tc in delta_tool_calls:
                                    idx = tc.get("index")
                                    if idx is None:
                                        continue
                                    tc_func = tc.get("function") or {}
                                    name_piece = tc_func.get("name") or ""
                                    if idx not in assembled_tool_calls:
                                        assembled_tool_calls[idx] = {
                                            "id": tc.get("id") or "",
                                            "type": tc.get("type") or "function",
                                            "function": {
                                                "name": name_piece,
                                                "arguments": tc_func.get("arguments") or ""
                                            }
                                        }
                                    else:
                                        existing = assembled_tool_calls[idx]
                                        if tc.get("id"):
                                            existing["id"] = tc.get("id")
                                        if tc.get("type"):
                                            existing["type"] = tc.get("type")
                                        if name_piece:
                                            existing["function"]["name"] += name_piece
                                        if tc_func.get("arguments"):
                                            existing["function"]["arguments"] += tc_func["arguments"]

                                    # Hint only when the name advanced this chunk
                                    # (arguments-only deltas would otherwise spam).
                                    _hint = assembled_tool_calls[idx]["function"]["name"]
                                    if name_piece and _hint and on_tool_hint is not None:
                                        on_tool_hint(_hint)

                                chunk_finish_reason = choice.get("finish_reason")
                                if chunk_finish_reason:
                                    finish_reason = chunk_finish_reason

            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:500]
                # Endpoint rejected the `reasoning` field: disable it for the
                # session and fall back to the non-streaming chat() (which shares
                # the retry path) so the turn still succeeds. Only safe before any
                # tokens streamed -- otherwise a partial stream would double-emit.
                if (not stream_started and self._reasoning_unsupported(e.code, detail)
                        and body.get("reasoning") is not None):
                    self.enable_reasoning = False
                    return self.chat(
                        messages, tools=tools, system=system, session_id=session_id,
                    )
                return DriverResponse(
                    text="", model=self.name, error=f"HTTP {e.code}: {detail}",
                    latency_ms=(time.time() - t0) * 1000.0,
                    meta={"stream_started": stream_started},
                )
            except Exception as e:
                return DriverResponse(
                    text="", model=self.name, error=repr(e),
                    latency_ms=(time.time() - t0) * 1000.0,
                    meta={"stream_started": stream_started},
                )

            latency = (time.time() - t0) * 1000.0

            # Build message_obj to pass to extract_reasoning
            message_obj = {"content": full_text}
            accumulated_reasoning = "".join(reasoning_pieces)
            if accumulated_reasoning:
                message_obj["reasoning"] = accumulated_reasoning
                message_obj["reasoning_content"] = accumulated_reasoning

            reasoning = extract_reasoning(message_obj)
            pure_text = strip_think_blocks(full_text)

            tool_calls = [assembled_tool_calls[i] for i in sorted(assembled_tool_calls.keys())]

            meta = {
                "tool_calls": tool_calls,
                "reasoning": reasoning,
                "finish_reason": finish_reason,
                "stream_started": stream_started,
                "cache_read_tokens": cached_tokens,
                "cache_write_tokens": cache_write_tokens,
            }
            if provider_cost_usd is not None:
                meta["provider_cost_usd"] = provider_cost_usd
            return DriverResponse(
                text=pure_text,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                latency_ms=latency,
                model=self.name,
                meta=meta,
            )

        return with_retry(_call)

