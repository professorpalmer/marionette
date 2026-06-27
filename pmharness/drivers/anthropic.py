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


class AnthropicDriver:
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
