from __future__ import annotations

"""Shared prompt-cache helpers for Anthropic-native and OpenAI-compat drivers.

OpenRouter requires EXPLICIT cache_control for Anthropic Claude and Alibaba
Qwen. OpenAI / Gemini / DeepSeek / Grok / Moonshot are automatic — do not
invent markers for those. Native AnthropicDriver uses the same AGNT-style
all-1h breakpoint policy via cache_control().

Hermes-inspired cache-carrier rules live here (sole stamper): empty /
whitespace system or tool envelopes never consume one of the ≤4 Anthropic
breakpoints; history markers walk back via ``history_cache_carriers`` to
messages that can actually carry a content-part marker (OpenAI-compat and
native AnthropicDriver both call it — no parallel marker logic).
"""

import hashlib
import os
import time
from typing import Any, Dict, Optional, Tuple


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


_VALID_CACHE_COMPACT_POLICIES = frozenset({"off", "defer", "refreeze"})


def prompt_cache_ttl_ms_for_driver(driver: str) -> Optional[int]:
    """Known prompt-cache TTL in ms, or None when we refuse to guess."""
    d = (driver or "").strip().lower()
    if not d:
        return None
    if "claude" in d or "anthropic" in d:
        ttl = (os.environ.get("HARNESS_ANTHROPIC_CACHE_TTL") or "1h").strip().lower()
        if ttl in ("5m", "5min", "off", "0", "false", "no"):
            return 5 * 60 * 1000
        return 60 * 60 * 1000
    if (
        d.startswith("openai/")
        or d.startswith("gpt-")
        or "/gpt-" in d
        or "openai" in d
        or "codex" in d
    ):
        return 5 * 60 * 1000
    return None


def cache_compact_policy() -> str:
    """Experiment-gated cache-hot compaction policy (default off).

    ``off`` — current compaction behavior unchanged.
    ``defer`` — skip automatic compaction while prompt cache is warm.
    ``refreeze`` — compact then reset append-only freeze for next prefix stamp.
    """
    raw = (os.environ.get("HARNESS_CACHE_COMPACT_POLICY") or "off").strip().lower()
    if raw in _VALID_CACHE_COMPACT_POLICIES:
        return raw
    return "off"


def prompt_cache_warm_for_session(session: Any) -> Tuple[bool, Dict[str, Any]]:
    """True when the session has a warm prompt cache eligible for deferral.

    Warm means: prompt cache enabled, driver TTL known, recent provider cache
    activity inside that TTL, and the last metered turn reported cache-read
    tokens > 0. Never raises; detail dict is diagnostic-only telemetry.
    """
    detail: Dict[str, Any] = {}
    try:
        if not prompt_cache_enabled():
            detail["warm_reason"] = "cache_disabled"
            return False, detail

        driver = ""
        try:
            driver = str(getattr(getattr(session, "config", None), "driver", "") or "")
        except Exception:
            driver = ""

        try:
            ttl_ms = prompt_cache_ttl_ms_for_driver(driver)
        except Exception:
            ttl_ms = None

        if ttl_ms is None:
            detail["warm_reason"] = "unknown_ttl"
            return False, detail

        activity_at = getattr(session, "_last_prompt_cache_activity_at", None)
        if activity_at is None:
            detail["warm_reason"] = "no_activity"
            return False, detail

        age_ms = max(0, int((time.time() - float(activity_at)) * 1000))
        detail["prompt_cache_age_ms"] = age_ms
        detail["prompt_cache_ttl_ms"] = int(ttl_ms)
        if age_ms >= ttl_ms:
            detail["warm_reason"] = "expired"
            return False, detail

        last_read = int(getattr(session, "_last_turn_cache_read_tokens", 0) or 0)
        detail["last_turn_cache_read_tokens"] = last_read
        if last_read <= 0:
            detail["warm_reason"] = "no_cache_read"
            return False, detail

        detail["warm_reason"] = "warm"
        return True, detail
    except Exception:
        detail["warm_reason"] = "error"
        return False, detail


def cache_control(*, stable: bool, family: str = "claude") -> dict:
    """Build a cache_control breakpoint.

    AGNT-style all-1h: every Claude breakpoint (system, last tool schema, and
    the two history markers) defaults to ttl:1h so long sessions keep paying
    cache-read rates. ``stable`` is retained for call-site clarity; both
    stable and history markers share the same TTL policy. Qwen only accepts
    ephemeral (no ttl). Override via HARNESS_ANTHROPIC_CACHE_TTL=1h|5m;
    5m/off/0/false/no drops ttl on ALL Claude markers so benches can run a
    5m arm.
    """
    marker: dict[str, str] = {"type": "ephemeral"}
    if family == "qwen":
        return marker
    _ = stable  # call-site intent only; Claude TTL is all-1h or all-ephemeral
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


def _can_carry_marker(msg: dict) -> bool:
    """True if a marker on this message is honored as a content-part carrier.

    Applies to OpenAI-compat (OpenRouter) envelopes and native Anthropic
    Messages content blocks. Empty / whitespace text envelopes would receive
    a marker the provider rejects or ignores — wasting one of the four
    breakpoints. Skip those so breakpoints land on messages that count.
    Predicate must agree with ``_mark_content_block`` (which only marks the
    last content part). Non-text dict parts (tool_use / tool_result / image)
    can carry the marker.
    """
    if not isinstance(msg, dict):
        return False
    content = msg.get("content")
    if content is None:
        return False
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        if not content or not isinstance(content[-1], dict):
            return False
        last = content[-1]
        # Text blocks must be non-empty/non-whitespace; non-text dict parts
        # (tool_use / tool_result / image) can carry the marker.
        if last.get("type") == "text" and not str(last.get("text") or "").strip():
            return False
        return True
    return False


def history_cache_carriers(messages: list, *, limit: int = 2) -> list[dict]:
    """Trailing messages eligible for the ≤2 Claude history breakpoints.

    Walks back past empty/whitespace (and other non-carrier) envelopes so
    history markers land on messages that can actually carry a content-part
    ``cache_control``. System messages are never carriers. Shared by native
    AnthropicDriver and ``apply_openai_compat_cache_control``.
    """
    if not isinstance(messages, list) or limit <= 0:
        return []
    carriers = [
        m for m in messages
        if isinstance(m, dict)
        and m.get("role") != "system"
        and _can_carry_marker(m)
    ]
    return carriers[-limit:]


def _tool_schema_can_carry(tool: Any) -> bool:
    """True if a tools[] entry is a non-empty schema envelope worth marking."""
    if not isinstance(tool, dict):
        return False
    func = tool.get("function")
    if isinstance(func, dict):
        return bool(str(func.get("name") or "").strip())
    return bool(str(tool.get("name") or "").strip())


def _mark_content_block(msg: dict, cc: dict) -> bool:
    """Attach cache_control to the last content block of a message. Returns True
    if a marker was placed. Never marks empty / whitespace-only text."""
    if not _can_carry_marker(msg):
        return False
    content = msg.get("content")
    if isinstance(content, str):
        msg["content"] = [
            {"type": "text", "text": content, "cache_control": dict(cc)}
        ]
        return True
    if isinstance(content, list) and content:
        last = content[-1]
        if not isinstance(last, dict):
            return False
        content[-1] = {**last, "cache_control": dict(cc)}
        return True
    return False


def _strip_cache_control(obj: Any) -> None:
    """Remove every cache_control marker from a message/tool tree in place.

    Anthropic allows at most 4 breakpoints per request. Multi-turn chats must
    clear prior markers before re-stamping the current stable/history set, or
    markers accumulate and the provider 400s ("Found 5/7/...").
    """
    if isinstance(obj, dict):
        obj.pop("cache_control", None)
        for v in obj.values():
            _strip_cache_control(v)
    elif isinstance(obj, list):
        for item in obj:
            _strip_cache_control(item)


def apply_openai_compat_cache_control(
    body: dict,
    *,
    model: str | None = None,
    family: str | None = None,
) -> str | None:
    """Stamp explicit cache_control on an OpenAI-compat chat body in place.

    Mirrors AnthropicDriver breakpoints for Claude (system + last tool + two
    history markers, ≤4). Qwen gets stable markers only (system + last tool),
    ephemeral without ttl. Empty/whitespace system or tool envelopes are
    skipped so they never consume a breakpoint; Claude history markers walk
    back to Hermes-style cache carriers. Returns the family used, or None
    when skipped. Best-effort: never raises.
    """
    try:
        if not prompt_cache_enabled():
            return None
        fam = family or explicit_cache_family(model or body.get("model"))
        if fam is None:
            return None

        messages = body.get("messages")
        if not isinstance(messages, list):
            messages = []

        # Drop leftover markers from earlier turns before placing a fresh ≤4 set.
        _strip_cache_control(messages)
        tools = body.get("tools")
        if isinstance(tools, list):
            _strip_cache_control(tools)

        # Stable: system text — skip empty/whitespace envelopes
        for msg in messages:
            if isinstance(msg, dict) and msg.get("role") == "system":
                if _can_carry_marker(msg):
                    _mark_content_block(msg, cache_control(stable=True, family=fam))
                break

        # Stable: last non-empty tool schema (identical every turn)
        if isinstance(tools, list) and tools:
            for i in range(len(tools) - 1, -1, -1):
                if _tool_schema_can_carry(tools[i]):
                    tools[i] = {
                        **tools[i],
                        "cache_control": cache_control(stable=True, family=fam),
                    }
                    break

        # Moving history: Claude only — walk back to messages that can carry
        if fam == "claude":
            history_cc = cache_control(stable=False, family=fam)
            for msg in history_cache_carriers(messages, limit=2):
                _mark_content_block(msg, history_cc)

        return fam
    except Exception:
        return None


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
