"""Request-scoped transport for an owned local llama.cpp child."""
from __future__ import annotations

from copy import copy, deepcopy
from typing import Callable, Optional

from pmharness.drivers.base import DriverResponse, SYSTEM_PROMPT
from pmharness.drivers.openai_compat import OpenAICompatDriver


class ManagedLocalDriver(OpenAICompatDriver):
    def __init__(self, *, manager, spec: str, **kwargs):
        super().__init__(**kwargs)
        self._manager = manager
        self._spec = spec

    def fork_for_compaction(self, *, model: str):
        if model and model != self.model:
            raise ValueError("Managed compaction must use the installed model")
        local = copy(self)
        local.extra_headers = deepcopy(self.extra_headers)
        local.extra_body = deepcopy(self.extra_body)
        for attr in ("_request_body_observer", "_build_body", "_build_chat_body"):
            local.__dict__.pop(attr, None)
        local._pool_provider = None
        local._pool_entry_id = None
        return local

    def _transport(self):
        return OpenAICompatDriver(
            name=self.name, model=self.model, base_url=self.base_url,
            api_key_env=self.api_key_env, temperature=self.temperature,
            max_tokens=self.max_tokens, timeout=self.timeout,
            extra_headers=dict(self.extra_headers), extra_body=deepcopy(self.extra_body),
            enable_reasoning=self.enable_reasoning, session_id=self.session_id,
            vendor=self.vendor, allow_keyless=self.allow_keyless,
        )

    def _leased_method(self, method):
        from functools import wraps

        @wraps(method)
        def request(*args, **kwargs):
            return self._request(method, *args, **kwargs)
        return request

    def _request(self, method, *args, **kwargs):
        with self._manager.request_scope(self._spec) as endpoint:
            if isinstance(method, str):
                driver = self._transport()
                driver.model = endpoint.model
                method = getattr(driver, method)
            else:
                driver = method.__self__
            driver.base_url = endpoint.base_url
            try:
                return method(*args, **kwargs)
            finally:
                if not driver.enable_reasoning:
                    self.enable_reasoning = False

    def complete(
        self, task_prompt: str, *, system: str = SYSTEM_PROMPT,
        session_id: Optional[str] = None,
    ) -> DriverResponse:
        return self._request("complete", task_prompt, system=system, session_id=session_id)

    def chat(
        self, messages: list, *, tools: Optional[list] = None,
        system: Optional[str] = None, session_id: Optional[str] = None,
        max_attempts: int = 4, is_cancelled: Optional[Callable[[], bool]] = None,
    ) -> DriverResponse:
        return self._request(
            "chat", messages, tools=tools, system=system, session_id=session_id,
            max_attempts=max_attempts, is_cancelled=is_cancelled,
        )

    def chat_stream(
        self, messages: list, *, tools: Optional[list] = None,
        system: Optional[str] = None, on_delta: Callable[[str], None],
        session_id: Optional[str] = None,
        on_reasoning_delta: Optional[Callable[[str], None]] = None,
        on_tool_hint: Optional[Callable[[str], None]] = None,
        is_cancelled: Optional[Callable[[], bool]] = None,
    ) -> DriverResponse:
        return self._request(
            "chat_stream", messages, tools=tools, system=system, on_delta=on_delta,
            session_id=session_id, on_reasoning_delta=on_reasoning_delta,
            on_tool_hint=on_tool_hint, is_cancelled=is_cancelled,
        )
