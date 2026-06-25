from __future__ import annotations

"""Data-driven driver registry built from pmharness/catalog.json -- the
research artifact listing every open-weights harness candidate with license and
native cost metadata.

Two reach modes:
  - "openrouter" (default): every model through one OpenAI-compatible endpoint
    with one key (OPENROUTER_API_KEY). Best for breadth; study the whole field
    fast. Driver-quality measurement is identical regardless of reach.
  - "native": provider's own endpoint + key. Use for finalists where the cost
    receipt must reflect true native pricing (not OpenRouter markup).

The stub oracle is always available offline with no key.
"""

import json
from pathlib import Path
from typing import Optional

from .drivers.base import Driver
from .drivers.stub import StubDriver
from .drivers.openai_compat import OpenAICompatDriver


_CATALOG_PATH = Path(__file__).resolve().parent / "catalog.json"

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
OPENROUTER_KEY_ENV = "OPENROUTER_API_KEY"


def load_catalog() -> dict:
    with open(_CATALOG_PATH) as f:
        return json.load(f)


def _entry(name: str) -> dict:
    cat = load_catalog()
    for m in cat["models"]:
        if m["name"] == name:
            return m
    raise KeyError(f"unknown model {name!r}; known={[m['name'] for m in cat['models']]}")


def model_names(tier: Optional[str] = None) -> list:
    cat = load_catalog()
    return [m["name"] for m in cat["models"] if tier is None or m["tier"] == tier]


def price(name: str) -> tuple:
    """Native (price_in, price_out) per Mtok for the cost column."""
    m = _entry(name)
    return (m.get("price_in"), m.get("price_out"))


def build(name: str, *, reach: str = "openrouter") -> Driver:
    import os as _os
    _mt = int(_os.environ.get("HARNESS_MAX_TOKENS", "1500"))
    """Construct a Driver for a catalog model.

    reach='openrouter' routes through OpenRouter (one key for the whole field).
    reach='native' uses the provider's own endpoint where defined.
    """
    if name == "stub-oracle":
        return StubDriver()
    if name == "stub-oracle-mt":
        from .drivers.stub_multiturn import StubMultiTurnDriver
        return StubMultiTurnDriver()
    if name == "stub-oracle-v2":
        from .drivers.stub_v2 import StubV2Driver
        return StubV2Driver()

    m = _entry(name)

    if reach == "native":
        nat = m.get("native")
        if not nat:
            raise ValueError(
                f"{name} has no native endpoint defined; use reach='openrouter'"
            )
        if nat.get("driver") == "anthropic":
            from .drivers.anthropic import AnthropicDriver
            return AnthropicDriver(
                name=name, model=nat["model"],
                base_url=nat["base_url"], api_key_env=nat["api_key_env"],
            )
        return OpenAICompatDriver(
            name=name, model=nat["model"], base_url=nat["base_url"],
            api_key_env=nat["api_key_env"], max_tokens=_mt,
        )

    if reach == "openrouter":
        slug = m.get("openrouter")
        if not slug:
            raise ValueError(f"{name} has no OpenRouter slug")
        return OpenAICompatDriver(
            name=name, model=slug, base_url=OPENROUTER_BASE,
            api_key_env=OPENROUTER_KEY_ENV, max_tokens=_mt,
            extra_headers={
                "HTTP-Referer": "https://github.com/professorpalmer/pm-harness",
                "X-Title": "pm-harness driver eval",
            },
        )

    raise ValueError(f"unknown reach {reach!r}; use 'openrouter' or 'native'")


# Convenience: all driver names (incl. the offline oracle).
def all_driver_names() -> list:
    return ["stub-oracle"] + model_names()


def has_vision(name: str) -> bool:
    """True if the model accepts native image input (HF task image-text-to-text)."""
    return bool(_entry(name).get("vision", False))


def vision_sidecars() -> list:
    """Cheap open VLMs the harness can use to transcribe image -> text artifact so
    a text-only DRIVER can consume it. Vision is a harness capability, not a
    driver requirement."""
    return [m["name"] for m in load_catalog()["models"]
            if m.get("tier") == "vision_sidecar"]
