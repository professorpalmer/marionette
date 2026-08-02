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

GPT-5.6+ Codex Responses explicit caching also lives here: durable
``prompt_cache_key`` identity, capability-gated ``prompt_cache_options`` +
``prompt_cache_breakpoint`` at the end of the stable instructions/tools
prefix (never on changing user/tool tails). ChatGPT Codex may reject the
public OpenAI breakpoint fields — callers strip and retry on 400.
"""

import hashlib
import os
import re
import time
import uuid
from typing import Any, Dict, Optional, Tuple

# Stable sentinel developer message that carries the GPT-5.6 explicit
# breakpoint. Identical every turn so it does not bust the cached prefix.
# Placed ahead of conversation history; stripped on Codex 400 fallback.
CODEX_CACHE_BOUNDARY_TEXT = "marionette_stable_prefix_v1"

_GPT56_OR_LATER_RE = re.compile(
    r"(?:^|[./:])gpt-(?:5\.(?:[6-9]|\d{2,})|[6-9])\b",
    re.IGNORECASE,
)

# Process-local memory: ChatGPT Codex backends that rejected GPT-5.6
# prompt_cache_options / breakpoint. Keyed by base_url|model so a rejection
# on one host/model does not disable breakpoints elsewhere. Cleared only by
# process exit (or tests via clear_codex_prompt_cache_breakpoint_memory).
_CODEX_BREAKPOINT_UNSUPPORTED: Dict[str, bool] = {}


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
        # GPT-5.6+ prompt_cache_options.ttl floor is 30m; older OpenAI/Codex
        # families keep the historic ~5m warm window.
        if supports_gpt56_explicit_prompt_cache(d):
            return 30 * 60 * 1000
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


def supports_gpt56_explicit_prompt_cache(model: str | None) -> bool:
    """True for GPT-5.6+ families that accept prompt_cache_options/breakpoint."""
    m = (model or "").strip().lower().replace("_", "-")
    if not m:
        return False
    return bool(_GPT56_OR_LATER_RE.search(m))


def codex_prompt_cache_breakpoint_enabled(model: str | None) -> bool:
    """Opt-in/capability gate for Codex GPT-5.6 explicit breakpoints.

    ``HARNESS_CODEX_PROMPT_CACHE_BREAKPOINT``:
      auto (default) — on only for GPT-5.6+ when ``HARNESS_PROMPT_CACHE`` is on
      on|1|true|yes  — force on (still requires global prompt-cache enable)
      off|0|false|no — force off
    """
    if not prompt_cache_enabled():
        return False
    raw = (os.environ.get("HARNESS_CODEX_PROMPT_CACHE_BREAKPOINT") or "auto").strip().lower()
    if raw in ("0", "false", "off", "no"):
        return False
    if raw in ("1", "true", "on", "yes"):
        return True
    return supports_gpt56_explicit_prompt_cache(model)


def durable_codex_prompt_cache_key(session_id: str | None) -> str | None:
    """Provider-friendly durable UUID cache key for one Marionette chat session.

    Native Codex routes on a conversation UUID. Marionette session ids are
    short hex slices of uuid4; map them to a stable UUID5 so the wire key is
    UUID-shaped, identical across pilot steps/turns, and distinct per chat.
    Already-valid UUID session ids pass through unchanged. Never raises.
    """
    try:
        sid = (session_id or "").strip()
        if not sid:
            return None
        try:
            return str(uuid.UUID(sid))
        except ValueError:
            pass
        # 32-char hex without hyphens is a valid UUID wire form.
        if re.fullmatch(r"[0-9a-fA-F]{32}", sid):
            return str(uuid.UUID(hex=sid))
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"marionette:codex-cache:{sid}"))
    except Exception:
        return None


def _codex_breakpoint_memory_key(
    base_url: str | None, model: str | None,
) -> str:
    return f"{(base_url or '').rstrip('/').lower()}|{(model or '').strip().lower()}"


def mark_codex_prompt_cache_breakpoint_unsupported(
    base_url: str | None, model: str | None,
) -> None:
    """Remember that this Codex host/model rejected GPT-5.6 cache extensions."""
    try:
        _CODEX_BREAKPOINT_UNSUPPORTED[
            _codex_breakpoint_memory_key(base_url, model)
        ] = True
    except Exception:
        return


def codex_prompt_cache_breakpoint_known_unsupported(
    base_url: str | None, model: str | None,
) -> bool:
    """True when a prior 400 taught us this host/model rejects breakpoints."""
    try:
        return bool(
            _CODEX_BREAKPOINT_UNSUPPORTED.get(
                _codex_breakpoint_memory_key(base_url, model)
            )
        )
    except Exception:
        return False


def clear_codex_prompt_cache_breakpoint_memory() -> None:
    """Test helper: reset process-local unsupported-breakpoint memory."""
    _CODEX_BREAKPOINT_UNSUPPORTED.clear()


def _is_codex_cache_boundary_item(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    if item.get("type") not in (None, "message"):
        return False
    if str(item.get("role") or "") != "developer":
        return False
    content = item.get("content")
    if not isinstance(content, list) or not content:
        return False
    first = content[0]
    return (
        isinstance(first, dict)
        and first.get("type") == "input_text"
        and str(first.get("text") or "") == CODEX_CACHE_BOUNDARY_TEXT
    )


def strip_codex_prompt_cache_extensions(body: dict) -> None:
    """Remove GPT-5.6 breakpoint fields Codex backends may reject.

    Keeps ``prompt_cache_key`` (already accepted by ChatGPT Codex). Best-effort.
    """
    if not isinstance(body, dict):
        return
    body.pop("prompt_cache_options", None)
    inp = body.get("input")
    if not isinstance(inp, list):
        return
    kept: list = []
    for item in inp:
        if _is_codex_cache_boundary_item(item):
            continue
        if isinstance(item, dict):
            content = item.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        part.pop("prompt_cache_breakpoint", None)
            item.pop("prompt_cache_breakpoint", None)
        kept.append(item)
    body["input"] = kept


def codex_request_cache_snapshot(body: Any) -> Dict[str, Any]:
    """Sanitized Codex request cache fields for receipts (no prompt/auth text)."""
    snap: Dict[str, Any] = {
        "prompt_cache_key_present": False,
        "prompt_cache_key": None,
        "prompt_cache_options": None,
        "explicit_breakpoint": False,
    }
    if not isinstance(body, dict):
        return snap
    key = body.get("prompt_cache_key")
    if isinstance(key, str) and key.strip():
        snap["prompt_cache_key_present"] = True
        snap["prompt_cache_key"] = key.strip()
    options = body.get("prompt_cache_options")
    if isinstance(options, dict):
        # Keep only known wire fields — never echo arbitrary nested content.
        cleaned: Dict[str, Any] = {}
        mode = options.get("mode")
        ttl = options.get("ttl")
        if isinstance(mode, str):
            cleaned["mode"] = mode
        if isinstance(ttl, str):
            cleaned["ttl"] = ttl
        snap["prompt_cache_options"] = cleaned if cleaned else {}
    # Explicit breakpoint presence only — do not copy boundary / prompt text.
    inp = body.get("input")
    if not isinstance(inp, list):
        return snap
    for item in inp:
        if _is_codex_cache_boundary_item(item):
            snap["explicit_breakpoint"] = True
            break
        if not isinstance(item, dict):
            continue
        if item.get("prompt_cache_breakpoint") is not None:
            snap["explicit_breakpoint"] = True
            break
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("prompt_cache_breakpoint") is not None:
                snap["explicit_breakpoint"] = True
                break
        if snap["explicit_breakpoint"]:
            break
    return snap


def apply_codex_responses_prompt_cache(
    body: dict,
    *,
    model: str | None = None,
    session_id: str | None = None,
    base_url: str | None = None,
) -> Dict[str, Any]:
    """Stamp Codex Responses prompt-cache key + optional GPT-5.6 breakpoint.

    Stable boundary: developer sentinel at the head of ``input`` with
    ``prompt_cache_breakpoint``, after top-level ``instructions`` + ``tools``
    (the genuinely stable Marionette prefix). Conversation / tool-call tails
    stay after the marker so they can change without invalidating the prefix.

    ``prompt_cache_options.mode=explicit`` disables the GPT-5.6 implicit
    latest-message breakpoint that was causing 0 ``cached_tokens`` despite a
    locally stable append-only prefix.

    ``HARNESS_PROMPT_CACHE=0`` is a global kill switch: no ``prompt_cache_key``,
    options, or developer breakpoint are stamped, and any stale markers are
    removed (``reason=cache_disabled``).

    When a prior HTTP 400 taught us this ``base_url``/``model`` rejects the
    extensions, only ``prompt_cache_key`` is stamped (no repeat 400s).

    Returns a small diagnostic dict. Never raises.
    """
    detail: Dict[str, Any] = {
        "prompt_cache_key": None,
        "breakpoint": False,
        "reason": "ok",
    }
    try:
        if not isinstance(body, dict):
            detail["reason"] = "bad_body"
            return detail

        # Always clear prior breakpoint stamps before (re)applying.
        strip_codex_prompt_cache_extensions(body)

        if not prompt_cache_enabled():
            body.pop("prompt_cache_key", None)
            detail["reason"] = "cache_disabled"
            return detail

        key = durable_codex_prompt_cache_key(session_id)
        if key:
            body["prompt_cache_key"] = key
            detail["prompt_cache_key"] = key
        elif "prompt_cache_key" in body and not session_id:
            # Caller cleared session — do not leave a stale key.
            body.pop("prompt_cache_key", None)

        model_id = model if model is not None else body.get("model")
        model_str = model_id if isinstance(model_id, str) else None
        if not codex_prompt_cache_breakpoint_enabled(model_str):
            detail["reason"] = "breakpoint_disabled"
            return detail
        if codex_prompt_cache_breakpoint_known_unsupported(base_url, model_str):
            detail["reason"] = "breakpoint_unsupported_cached"
            return detail

        body["prompt_cache_options"] = {"mode": "explicit", "ttl": "30m"}
        boundary = {
            "type": "message",
            "role": "developer",
            "content": [
                {
                    "type": "input_text",
                    "text": CODEX_CACHE_BOUNDARY_TEXT,
                    "prompt_cache_breakpoint": {"mode": "explicit"},
                }
            ],
        }
        inp = list(body.get("input") or [])
        body["input"] = [boundary, *inp]
        detail["breakpoint"] = True
        detail["reason"] = "explicit_breakpoint"
        return detail
    except Exception:
        detail["reason"] = "error"
        return detail


def codex_prompt_cache_unsupported_error(detail: str) -> bool:
    """True when an HTTP 400 body indicates GPT-5.6 cache fields were rejected."""
    low = (detail or "").lower()
    if not low:
        return False
    needles = (
        "prompt_cache_options",
        "prompt_cache_breakpoint",
        "prompt cache options",
        "prompt cache breakpoint",
        "unknown parameter: 'prompt_cache_options'",
        "unknown parameter: 'prompt_cache_breakpoint'",
        "unsupported parameter: 'prompt_cache_options'",
        "unsupported parameter: 'prompt_cache_breakpoint'",
    )
    return any(n in low for n in needles)


def body_has_codex_prompt_cache_extensions(body: dict) -> bool:
    """True when the body still carries strip-able GPT-5.6 cache extensions."""
    if not isinstance(body, dict):
        return False
    if isinstance(body.get("prompt_cache_options"), dict):
        return True
    inp = body.get("input")
    if not isinstance(inp, list):
        return False
    for item in inp:
        if _is_codex_cache_boundary_item(item):
            return True
        if not isinstance(item, dict):
            continue
        if item.get("prompt_cache_breakpoint") is not None:
            return True
        content = item.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("prompt_cache_breakpoint") is not None:
                    return True
    return False
