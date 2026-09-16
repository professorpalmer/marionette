"""Opaque provider output, replayable only by its originating transport/model."""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ReasoningEnvelope:
    provider: str
    endpoint: str
    model: str
    output_json: str

    def to_dict(self) -> dict:
        return dict(provider=self.provider, endpoint=self.endpoint,
                    model=self.model, output=json.loads(self.output_json), version=1)


def capture_reasoning(provider: str, endpoint: str, model: str, output: list) -> Optional[ReasoningEnvelope]:
    kinds = {"thinking", "redacted_thinking"} if provider == "anthropic" else {"reasoning"}
    if not any(isinstance(item, dict) and item.get("type") in kinds for item in output):
        return None
    return ReasoningEnvelope(provider, endpoint.rstrip("/"), model, json.dumps(output, ensure_ascii=False))


def retain_reasoning(message: dict, envelope: Optional[ReasoningEnvelope]) -> None:
    if envelope is not None:
        value = envelope.to_dict()
        value["canonical"] = copy.deepcopy({k: v for k, v in message.items() if k != "reasoning_envelope"})
        message["reasoning_envelope"] = value


def replay_reasoning(message: dict, provider: str, endpoint: str, model: str) -> Optional[list]:
    envelope = message.get("reasoning_envelope")
    if not isinstance(envelope, dict) or envelope.get("version") != 1:
        return None
    if (envelope.get("provider"), envelope.get("endpoint"), envelope.get("model")) != (provider, endpoint.rstrip("/"), model):
        return None
    # Compaction, edits, and tool-id repair invalidate the native output as a
    # whole. Never splice a signature onto reconstructed/modified content.
    canonical = {k: v for k, v in message.items() if k != "reasoning_envelope"}
    if envelope.get("canonical") != canonical or not isinstance(envelope.get("output"), list):
        return None
    return copy.deepcopy(envelope["output"])
