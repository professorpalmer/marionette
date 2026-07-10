from __future__ import annotations

"""Shared prompt-cache helpers for Anthropic-native and OpenAI-compat drivers.

OpenRouter requires EXPLICIT cache_control for Anthropic Claude and Alibaba
Qwen. OpenAI / Gemini / DeepSeek / Grok / Moonshot are automatic — do not
invent markers for those. Native AnthropicDriver uses the same breakpoint
policy (stable 1h / history 5m) via cache_control().
"""

import hashlib
import os
from typing import Any


# Known OpenRouter / Alibaba slugs that need explicit ephemeral cache_control.
_QWEN_EXPLICIT_SLUGS = (
    "qwen3-max",
    "qwen-plus",
    "qwen3.6-plus",
    "qwen3-coder-plus",
    "qwen3-coder-flash",
)


def prompt_cache_enabled() -> bool:
    """Global kill switch: HARNESS_PROMPT_CACHE=0|false|off|no disables stamping."""
    raw = (os.environ.get("HARNESS_PROMPT_CACHE") or "1").strip().lower()
    return raw not in ("0", "false", "off", "no")


def cache_control(*, stable: bool, family: str = "claude") -> dict:
    """Build a cache_control breakpoint.

    Stable prefixes (system + last tool schema) default to a 1h TTL for Claude
    so repeats across long sessions keep paying the cheaper cache read. Moving
    history breakpoints stay on the default 5m window (no ttl key). Qwen only
    accepts ephemeral (no ttl). Override Claude stable TTL via
    HARNESS_ANTHROPIC_CACHE_TTL=1h|5m; 5m/off drops ttl on stable markers too.
    """
    marker: dict[str, str] = {"type": "ephemeral"}
    if family == "qwen" or not stable:
        return marker
    ttl = (os.environ.get("HARNESS_ANTHROPIC_CACHE_TTL") or "1h").strip().lower()
    if ttl in ("5m", "5min", "off", "0", "false", "no"):
        return marker
    marker["ttl"] = "1h"
    return marker


def explicit_cache_family(model: str | None) -> str | None:
    """Return 'claude' | 'qwen' when the model needs explicit cache_control.

    Automatic-cache providers (gpt, gemini, deepseek, grok, moonshot, …) return
    None so callers never invent fake markers.
    """
    m = (model or "").strip().lower()
    if not m:
        return None
    if "anthropic/" in m or "claude" in m:
        return "claude"
    if "qwen/" in m or m.startswith("qwen") or "/qwen" in m:
        return "qwen"
    for slug in _QWEN_EXPLICIT_SLUGS:
        if slug in m:
            return "qwen"
    return None


def _mark_content_block(msg: dict, cc: dict) -> bool:
    """Attach cache_control to the last content block of a message. Returns True
    if a marker was placed. Never marks empty / whitespace-only text."""
    content = msg.get("content")
    if content is None:
        return False
    if isinstance(content, str):
        if not content.strip():
            return False
        msg["content"] = [
            {"type": "text", "text": content, "cache_control": dict(cc)}
        ]
        return True
    if isinstance(content, list) and content:
        last = content[-1]
        if not isinstance(last, dict):
            return False
        if last.get("type") == "text" and not str(last.get("text") or "").strip():
            return False
        content[-1] = {**last, "cache_control": dict(cc)}
        return True
    return False


def apply_openai_compat_cache_control(
    body: dict,
    *,
    model: str | None = None,
    family: str | None = None,
) -> str | None:
    """Stamp explicit cache_control on an OpenAI-compat chat body in place.

    Mirrors AnthropicDriver breakpoints for Claude (system + last tool + two
    history markers, ≤4). Qwen gets stable markers only (system + last tool),
    ephemeral without ttl. Returns the family used, or None when skipped.
    """
    if not prompt_cache_enabled():
        return None
    fam = family or explicit_cache_family(model or body.get("model"))
    if fam is None:
        return None

    messages = body.get("messages")
    if not isinstance(messages, list):
        messages = []

    # Stable: system text
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "system":
            _mark_content_block(msg, cache_control(stable=True, family=fam))
            break

    # Stable: last tool schema (identical every turn)
    tools = body.get("tools")
    if isinstance(tools, list) and tools:
        last_tool = tools[-1]
        if isinstance(last_tool, dict):
            tools[-1] = {
                **last_tool,
                "cache_control": cache_control(stable=True, family=fam),
            }

    # Moving history: Claude only (Qwen docs cover stable breakpoints)
    if fam == "claude":
        history_cc = cache_control(stable=False, family=fam)
        non_system = [
            m for m in messages
            if isinstance(m, dict) and m.get("role") != "system"
        ]
        if len(non_system) >= 2:
            _mark_content_block(non_system[-2], history_cc)
        if non_system:
            _mark_content_block(non_system[-1], history_cc)

    return fam


def resolve_session_id(
    *,
    session_id: str | None = None,
    messages: list | None = None,
    system: str | None = None,
) -> str | None:
    """Best-effort sticky session id for OpenRouter routing. Never raises."""
    try:
        if session_id and str(session_id).strip():
            return str(session_id).strip()
        env = (os.environ.get("HARNESS_SESSION_ID") or "").strip()
        if env:
            return env
        parts: list[str] = []
        if system:
            parts.append(str(system))
        for m in messages or []:
            if not isinstance(m, dict):
                continue
            if m.get("role") == "system" and not system:
                c = m.get("content")
                parts.append(_content_as_text(c))
                continue
            if m.get("role") == "user":
                parts.append(_content_as_text(m.get("content")))
                break
        if not parts:
            return None
        digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
        return digest[:32]
    except Exception:
        return None


def _content_as_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        bits = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                bits.append(str(b.get("text") or ""))
            elif isinstance(b, str):
                bits.append(b)
        return "".join(bits)
    return str(content)


def maybe_attach_openrouter_session_id(
    body: dict,
    *,
    base_url: str | None,
    session_id: str | None = None,
    messages: list | None = None,
    system: str | None = None,
) -> None:
    """Set top-level session_id on OpenRouter requests. Best-effort, never fails."""
    try:
        if "openrouter.ai" not in (base_url or "").lower():
            return
        sid = resolve_session_id(
            session_id=session_id,
            messages=messages if messages is not None else body.get("messages"),
            system=system,
        )
        if sid:
            body["session_id"] = sid
    except Exception:
        return
