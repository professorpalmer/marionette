from __future__ import annotations

"""Stand-in pilot for a saved model that can no longer be built.

A session must open even when its saved pilot is gone (deleted endpoint,
revoked key). Every call answers with the real reason so the user can pick
another model; the picker swap replaces this driver.
"""

from .base import SYSTEM_PROMPT, DriverResponse


class UnavailablePilotDriver:
    def __init__(self, spec: str, reason: str) -> None:
        self.name = spec
        self.model = spec
        self.reason = reason

    def _response(self) -> DriverResponse:
        return DriverResponse(
            text="",
            model=self.name,
            error=f"Pilot {self.name!r} is unavailable: {self.reason}. Pick another model.",
        )

    def complete(self, task_prompt: str, *, system: str = SYSTEM_PROMPT) -> DriverResponse:
        return self._response()

    def chat(self, messages: list, *, tools: list | None = None, system: str | None = None,
             **_kwargs) -> DriverResponse:
        return self._response()
