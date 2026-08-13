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
from pmharness.think_scrubber import StreamingThinkScrubber


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
        extra_body: dict | None = None,
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
        # Provider-specific request fields (e.g. an OpenCode Go model's native
        # `thinking` / `reasoning_effort` dialect) merged into every request.
        self.extra_body = extra_body or {}
        self.enable_reasoning = enable_reasoning
        self.session_id = session_id
        # Set by _key() when a credential-pool entry is selected.
        self._pool_provider: str | None = None
        self._pool_entry_id: str | None = None

    def _key(self) -> str:
        """Resolve API key: credential pool first, then process env."""
        self._pool_provider = None
        self._pool_entry_id = None
        try:
            from harness.credential_pool import resolve_entry_for_env
            entry = resolve_entry_for_env(self.api_key_env)
            if entry is not None and entry.runtime_token:
                self._pool_provider = entry.provider
                self._pool_entry_id = entry.id
                return entry.runtime_token
        except Exception:
            pass
        key = os.environ.get(self.api_key_env, "").strip()
        if not key:
            raise RuntimeError(f"missing API key in env var {self.api_key_env}")
        return key

    def _uses_openai_gpt5_chat_parameters(self) -> bool:
        return (
            self.base_url == "https://api.openai.com/v1"
            and self.model.lower().startswith("gpt-5")
        )

    def _output_token_limit_field(self) -> str:
        """Return the Chat Completions output-limit field for this endpoint/model."""
        return (
            "max_completion_tokens"
            if self._uses_openai_gpt5_chat_parameters()
            else "max_tokens"
        )

    def _apply_temperature(self, body: dict) -> None:
        if not self._uses_openai_gpt5_chat_parameters():
            body["temperature"] = self.temperature

    def _pool_rotate_backoff(
        self,
        code: int,
        detail: str,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Pause after a pool rotate so the next key is not stampeded immediately.

        Rate limits honor Retry-After (capped); other rotate-eligible codes take
        a short fixed pause. Classification comes from error_classifier so the
        rotate path does not invent its own retry policy.
        """
        from . import error_classifier

        err_class = error_classifier.classify(code, detail)
        if err_class == error_classifier.ErrorClass.RATE_LIMIT:
            retry_after = error_classifier.parse_retry_after(detail)
            delay = float(retry_after) if retry_after is not None else 1.0
            sleep(min(max(delay, 0.5), 20.0))
            return
        sleep(0.25)

    def _pool_rotate_on_http_error(
        self,
        code: int,
        detail: str,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> str | None:
        """On 429 plan-limit / 402 / auth fail, rotate pool and return next key."""
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
                self._pool_rotate_backoff(code, detail, sleep=sleep)
                # Re-select so _pool_entry_id tracks the new entry
                self._key()
                return nxt
        except Exception:
            pass
        return None

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
        """Stamp provider extras, explicit cache_control (Claude/Qwen), and the
        OpenRouter session_id.

        Best-effort: never raises; automatic-cache models are left untouched.
        """
        for field, value in self.extra_body.items():
            # Conversation shape and streaming are the caller's; extras only add
            # provider-dialect knobs on top.
            if field not in ("model", "messages", "stream", "tools", "tool_choice"):
                body[field] = value
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
        """Return (cache_read_tokens, cache_write_tokens) via shared normalization."""
        from .token_usage import coerce_token_usage_record

        detail = coerce_token_usage_record(usage or {})
        return int(detail.cache_read), int(detail.cache_write)

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

    @staticmethod
    def _usage_reports_cache_read(usage: dict) -> bool:
        """True when the provider usage blob explicitly carries a cache-read field."""
        if not isinstance(usage, dict):
            return False
        keys = (
            "cache_read_tokens",
            "cache_read_input_tokens",
            "cached_input_tokens",
            "cacheReadTokens",
            "cachedInputTokens",
            "cacheReadInputTokens",
            "cacheReadInputTokenCount",
            "cached_tokens",
            "cachedTokens",
            "tokens_cached",
        )
        if any(k in usage for k in keys):
            return True
        for nest_key in (
            "prompt_tokens_details",
            "input_tokens_details",
            "promptTokensDetails",
            "inputTokensDetails",
        ):
            nest = usage.get(nest_key)
            if isinstance(nest, dict) and OpenAICompatDriver._usage_reports_cache_read(nest):
                return True
        return False

    @staticmethod
    def _usage_reports_cache_write(usage: dict) -> bool:
        """True when the provider usage blob explicitly carries a cache-write field."""
        if not isinstance(usage, dict):
            return False
        keys = (
            "cache_write_tokens",
            "cache_creation_input_tokens",
            "cacheWriteTokens",
            "cacheWriteInputTokens",
            "cacheWriteInputTokenCount",
            "cache_write_input_tokens",
            "tokens_cache_write",
            "cache_creation_tokens",
            "cacheCreationTokens",
        )
        if any(k in usage for k in keys):
            return True
        for nest_key in (
            "prompt_tokens_details",
            "input_tokens_details",
            "promptTokensDetails",
            "inputTokensDetails",
        ):
            nest = usage.get(nest_key)
            if isinstance(nest, dict) and OpenAICompatDriver._usage_reports_cache_write(nest):
                return True
        return False

    @classmethod
    def _usage_meta(cls, usage: dict) -> dict:
        """Shared cache + billed-cost fields for DriverResponse.meta.

        Omits cache_read_tokens/cache_write_tokens when the provider usage
        blob has no corresponding field. Explicit provider zeros are kept.
        """
        from .token_usage import attach_modality_fields, coerce_token_usage_record

        usage = usage or {}
        detail = coerce_token_usage_record(usage)
        meta = {
            "raw_usage": usage,
        }
        if cls._usage_reports_cache_read(usage):
            meta["cache_read_tokens"] = int(detail.cache_read)
        if cls._usage_reports_cache_write(usage):
            meta["cache_write_tokens"] = int(detail.cache_write)
        cost = cls._cost_from_usage(usage)
        if cost is not None:
            meta["provider_cost_usd"] = cost
        attach_modality_fields(meta, detail)
        return meta

    @staticmethod
    def _served_model_from_payload(payload: dict) -> str | None:
        """Return non-empty response ``model`` when the provider reports one."""
        if not isinstance(payload, dict):
            return None
        model = payload.get("model")
        if isinstance(model, str) and model.strip():
            return model.strip()
        return None

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
            self._output_token_limit_field(): self.max_tokens,
        }
        self._apply_temperature(body)
        self._prepare_body(
            body,
            messages=body["messages"],
            system=system,
            session_id=session_id,
        )
        data = json.dumps(body).encode("utf-8")

        def _call() -> DriverResponse:
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._key()}",
            }
            headers.update(self.extra_headers)
            t0 = time.time()
            raw = None
            last_err = None
            for attempt in range(2):
                try:
                    req = urllib.request.Request(
                        url, data=data, headers=headers, method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                        raw = json.loads(resp.read().decode("utf-8"))
                    last_err = None
                    break
                except urllib.error.HTTPError as e:
                    detail = e.read().decode("utf-8", "replace")[:500]
                    last_err = (e.code, detail)
                    if attempt == 0:
                        nxt = self._pool_rotate_on_http_error(e.code, detail)
                        if nxt:
                            headers["Authorization"] = f"Bearer {self._key()}"
                            continue
                    return DriverResponse(
                        text="", model=self.name,
                        error=f"HTTP {e.code}: {detail}",
                        latency_ms=(time.time() - t0) * 1000.0,
                    )
                except Exception as e:  # network, timeout, json
                    return DriverResponse(
                        text="", model=self.name, error=repr(e),
                        latency_ms=(time.time() - t0) * 1000.0,
                    )
            if last_err is not None:
                code, detail = last_err
                return DriverResponse(
                    text="", model=self.name, error=f"HTTP {code}: {detail}",
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
            served = self._served_model_from_payload(raw if isinstance(raw, dict) else {})
            if served:
                meta["served_model"] = served
            from .token_usage import coerce_token_usage_record

            usage_detail = coerce_token_usage_record(usage)
            return DriverResponse(
                text=text,
                tokens_in=int(usage_detail.tokens_in),
                tokens_out=int(usage_detail.tokens_out),
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
            self._output_token_limit_field(): self.max_tokens,
        }
        self._apply_temperature(body)
        if self.enable_reasoning and not (
            tools and self._uses_openai_gpt5_chat_parameters()
        ):
            body["reasoning"] = {"max_tokens": 1024}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
            if self._uses_openai_gpt5_chat_parameters():
                body["reasoning_effort"] = "none"
        self._prepare_body(
            body,
            messages=full_messages,
            system=system,
            session_id=session_id,
        )

        data = json.dumps(body).encode("utf-8")

        def _call() -> DriverResponse:
            nonlocal data
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._key()}",
            }
            headers.update(self.extra_headers)
            t0 = time.time()
            raw = None
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
                        nxt = self._pool_rotate_on_http_error(e2.code, d2)
                        if nxt:
                            headers["Authorization"] = f"Bearer {self._key()}"
                            try:
                                req = urllib.request.Request(
                                    url, data=data, headers=headers, method="POST",
                                )
                                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                                    raw = json.loads(resp.read().decode("utf-8"))
                            except urllib.error.HTTPError as e3:
                                d3 = e3.read().decode("utf-8", "replace")[:500]
                                return DriverResponse(
                                    text="", model=self.name,
                                    error=f"HTTP {e3.code}: {d3}",
                                    latency_ms=(time.time() - t0) * 1000.0,
                                )
                            except Exception as e3:
                                return DriverResponse(
                                    text="", model=self.name, error=repr(e3),
                                    latency_ms=(time.time() - t0) * 1000.0,
                                )
                        else:
                            return DriverResponse(
                                text="", model=self.name,
                                error=f"HTTP {e2.code}: {d2}",
                                latency_ms=(time.time() - t0) * 1000.0,
                            )
                    except Exception as e2:
                        return DriverResponse(text="", model=self.name, error=repr(e2),
                                              latency_ms=(time.time() - t0) * 1000.0)
                else:
                    nxt = self._pool_rotate_on_http_error(e.code, detail)
                    if nxt:
                        headers["Authorization"] = f"Bearer {self._key()}"
                        try:
                            req = urllib.request.Request(
                                url, data=data, headers=headers, method="POST",
                            )
                            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                                raw = json.loads(resp.read().decode("utf-8"))
                        except urllib.error.HTTPError as e2:
                            d2 = e2.read().decode("utf-8", "replace")[:500]
                            return DriverResponse(
                                text="", model=self.name,
                                error=f"HTTP {e2.code}: {d2}",
                                latency_ms=(time.time() - t0) * 1000.0,
                            )
                        except Exception as e2:
                            return DriverResponse(
                                text="", model=self.name, error=repr(e2),
                                latency_ms=(time.time() - t0) * 1000.0,
                            )
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
            served = self._served_model_from_payload(raw if isinstance(raw, dict) else {})
            if served:
                meta["served_model"] = served
            from .token_usage import coerce_token_usage_record

            usage_detail = coerce_token_usage_record(usage)
            return DriverResponse(
                text=pure_text,
                tokens_in=int(usage_detail.tokens_in),
                tokens_out=int(usage_detail.tokens_out),
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
            self._output_token_limit_field(): self.max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        self._apply_temperature(body)
        if self.enable_reasoning and not (
            tools and self._uses_openai_gpt5_chat_parameters()
        ):
            body["reasoning"] = {"max_tokens": 1024}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
            if self._uses_openai_gpt5_chat_parameters():
                body["reasoning_effort"] = "none"
        self._prepare_body(
            body,
            messages=full_messages,
            system=system,
            session_id=session_id,
        )

        data = json.dumps(body).encode("utf-8")

        def _call() -> DriverResponse:
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._key()}",
            }
            headers.update(self.extra_headers)
            t0 = time.time()
            full_text = ""
            reasoning_pieces = []
            assembled_tool_calls = {}
            finish_reason = ""
            tokens_in = 0
            tokens_out = 0
            cached_tokens = 0
            cache_write_tokens = 0
            stream_has_cache_read = False
            stream_has_cache_write = False
            provider_cost_usd = None
            stream_raw_usage = None
            served_model = None
            stream_started = False
            # Hermes-style: suppress <think>/<reasoning> tags that split across
            # SSE deltas so visible on_delta never leaks mid-stream reasoning.
            think_scrubber = StreamingThinkScrubber()

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

                            chunk_served = self._served_model_from_payload(chunk)
                            if chunk_served:
                                served_model = chunk_served

                            # Process token usage if present
                            chunk_usage = chunk.get("usage")
                            if chunk_usage:
                                from .token_usage import coerce_token_usage_record

                                usage_detail = coerce_token_usage_record(chunk_usage)
                                tokens_in = int(usage_detail.tokens_in)
                                tokens_out = int(usage_detail.tokens_out)
                                if self._usage_reports_cache_read(chunk_usage):
                                    stream_has_cache_read = True
                                    cached_tokens = int(usage_detail.cache_read)
                                if self._usage_reports_cache_write(chunk_usage):
                                    stream_has_cache_write = True
                                    cache_write_tokens = int(usage_detail.cache_write)
                                stream_raw_usage = chunk_usage
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
                                    full_text += content_delta
                                    visible = think_scrubber.feed(content_delta)
                                    if visible:
                                        on_delta(visible)

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
                # Pool rotate only before any tokens streamed (same safety rule).
                if not stream_started:
                    nxt = self._pool_rotate_on_http_error(e.code, detail)
                    if nxt:
                        return self.chat_stream(
                            messages,
                            tools=tools,
                            system=system,
                            on_delta=on_delta,
                            session_id=session_id,
                            on_reasoning_delta=on_reasoning_delta,
                            on_tool_hint=on_tool_hint,
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

            # Emit any held-back partial-tag tail that was not a real tag.
            try:
                scrub_tail = think_scrubber.flush()
            except Exception:
                scrub_tail = ""
            if scrub_tail:
                on_delta(scrub_tail)

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
            }
            if stream_has_cache_read:
                meta["cache_read_tokens"] = cached_tokens
            if stream_has_cache_write:
                meta["cache_write_tokens"] = cache_write_tokens
            if stream_raw_usage is not None:
                meta["raw_usage"] = stream_raw_usage
            if provider_cost_usd is not None:
                meta["provider_cost_usd"] = provider_cost_usd
            if served_model:
                meta["served_model"] = served_model
            return DriverResponse(
                text=pure_text,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                latency_ms=latency,
                model=self.name,
                meta=meta,
            )

        return with_retry(_call)

