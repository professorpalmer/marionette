from __future__ import annotations

"""BedrockDriver: interactive pilot over AWS Bedrock Converse.

Wraps ``puppetmaster.bedrock.bedrock_chat`` (imported at call time — no new
pip deps). Auth comes from process env (Marionette injects AWS_* / BEDROCK_*
from Settings BYOK). Converse stamps prompt-cache ``cachePoint``s and reports
``cache_read_tokens`` / ``cache_write_tokens`` so session cost meters and
``cache_savings_usd`` match other providers. Bedrock invoke is non-SSE;
``chat_stream`` falls back to ``chat`` and emits one ``on_delta`` with the
full text.
"""

import json
import os
import time
from typing import Any, Callable, Optional

from .base import DriverResponse, SYSTEM_PROMPT
from .retry import with_retry
from pmharness.reasoning import extract_reasoning, strip_think_blocks


class BedrockDriver:
    supports_streaming = False

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
        return extra

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
        wire = list(messages or [])
        if system:
            # bedrock_chat / build_anthropic_invoke_body pull system role messages.
            wire = [{"role": "system", "content": system}] + [
                m for m in wire if m.get("role") != "system"
            ]

        def _call() -> DriverResponse:
            return self._invoke(wire, tools=tools)

        return with_retry(_call)

    def chat_stream(
        self,
        messages: list,
        *,
        tools: Optional[list] = None,
        system: Optional[str] = None,
        on_delta=None,
    ) -> DriverResponse:
        """Non-SSE Bedrock invoke: run chat, then emit one full-text delta."""
        resp = self.chat(messages, tools=tools, system=system)
        if on_delta is not None and resp.text and not resp.error:
            on_delta(resp.text)
        if resp.meta is not None:
            resp.meta["stream_started"] = bool(resp.text and not resp.error)
        return resp
