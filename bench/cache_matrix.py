#!/usr/bin/env python3
"""Fair GPT-5.6 Luna prompt-cache comparison across Marionette arms.

Compares cache evidence for the same deterministic protocol across:
  - Marionette Codex Responses (plan credits)
  - OpenRouter / OpenAI-compat (API dollars when provider-reported)
  - Cursor Agent CLI (plan credits, native resume)
  - Native Codex CLI control (``codex exec`` / ``resume``, plan)
  - Optional ``negative_control`` (HARNESS_PROMPT_CACHE=0; wire-marker absence)

Safe default is dry-run (no auth / network / provider subprocess). Pass
``--live`` only when you intend to spend plan credits or API dollars.

Plan arms report token/credit evidence (optionally plan_estimated dollars).
They never headline plan-vs-API USD savings. Missing cache fields stay null —
never coerced to zero.

Usage:
  python -m bench.cache_matrix --dry-run
  python -m bench.cache_matrix --live --arms codex,openrouter
  python -m bench.cache_matrix --live --skip-unavailable
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SCHEMA_VERSION = 1
PROTOCOL_ID = "gpt56-luna-cache-matrix-v1"

DEFAULT_ARMS = ("codex", "openrouter", "cursor", "native-codex")
ALL_ARMS = DEFAULT_ARMS + ("negative_control",)

DEFAULT_CODEX_MODEL = "gpt-5.6-luna"
DEFAULT_OPENROUTER_MODEL = "openai/gpt-5.6-luna"
DEFAULT_CURSOR_MODEL = "gpt-5.6-luna"
DEFAULT_NATIVE_CODEX_MODEL = "gpt-5.6-luna"

DEFAULT_TURNS = 4
DEFAULT_WARMUP_TURNS = 1
DEFAULT_CONTENT_TOKENS = 2048
DEFAULT_STABLE_PREFIX_TOKENS = 2048
DEFAULT_MAX_OUTPUT_TOKENS = 64
DEFAULT_OPENROUTER_BASE = "https://openrouter.ai/api/v1"
DEFAULT_NATIVE_CODEX_BIN = "codex"

TOOL_SURFACE_LIMITATION = (
    "Marionette HTTP arms receive identical messages/system/tools. "
    "Cursor Agent and native Codex CLI arms embed the same stable system "
    "prefix and tool-manifest text in the prompt, but execute their own "
    "native agent loops — tool execution is NOT identical across arms."
)

BILLING_BY_ARM = {
    "codex": "plan",
    "openrouter": "api",
    "cursor": "plan",
    "native-codex": "plan",
    "native_codex_cli": "plan",
    "negative_control": "plan",
}

DRIVER_BY_ARM = {
    "codex": "CodexResponsesDriver",
    "openrouter": "OpenAICompatDriver",
    "cursor": "CursorCliDriver",
    "native-codex": "native_codex_cli",
    "native_codex_cli": "native_codex_cli",
    "negative_control": "CodexResponsesDriver",
}

# Env keys mutated by arms; always restored via env_override.
_ARM_ENV_KEYS = (
    "HARNESS_PROMPT_CACHE",
    "HARNESS_CODEX_PROMPT_CACHE_BREAKPOINT",
)


class ModelMismatchError(RuntimeError):
    """Served model differs from the requested model (fail-closed)."""


class ArmUnavailableError(RuntimeError):
    """Arm prerequisite missing (binary / auth / key)."""


# ---------------------------------------------------------------------------
# Protocol (deterministic, shared)
# ---------------------------------------------------------------------------


def build_stable_system_prefix(
    stable_prefix_tokens: int = DEFAULT_STABLE_PREFIX_TOKENS,
) -> str:
    """Deterministic stable system prefix shared by every arm.

    Appends clearly labeled stable ballast so the prefix is approximately
    ``stable_prefix_tokens * 4`` characters — enough for provider cache
    eligibility — while preserving the rules and stable marker. No
    randomness or timestamps.
    """
    header = (
        "You are Marionette cache-matrix bench pilot.\n"
        "Protocol: gpt56-luna-cache-matrix-v1\n"
        "Rules:\n"
        "- Reply with exactly OK-<turn> and nothing else.\n"
        "- Do not call tools.\n"
        "- Keep answers under one short line.\n"
        "Stable marker: marionette_cache_matrix_stable_v1\n"
    )
    if stable_prefix_tokens <= 0:
        return header
    target_chars = max(0, int(stable_prefix_tokens) * 4)
    if len(header) >= target_chars:
        return header
    ballast_label = (
        "Stable ballast (deterministic cache-prefix padding):\n"
    )
    unit = (
        "[cache-matrix stable-prefix block={n}] "
        "Deterministic GPT-5.6 Luna stable system ballast. "
    )
    prefix = header + ballast_label
    fill_budget = max(0, target_chars - len(prefix))
    chunks: List[str] = []
    n = 0
    while sum(len(c) for c in chunks) < fill_budget:
        n += 1
        chunks.append(unit.replace("{n}", str(n)))
    return prefix + "".join(chunks)[:fill_budget]


def build_tool_manifest() -> List[Dict[str, Any]]:
    """Deterministic tool schema shared by Marionette HTTP arms."""
    return [
        {
            "type": "function",
            "function": {
                "name": "cache_matrix_noop",
                "description": (
                    "Bench-only no-op tool. Never call it during cache_matrix runs."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "note": {
                            "type": "string",
                            "description": "Ignored note field for schema ballast.",
                        }
                    },
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "cache_matrix_echo",
                "description": (
                    "Bench-only echo tool. Present for schema stability only."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                    },
                    "required": ["text"],
                    "additionalProperties": False,
                },
            },
        },
    ]


def tool_manifest_text(tools: Sequence[Dict[str, Any]]) -> str:
    """Stable textual tool manifest for Cursor / native Codex prompts."""
    return json.dumps(list(tools), sort_keys=True, separators=(",", ":"))


def pad_user_turn(index: int, n_turns: int, content_tokens: int) -> str:
    """Build a deterministic padded user turn (~content_tokens)."""
    header = (
        f"Bench turn {index + 1}/{n_turns}. "
        f"Reply with exactly: OK-{index + 1}. No tools.\n\n"
    )
    if content_tokens <= 0:
        return header.strip()
    target_chars = max(0, int(content_tokens) * 4 - len(header))
    unit = (
        f"[cache-matrix turn={index + 1} block={{n}}] "
        "Deterministic GPT-5.6 Luna cache ballast. "
    )
    chunks: List[str] = []
    n = 0
    while sum(len(c) for c in chunks) < target_chars:
        n += 1
        chunks.append(unit.replace("{n}", str(n)))
    return header + "".join(chunks)[:target_chars]


def build_protocol(
    *,
    turns: int = DEFAULT_TURNS,
    content_tokens: int = DEFAULT_CONTENT_TOKENS,
    stable_prefix_tokens: int = DEFAULT_STABLE_PREFIX_TOKENS,
) -> Dict[str, Any]:
    """Build the shared fair protocol (system, tools, padded user turns)."""
    system = build_stable_system_prefix(
        stable_prefix_tokens=stable_prefix_tokens,
    )
    tools = build_tool_manifest()
    manifest = tool_manifest_text(tools)
    user_turns = [pad_user_turn(i, turns, content_tokens) for i in range(turns)]
    # First native exec needs the stable system/tool prefix. Resume restores
    # that prefix from the thread — send only the current user turn after.
    native_prompts = [
        (
            f"{system}\n\n# Tool manifest (schema only; native loop differs)\n"
            f"{manifest}\n\n# User\n{user}"
        )
        for user in user_turns
    ]
    native_user_prompts = list(user_turns)
    return {
        "system": system,
        "tools": tools,
        "tool_manifest_text": manifest,
        "user_turns": user_turns,
        "native_prompts": native_prompts,
        "native_user_prompts": native_user_prompts,
        "turns": turns,
        "content_tokens": content_tokens,
        "stable_prefix_tokens": int(stable_prefix_tokens),
        "tool_surface_limitation": TOOL_SURFACE_LIMITATION,
    }


# ---------------------------------------------------------------------------
# Cache / cost hygiene
# ---------------------------------------------------------------------------


def _usage_has_cache_read_signal(usage: Any) -> bool:
    """True when a usage blob explicitly carries a cache-read alias."""
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
    for nest in (
        usage.get("prompt_tokens_details"),
        usage.get("input_tokens_details"),
        usage.get("promptTokensDetails"),
        usage.get("inputTokensDetails"),
        usage.get("input") if isinstance(usage.get("input"), dict) else None,
        usage.get("prompt") if isinstance(usage.get("prompt"), dict) else None,
        usage.get("usage") if isinstance(usage.get("usage"), dict) else None,
    ):
        if isinstance(nest, dict) and _usage_has_cache_read_signal(nest):
            return True
    return False


def _usage_has_cache_write_signal(usage: Any) -> bool:
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
    for nest in (
        usage.get("prompt_tokens_details"),
        usage.get("input_tokens_details"),
        usage.get("promptTokensDetails"),
        usage.get("inputTokensDetails"),
        usage.get("input") if isinstance(usage.get("input"), dict) else None,
        usage.get("prompt") if isinstance(usage.get("prompt"), dict) else None,
        usage.get("usage") if isinstance(usage.get("usage"), dict) else None,
    ):
        if isinstance(nest, dict) and _usage_has_cache_write_signal(nest):
            return True
    return False


def nullable_cache_from_usage(
    *blobs: Any,
) -> Tuple[Optional[int], Optional[int]]:
    """Return (cache_read, cache_write) with None when fields are absent.

    Uses ``coerce_token_usage_record`` for alias recognition, but never
    invents zero from missing evidence.
    """
    from pmharness.drivers.token_usage import coerce_token_usage_record

    saw_read = False
    saw_write = False
    for blob in blobs:
        if _usage_has_cache_read_signal(blob):
            saw_read = True
        if _usage_has_cache_write_signal(blob):
            saw_write = True
        if isinstance(blob, dict):
            raw = blob.get("raw_usage")
            if _usage_has_cache_read_signal(raw):
                saw_read = True
            if _usage_has_cache_write_signal(raw):
                saw_write = True

    detail = coerce_token_usage_record(*blobs)
    # Driver meta may already expose cache_*_tokens when provider reported >0.
    meta_read: Optional[int] = None
    meta_write: Optional[int] = None
    for blob in blobs:
        if not isinstance(blob, dict):
            continue
        if "cache_read_tokens" in blob:
            saw_read = True
            try:
                meta_read = int(blob["cache_read_tokens"])
            except (TypeError, ValueError):
                pass
        if "cache_write_tokens" in blob:
            saw_write = True
            try:
                meta_write = int(blob["cache_write_tokens"])
            except (TypeError, ValueError):
                pass

    cache_read: Optional[int]
    if saw_read:
        cache_read = int(meta_read if meta_read is not None else detail.cache_read)
    else:
        cache_read = None

    cache_write: Optional[int]
    if saw_write:
        cache_write = int(meta_write if meta_write is not None else detail.cache_write)
    else:
        cache_write = None
    return cache_read, cache_write


def normalize_model_id(model: str) -> str:
    """Canonicalize a model id for family matching.

    Lowercases and collapses runs of spaces, underscores, and hyphens so
    provider display labels (``GPT-5.6 Luna 272K Medium``) share a family
    with slug ids (``gpt-5.6-luna`` / ``gpt_5.6_luna``). Provider ``/``
    prefixes are left intact for the caller to strip.
    """
    text = (model or "").strip().lower()
    if not text:
        return ""
    return re.sub(r"[\s_-]+", "-", text).strip("-")


def models_compatible(requested: str, served: str) -> bool:
    """True when served is the requested model (or a dated/prefixed variant)."""
    req = normalize_model_id(requested)
    srv = normalize_model_id(served)
    if not req or not srv:
        return True
    if req == srv:
        return True
    req_tail = req.split("/")[-1]
    srv_tail = srv.split("/")[-1]
    if req_tail == srv_tail:
        return True
    # Dated / preview / display-label suffixes (gpt-5.6-luna-2026-…,
    # gpt-5.6-luna-272k-medium, …).
    if srv_tail.startswith(req_tail + "-") or srv_tail.startswith(req_tail + "."):
        return True
    return False


def assert_model_match(
    requested: str,
    served: Optional[str],
    *,
    allow_mismatch: bool = False,
) -> None:
    """Fail closed when a known served model differs from requested."""
    if served is None or not str(served).strip():
        return
    if models_compatible(requested, str(served)):
        return
    if allow_mismatch:
        return
    raise ModelMismatchError(
        f"served model {served!r} differs from requested {requested!r}; "
        f"pass --allow-model-mismatch to override (never silently substituted)"
    )


def cost_source_for_arm(
    *,
    billing: str,
    provider_cost_usd: Optional[float],
    plan_estimated_usd: Optional[float] = None,
) -> str:
    """Separate plan credit evidence from API provider dollars."""
    b = (billing or "unknown").strip().lower()
    if b == "api":
        return "provider" if provider_cost_usd is not None else "unknown"
    if b == "plan":
        # Plan arms never claim provider API dollars as the cost source.
        _ = plan_estimated_usd  # optional estimated dollars; source stays plan_* 
        return "plan_estimated"
    return "unknown"


def warmup_excluded_hit_rate(
    turns: Sequence[Dict[str, Any]],
    *,
    warmup_turns: int = DEFAULT_WARMUP_TURNS,
) -> Optional[float]:
    """Cache-hit ratio over post-warmup turns with known cache_read evidence.

    Warmup / turn-1 rows stay in totals but are excluded here. Missing
    cache_read evidence is omitted from the denominator (never treated as 0).
    Returns None when the post-warmup denominator is unknown or empty.
    """
    warm = max(0, int(warmup_turns))
    evidence = list(turns)[warm:]
    known = [
        t for t in evidence
        if isinstance(t, dict) and t.get("cache_read") is not None
    ]
    if not known:
        return None
    total_in = 0
    total_read = 0
    for t in known:
        try:
            tin = int(t.get("tokens_in") or 0)
        except (TypeError, ValueError):
            tin = 0
        try:
            cread = int(t.get("cache_read") or 0)
        except (TypeError, ValueError):
            cread = 0
        if tin <= 0:
            continue
        total_in += tin
        total_read += max(0, cread)
    if total_in <= 0:
        return None
    return round(total_read / float(total_in), 6)


# ---------------------------------------------------------------------------
# Env / preflight
# ---------------------------------------------------------------------------


@contextmanager
def env_override(updates: Dict[str, Optional[str]]) -> Iterator[None]:
    """Temporarily set/clear env keys; always restore prior values."""
    prior: Dict[str, Optional[str]] = {}
    for key, value in updates.items():
        prior[key] = os.environ.get(key)
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    try:
        yield
    finally:
        for key, old in prior.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


def _env_present(name: str) -> bool:
    return bool((os.environ.get(name) or "").strip())


def _driver_credential_pool_present(
    env_name: str,
    *,
    fallback_provider: Optional[str] = None,
) -> bool:
    """Presence-only check matching driver credential-pool resolution.

    CodexResponsesDriver._key uses
    ``provider_for_env_var(api_key_env) or "openai-codex"`` then
    ``resolve_entry``; OpenAICompatDriver._key uses
    ``resolve_entry_for_env(api_key_env)``. This helper follows the same
    provider seam but only returns a boolean — never token text.
    """
    try:
        from harness.credential_pool import (
            has_healthy_credential,
            provider_for_env_var,
            providers_for_env_var,
        )

        if fallback_provider is not None:
            prov = provider_for_env_var(env_name) or fallback_provider
            return bool(has_healthy_credential(prov))
        return any(has_healthy_credential(p) for p in providers_for_env_var(env_name))
    except Exception:
        return False


def _try_load_keys(*providers: str) -> None:
    """Best-effort key load without raising (live preflight only)."""
    try:
        from harness.keys import load_api_keys_on_startup

        for p in providers:
            try:
                load_api_keys_on_startup(p)
            except Exception:
                pass
    except Exception:
        pass


def resolve_native_codex_binary(explicit: Optional[str] = None) -> Optional[str]:
    raw = (explicit or DEFAULT_NATIVE_CODEX_BIN or "").strip()
    if not raw:
        return None
    if os.path.isabs(raw) and os.path.isfile(raw) and os.access(raw, os.X_OK):
        return raw
    found = shutil.which(raw)
    return found


def codex_oauth_store_path() -> Optional[Path]:
    """Return path to Codex ``auth.json`` if the file exists.

    Presence-only check — never reads or logs file contents.
    Honors ``CODEX_HOME``; otherwise ``~/.codex/auth.json``.
    """
    home = (os.environ.get("CODEX_HOME") or "").strip()
    if home:
        candidate = Path(home) / "auth.json"
    else:
        candidate = Path.home() / ".codex" / "auth.json"
    try:
        if candidate.is_file():
            return candidate
    except OSError:
        return None
    return None


def codex_oauth_store_present() -> bool:
    """True when a Codex OAuth store file exists (contents never inspected)."""
    return codex_oauth_store_path() is not None


# Known unauthenticated status categories from CLI status probes.
# Receipts store only these categories — never raw stdout or account emails.
_UNAUTHENTICATED_STATUS_CATEGORIES = frozenset(
    {
        "unauthenticated",
        "logged_out",
        "signed_out",
        "not_logged_in",
    }
)


def _parse_status_json_blob(text: str) -> Dict[str, Any]:
    raw = (text or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        # Some CLIs emit a JSON object among other lines.
        for line in raw.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                data = json.loads(line)
            except Exception:
                continue
            if isinstance(data, dict):
                return data
    return {}


def _classify_cli_auth(
    data: Dict[str, Any],
    stdout: str,
) -> Tuple[Optional[bool], str]:
    """Map CLI status output to (authenticated|None, status_category).

    Never returns emails, tokens, or raw command output — only a boolean
    (or None when indeterminate) and a coarse status category string.
    """
    if data:
        status_raw = str(data.get("status") or "").strip().lower()
        if status_raw in _UNAUTHENTICATED_STATUS_CATEGORIES:
            return False, status_raw
        truthy = (
            data.get("loggedIn") is True
            or data.get("authenticated") is True
            or data.get("isAuthenticated") is True
        )
        falsy = (
            data.get("loggedIn") is False
            or data.get("authenticated") is False
            or data.get("isAuthenticated") is False
        )
        if truthy:
            return True, "authenticated"
        if falsy:
            return False, status_raw or "unauthenticated"
        if status_raw in ("authenticated", "logged_in", "signed_in"):
            return True, "authenticated"

    lower = (stdout or "").lower()
    if (
        "not logged" in lower
        or "not authenticated" in lower
        or "unauthenticated" in lower
        or "logged out" in lower
    ):
        return False, "unauthenticated"
    if "logged in" in lower or "authenticated" in lower:
        return True, "authenticated"
    return None, "unknown"


def _probe_cli_auth(
    argv: Sequence[str],
    *,
    timeout: float = 8.0,
) -> Dict[str, Any]:
    """Run a status command; return only authenticated + status category."""
    try:
        proc = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return {
            "checked": True,
            "authenticated": False,
            "status": "missing_binary",
        }
    except subprocess.TimeoutExpired:
        return {
            "checked": True,
            "authenticated": False,
            "status": "timeout",
        }
    except Exception:
        return {
            "checked": True,
            "authenticated": False,
            "status": "error",
        }

    # Combine streams for classification only — never persist the text.
    combined = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    data = _parse_status_json_blob(proc.stdout or "")
    if not data:
        data = _parse_status_json_blob(combined)
    authenticated, category = _classify_cli_auth(data, combined)
    if authenticated is None:
        # Non-zero exit without a clear auth signal → treat as unauthenticated
        # for fail-closed live preflight.
        if proc.returncode not in (0, None):
            return {
                "checked": True,
                "authenticated": False,
                "status": "error",
            }
        return {
            "checked": True,
            "authenticated": None,
            "status": category or "unknown",
        }
    return {
        "checked": True,
        "authenticated": bool(authenticated),
        "status": category,
    }


def preflight_status(
    *,
    arms: Sequence[str],
    cursor_agent_bin: Optional[str] = None,
    native_codex_bin: Optional[str] = None,
    load_keys: bool = False,
) -> Dict[str, Any]:
    """Auth/binary presence for the receipt — never includes secret values."""
    if load_keys:
        _try_load_keys("openrouter", "openai-codex", "openai")

    cursor_path = None
    try:
        from pmharness.drivers.cursor_cli import resolve_agent_binary

        cursor_path = cursor_agent_bin or resolve_agent_binary()
    except Exception:
        cursor_path = cursor_agent_bin
    if cursor_agent_bin and not cursor_path:
        cursor_path = shutil.which(cursor_agent_bin) or (
            cursor_agent_bin if os.path.isfile(cursor_agent_bin) else None
        )

    native_path = resolve_native_codex_binary(native_codex_bin)
    # CodexResponsesDriver._key only reads OPENAI_CODEX_TOKEN (not OPENAI_API_KEY).
    codex_env_present = _env_present("OPENAI_CODEX_TOKEN")
    openrouter_env_present = _env_present("OPENROUTER_API_KEY")
    # auth.json is a native-CLI diagnostic only — never Marionette driver readiness.
    oauth_store = codex_oauth_store_present()

    # Pool resolution may load/mirror credentials; dry-run (load_keys=False)
    # stays resolver-free and subprocess-free.
    codex_pool_present = False
    openrouter_pool_present = False
    if load_keys:
        codex_pool_present = _driver_credential_pool_present(
            "OPENAI_CODEX_TOKEN",
            fallback_provider="openai-codex",
        )
        openrouter_pool_present = _driver_credential_pool_present(
            "OPENROUTER_API_KEY",
        )

    # CURSOR_API_KEY is an official non-browser Agent auth path. Presence-only
    # — never read or serialize the value. Browser-store probe stays separate.
    cursor_api_key_present = _env_present("CURSOR_API_KEY")
    cursor_agent: Dict[str, Any] = {
        "present": bool(cursor_path),
        "binary": cursor_path,
        "api_key_present": cursor_api_key_present,
        "api_key_env": "CURSOR_API_KEY",
    }
    native_codex_cli: Dict[str, Any] = {
        "present": bool(native_path),
        "binary": native_path,
    }

    # Live-only CLI auth probes. Dry-run stays subprocess-free.
    # Browser-store status is recorded honestly even when CURSOR_API_KEY is set
    # — api_key_present is the receipt for the non-browser path; do not claim
    # browser authentication succeeded solely because the env key is present.
    if load_keys:
        if cursor_path:
            cursor_agent["auth"] = _probe_cli_auth([str(cursor_path), "status"])
        else:
            cursor_agent["auth"] = {
                "checked": True,
                "authenticated": False,
                "status": "missing_binary",
            }
        if native_path:
            native_codex_cli["auth"] = _probe_cli_auth(
                [str(native_path), "login", "status"]
            )
        else:
            native_codex_cli["auth"] = {
                "checked": True,
                "authenticated": False,
                "status": "missing_binary",
            }

    status = {
        "openrouter_api_key": {
            "present": openrouter_env_present or openrouter_pool_present,
            "env": "OPENROUTER_API_KEY",
            "env_present": openrouter_env_present,
            "credential_pool_present": openrouter_pool_present,
        },
        "openai_codex_token": {
            # Driver token: env OR credential-pool runtime token (not auth.json).
            "present": codex_env_present or codex_pool_present,
            "env": "OPENAI_CODEX_TOKEN",
            "env_present": codex_env_present,
            "credential_pool_present": codex_pool_present,
            "oauth_store_present": oauth_store,
        },
        "codex_oauth_store": {
            "present": oauth_store,
        },
        "cursor_agent": cursor_agent,
        "native_codex_cli": native_codex_cli,
        "arms_requested": list(arms),
    }
    return status


def _auth_blocker_reason(
    auth: Any,
    *,
    missing_msg: str,
) -> Optional[str]:
    """Fail closed on known unauthenticated live CLI statuses."""
    if not isinstance(auth, dict) or not auth.get("checked"):
        return None
    status = str(auth.get("status") or "").strip().lower()
    if status in _UNAUTHENTICATED_STATUS_CATEGORIES or status in (
        "missing_binary",
        "timeout",
        "error",
    ):
        return missing_msg
    if auth.get("authenticated") is False:
        return missing_msg
    return None


def arm_blocker(arm: str, status: Dict[str, Any]) -> Optional[str]:
    """Return a short blocker reason, or None if the arm can run."""
    a = arm.strip().lower().replace("_", "-")
    if a in ("codex", "negative-control", "negative_control"):
        # Marionette Codex driver readiness — auth.json alone is insufficient.
        token = status.get("openai_codex_token") or {}
        if token.get("present"):
            return None
        return "missing OPENAI_CODEX_TOKEN (or Codex credential pool token)"
    if a == "openrouter":
        if not status.get("openrouter_api_key", {}).get("present"):
            return "missing OPENROUTER_API_KEY"
        return None
    if a == "cursor":
        cursor = status.get("cursor_agent") or {}
        if not cursor.get("present"):
            return "Cursor Agent CLI binary not found"
        # Official non-browser path: env key presence unblocks even when the
        # browser-store probe reports unauthenticated.
        if cursor.get("api_key_present"):
            return None
        return _auth_blocker_reason(
            cursor.get("auth"),
            missing_msg="Cursor Agent CLI not authenticated",
        )
    if a in ("native-codex", "native_codex_cli"):
        if not status.get("native_codex_cli", {}).get("present"):
            return "native codex binary not found"
        return _auth_blocker_reason(
            (status.get("native_codex_cli") or {}).get("auth"),
            missing_msg="native Codex CLI not authenticated",
        )
    return f"unknown arm {arm!r}"


# ---------------------------------------------------------------------------
# Native Codex CLI control arm
# ---------------------------------------------------------------------------


def build_native_codex_cmd(
    *,
    binary: str,
    model: str,
    prompt: str,
    thread_id: Optional[str] = None,
    workspace: Optional[str] = None,
    sandbox: str = "read-only",
) -> List[str]:
    """Build argv for first ``exec`` or later ``exec resume`` turn.

    First turn: ``exec --json --model MODEL --sandbox read-only [--cd DIR] PROMPT``.
    Resume: ``exec resume --json --model MODEL THREAD_ID PROMPT`` — ``resume``
    does not expose ``--sandbox`` / ``--cd``; workspace is the subprocess cwd.

    Never embeds credentials — Codex CLI uses its own auth store.
    """
    if thread_id:
        # resume help supports --json/--model only (not --sandbox/--cd).
        return [
            binary,
            "exec",
            "resume",
            "--json",
            "--model",
            model,
            thread_id,
            prompt,
        ]
    cmd = [
        binary,
        "exec",
        "--json",
        "--model",
        model,
        "--sandbox",
        sandbox,
    ]
    if workspace:
        cmd.extend(["--cd", workspace])
    cmd.append(prompt)
    return cmd


def parse_native_codex_jsonl(lines: Sequence[str]) -> Dict[str, Any]:
    """Extract thread/session, text, model, and usage aliases from Codex JSONL.

    Missing cache fields remain null (not zero).
    """
    from pmharness.drivers.token_usage import coerce_token_usage_record

    thread_id: Optional[str] = None
    session_id: Optional[str] = None
    served_model: Optional[str] = None
    text_parts: List[str] = []
    usage_blobs: List[Any] = []
    errors: List[str] = []
    raw_events: List[Any] = []

    for line in lines:
        raw = (line or "").strip()
        if not raw:
            continue
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        raw_events.append(ev)
        et = str(ev.get("type") or ev.get("event") or "")

        for key in ("thread_id", "threadId", "session_id", "sessionId", "id"):
            val = ev.get(key)
            if isinstance(val, str) and val.strip():
                if "thread" in key.lower() or et.endswith("thread.started"):
                    thread_id = thread_id or val.strip()
                if "session" in key.lower():
                    session_id = session_id or val.strip()

        if et in ("thread.started", "session.created", "session_meta"):
            for key in ("thread_id", "threadId", "session_id", "sessionId"):
                val = ev.get(key)
                if isinstance(val, str) and val.strip():
                    if "thread" in key.lower():
                        thread_id = thread_id or val.strip()
                    else:
                        session_id = session_id or val.strip()
            nested = ev.get("thread") or ev.get("session") or {}
            if isinstance(nested, dict):
                tid = nested.get("id") or nested.get("thread_id")
                if isinstance(tid, str) and tid.strip():
                    thread_id = thread_id or tid.strip()

        model = ev.get("model")
        if isinstance(model, str) and model.strip():
            served_model = model.strip()

        # Agent message text (single path — avoid double-append).
        item = ev.get("item") if isinstance(ev.get("item"), dict) else None
        if et == "item.completed" and isinstance(item, dict):
            if item.get("type") in (None, "agent_message", "message"):
                content = item.get("text") or item.get("content")
                if isinstance(content, str) and content.strip():
                    text_parts.append(content.strip())
                elif isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and isinstance(part.get("text"), str):
                            text_parts.append(part["text"])
        elif et in ("agent_message", "message"):
            content = ev.get("text") or ev.get("content")
            if isinstance(content, str) and content.strip():
                text_parts.append(content.strip())

        # Usage / token_count events
        if "usage" in ev or "tokenUsage" in ev or "token_usage" in ev:
            usage_blobs.append(ev)
        msg = ev.get("msg") if isinstance(ev.get("msg"), dict) else None
        if msg is not None:
            if msg.get("type") in ("token_count", "usage"):
                usage_blobs.append(msg)
            if "usage" in msg:
                usage_blobs.append(msg)
        info = ev.get("info") if isinstance(ev.get("info"), dict) else None
        if info is not None and (
            "usage" in info or "total_token_usage" in info or "token_usage" in info
        ):
            usage_blobs.append(info)
            nested_usage = info.get("total_token_usage") or info.get("last_token_usage")
            if isinstance(nested_usage, dict):
                usage_blobs.append(nested_usage)

        if et in ("error", "turn.failed") or ev.get("error"):
            err = ev.get("error") or ev.get("message") or et
            errors.append(str(err)[:500])

    detail = coerce_token_usage_record(*usage_blobs)
    cache_read, cache_write = nullable_cache_from_usage(*usage_blobs)

    # Prefer thread_id; fall back to session_id for resume.
    resume_id = thread_id or session_id
    tokens_in = int(detail.tokens_in) if detail.tokens_in else 0
    tokens_out = int(detail.tokens_out) if detail.tokens_out else 0
    tokens_in_basis = "provider" if tokens_in > 0 else "unknown"

    return {
        "thread_id": thread_id,
        "session_id": session_id,
        "resume_id": resume_id,
        "served_model": served_model,
        "text": "\n".join(text_parts).strip(),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "tokens_in_basis": tokens_in_basis,
        "cache_read": cache_read,
        "cache_write": cache_write,
        "provider_cost_usd": detail.cost,
        "errors": errors,
        "raw_event_count": len(raw_events),
    }


def run_native_codex_turn(
    *,
    binary: str,
    model: str,
    prompt: str,
    thread_id: Optional[str],
    workspace: Optional[str],
    timeout: int = 600,
    runner: Optional[Callable[..., subprocess.CompletedProcess]] = None,
) -> Dict[str, Any]:
    """Execute one native Codex CLI turn; return parsed receipt fields."""
    cmd = build_native_codex_cmd(
        binary=binary,
        model=model,
        prompt=prompt,
        thread_id=thread_id,
        workspace=workspace,
    )
    # Redacted argv for receipts: keep shape, drop prompt body.
    argv_for_log = []
    for i, part in enumerate(cmd):
        if i == len(cmd) - 1 and part == prompt:
            argv_for_log.append("<prompt>")
        else:
            argv_for_log.append(part)

    t0 = time.time()
    run = runner or subprocess.run
    try:
        proc = run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=workspace or None,
        )
    except subprocess.TimeoutExpired as e:
        return {
            "error": f"native codex timed out after {timeout}s",
            "wall_s": round(time.time() - t0, 3),
            "cmd": argv_for_log,
            "thread_id": thread_id,
            "cache_read": None,
            "cache_write": None,
            "stdout_tail": (e.stdout or "")[-500:] if isinstance(e.stdout, str) else "",
        }
    except FileNotFoundError:
        return {
            "error": f"native codex binary not found: {binary}",
            "wall_s": round(time.time() - t0, 3),
            "cmd": argv_for_log,
            "thread_id": thread_id,
            "cache_read": None,
            "cache_write": None,
        }

    wall = round(time.time() - t0, 3)
    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    parsed = parse_native_codex_jsonl(stdout.splitlines())
    if proc.returncode != 0 and not parsed.get("errors"):
        parsed["errors"] = [
            f"exit {proc.returncode}: {(stderr or stdout)[-300:]}"
        ]
    parsed["wall_s"] = wall
    parsed["cmd"] = argv_for_log
    parsed["exit_code"] = proc.returncode
    if proc.returncode != 0:
        parsed["error"] = parsed["errors"][0] if parsed.get("errors") else f"exit {proc.returncode}"
    return parsed


# ---------------------------------------------------------------------------
# Turn extraction from Marionette DriverResponse
# ---------------------------------------------------------------------------


def cursor_fair_system(protocol: Dict[str, Any]) -> str:
    """Stable prefix + exact tool-manifest text for the Cursor fair-protocol arm."""
    system = str(protocol.get("system") or "").rstrip()
    manifest = str(protocol.get("tool_manifest_text") or "")
    return (
        f"{system}\n\n"
        f"# Tool manifest (schema only; native loop differs)\n"
        f"{manifest}"
    )


def turn_from_driver_response(
    resp: Any,
    *,
    turn_index: int,
    requested_model: str,
    wall_s: float,
    session_id: Optional[str] = None,
    allow_mismatch: bool = False,
) -> Dict[str, Any]:
    """Normalize a DriverResponse into a receipt turn row."""
    meta = getattr(resp, "meta", None) or {}
    if not isinstance(meta, dict):
        meta = {}
    error = getattr(resp, "error", None)
    served = meta.get("served_model") or getattr(resp, "model", None)
    if isinstance(served, str) and served in (
        "cache-matrix-codex",
        "cache-matrix-openrouter",
        "cache-matrix-cursor",
        "cache-matrix-negative",
    ):
        # Driver.name, not provider served model.
        served = meta.get("served_model")

    raw_usage = meta.get("raw_usage")
    cache_read, cache_write = nullable_cache_from_usage(meta, raw_usage)
    tin = int(getattr(resp, "tokens_in", 0) or 0)
    tout = int(getattr(resp, "tokens_out", 0) or 0)
    if tin <= 0 and isinstance(raw_usage, dict):
        from pmharness.drivers.token_usage import coerce_token_usage_record

        d = coerce_token_usage_record(raw_usage)
        tin = int(d.tokens_in or 0)
        tout = int(d.tokens_out or tout)

    provider_cost = meta.get("provider_cost_usd")
    if provider_cost is not None:
        try:
            provider_cost = float(provider_cost)
        except (TypeError, ValueError):
            provider_cost = None

    served_model = meta.get("served_model")
    if not isinstance(served_model, str) or not served_model.strip():
        served_model = served if isinstance(served, str) else None
    if isinstance(served_model, str) and served_model.strip():
        assert_model_match(
            requested_model, served_model, allow_mismatch=allow_mismatch,
        )
    else:
        served_model = None

    tokens_in_basis = "provider" if tin > 0 else "unknown"
    if meta.get("tokens_in_basis"):
        tokens_in_basis = str(meta["tokens_in_basis"])

    turn: Dict[str, Any] = {
        "turn": turn_index,
        "wall_s": wall_s,
        "tokens_in": tin,
        "tokens_out": tout,
        "cache_read": cache_read,
        "cache_write": cache_write,
        "tokens_in_basis": tokens_in_basis,
        "provider_cost_usd": provider_cost,
        "requested_model": requested_model,
        "served_model": served_model,
        "session_id": session_id,
        "text_preview": (getattr(resp, "text", None) or "")[:80],
    }
    if error:
        turn["error"] = str(error)
    for key in (
        "prompt_cache_key",
        "prompt_cache_breakpoint",
        "prompt_cache_options",
        "cache_breakpoint",
        "native_chat_id",
        "resume_chat_id",
    ):
        if key in meta:
            turn[key] = meta[key]
    # Retain nested prompt-cache diagnostics (initial/final wire snapshots,
    # apply reason, etc.) — do not filter down to a key whitelist.
    pc = meta.get("prompt_cache")
    if isinstance(pc, dict):
        turn["prompt_cache_diagnostics"] = dict(pc)
    if meta.get("prompt_cache_key"):
        turn["prompt_cache_key"] = meta["prompt_cache_key"]
    return turn


def summarize_arm_turns(
    turns: Sequence[Dict[str, Any]],
    *,
    warmup_turns: int,
) -> Dict[str, Any]:
    totals = {
        "tokens_in": 0,
        "tokens_out": 0,
        "cache_read": 0,
        "cache_write": 0,
        "cache_read_known_turns": 0,
        "cache_write_known_turns": 0,
        "provider_cost_usd": None,
        "errors": 0,
        "wall_s": 0.0,
    }
    cost_sum = 0.0
    cost_any = False
    for t in turns:
        if t.get("error"):
            totals["errors"] += 1
        totals["tokens_in"] += int(t.get("tokens_in") or 0)
        totals["tokens_out"] += int(t.get("tokens_out") or 0)
        totals["wall_s"] += float(t.get("wall_s") or 0.0)
        if t.get("cache_read") is not None:
            totals["cache_read"] += int(t["cache_read"])
            totals["cache_read_known_turns"] += 1
        if t.get("cache_write") is not None:
            totals["cache_write"] += int(t["cache_write"])
            totals["cache_write_known_turns"] += 1
        if t.get("provider_cost_usd") is not None:
            try:
                cost_sum += float(t["provider_cost_usd"])
                cost_any = True
            except (TypeError, ValueError):
                pass
    totals["wall_s"] = round(totals["wall_s"], 3)
    totals["provider_cost_usd"] = round(cost_sum, 6) if cost_any else None
    return {
        "totals": totals,
        "warmup_excluded_hit_rate": warmup_excluded_hit_rate(
            turns, warmup_turns=warmup_turns,
        ),
    }


# ---------------------------------------------------------------------------
# Arm runners
# ---------------------------------------------------------------------------


def _fresh_session_id(arm: str) -> str:
    return f"cache-matrix-{arm}-{uuid.uuid4().hex[:12]}"


def run_marionette_driver_arm(
    *,
    arm: str,
    driver: Any,
    protocol: Dict[str, Any],
    requested_model: str,
    session_id: Optional[str] = None,
    warmup_turns: int = DEFAULT_WARMUP_TURNS,
    idle_seconds: float = 0.0,
    allow_mismatch: bool = False,
    billing: Optional[str] = None,
) -> Dict[str, Any]:
    """Run identical padded turns against a Marionette chat driver."""
    sid = session_id or _fresh_session_id(arm)
    billing = billing or BILLING_BY_ARM.get(arm, "unknown")
    history: List[Dict[str, Any]] = []
    turns: List[Dict[str, Any]] = []
    arm_key = str(arm).strip().lower().replace("_", "-")
    # Cursor fair protocol: embed identical stable prefix + tool-manifest text
    # in the system string (native loop differs; tools schemas are ignored).
    if arm_key == "cursor":
        system = cursor_fair_system(protocol)
    else:
        system = protocol["system"]
    tools = protocol["tools"]
    prompt_cache_diags: List[Any] = []

    for i, user in enumerate(protocol["user_turns"]):
        if idle_seconds > 0 and i > 0:
            time.sleep(idle_seconds)
        history.append({"role": "user", "content": user})
        t0 = time.time()
        resp = driver.chat(
            history,
            tools=tools,
            system=system,
            session_id=sid,
        )
        wall = round(time.time() - t0, 3)
        meta = getattr(resp, "meta", None) or {}
        if isinstance(meta, dict):
            if meta.get("prompt_cache_key") or meta.get("prompt_cache"):
                prompt_cache_diags.append(
                    {
                        "turn": i + 1,
                        "prompt_cache_key": meta.get("prompt_cache_key"),
                        "prompt_cache": meta.get("prompt_cache"),
                    }
                )
        turn = turn_from_driver_response(
            resp,
            turn_index=i + 1,
            requested_model=requested_model,
            wall_s=wall,
            session_id=sid,
            allow_mismatch=allow_mismatch,
        )
        # Prefer native resume id from Cursor meta when present.
        if isinstance(meta, dict):
            native = (
                meta.get("session_id")
                or meta.get("native_chat_id")
                or meta.get("resume_chat_id")
            )
            if native:
                turn["native_resume_id"] = native
        turns.append(turn)
        assistant = (getattr(resp, "text", None) or f"OK-{i + 1}").strip() or f"OK-{i + 1}"
        history.append({"role": "assistant", "content": assistant})

    summary = summarize_arm_turns(turns, warmup_turns=warmup_turns)
    served_models = sorted(
        {
            t["served_model"]
            for t in turns
            if isinstance(t.get("served_model"), str) and t["served_model"]
        }
    )
    provider_cost = summary["totals"]["provider_cost_usd"]
    return {
        "arm": arm,
        "driver": DRIVER_BY_ARM.get(arm, type(driver).__name__),
        "billing": billing,
        "cost_source": cost_source_for_arm(
            billing=billing, provider_cost_usd=provider_cost,
        ),
        "requested_model": requested_model,
        "served_model": served_models[0] if len(served_models) == 1 else (
            served_models or None
        ),
        "session_id": sid,
        "turns": turns,
        "prompt_cache_diagnostics": prompt_cache_diags,
        **summary,
    }


def run_native_codex_arm(
    *,
    protocol: Dict[str, Any],
    requested_model: str,
    binary: str,
    workspace: Optional[str],
    warmup_turns: int = DEFAULT_WARMUP_TURNS,
    idle_seconds: float = 0.0,
    allow_mismatch: bool = False,
    timeout: int = 600,
    runner: Optional[Callable[..., subprocess.CompletedProcess]] = None,
) -> Dict[str, Any]:
    """Native Codex CLI control: exec then resume with shared prompts.

    First turn sends ``native_prompts`` (stable system/tool prefix + user).
    Resume turns send ``native_user_prompts`` only — the thread already
    restored the first-turn stable prefix.
    """
    turns: List[Dict[str, Any]] = []
    thread_id: Optional[str] = None
    cmds: List[List[str]] = []
    native_full = list(protocol.get("native_prompts") or [])
    native_user = list(
        protocol.get("native_user_prompts") or protocol.get("user_turns") or []
    )
    n_turns = max(len(native_full), len(native_user))

    for i in range(n_turns):
        if idle_seconds > 0 and i > 0:
            time.sleep(idle_seconds)
        if thread_id:
            prompt = native_user[i] if i < len(native_user) else native_full[i]
        else:
            prompt = native_full[i] if i < len(native_full) else native_user[i]
        parsed = run_native_codex_turn(
            binary=binary,
            model=requested_model,
            prompt=prompt,
            thread_id=thread_id,
            workspace=workspace,
            timeout=timeout,
            runner=runner,
        )
        cmds.append(list(parsed.get("cmd") or []))
        if parsed.get("resume_id") or parsed.get("thread_id"):
            thread_id = parsed.get("resume_id") or parsed.get("thread_id")
        served = parsed.get("served_model")
        if served:
            assert_model_match(
                requested_model, str(served), allow_mismatch=allow_mismatch,
            )
        turn = {
            "turn": i + 1,
            "wall_s": parsed.get("wall_s"),
            "tokens_in": int(parsed.get("tokens_in") or 0),
            "tokens_out": int(parsed.get("tokens_out") or 0),
            "cache_read": parsed.get("cache_read"),
            "cache_write": parsed.get("cache_write"),
            "tokens_in_basis": parsed.get("tokens_in_basis") or "unknown",
            "provider_cost_usd": parsed.get("provider_cost_usd"),
            "requested_model": requested_model,
            "served_model": served,
            "native_resume_id": thread_id,
            "cmd": parsed.get("cmd"),
            "text_preview": (parsed.get("text") or "")[:80],
        }
        if parsed.get("error"):
            turn["error"] = parsed["error"]
        turns.append(turn)

    summary = summarize_arm_turns(turns, warmup_turns=warmup_turns)
    return {
        "arm": "native_codex_cli",
        "driver": "native_codex_cli",
        "billing": "plan",
        "cost_source": cost_source_for_arm(
            billing="plan",
            provider_cost_usd=summary["totals"]["provider_cost_usd"],
        ),
        "requested_model": requested_model,
        "served_model": next(
            (t.get("served_model") for t in turns if t.get("served_model")),
            None,
        ),
        "native_resume_id": thread_id,
        "turns": turns,
        "command_shapes": cmds,
        **summary,
    }


def negative_control_wire_check(
    *,
    model: str,
    session_id: str,
) -> Dict[str, Any]:
    """Assert Codex cache markers are absent under cache kill-switch.

    Requires ``prompt_cache_key``, ``prompt_cache_options``, and the developer
    breakpoint to be absent when ``HARNESS_PROMPT_CACHE=0``. Does not claim
    provider cache_read == 0 — only wire-marker absence.
    """
    from pmharness.drivers.prompt_cache import (
        apply_codex_responses_prompt_cache,
        body_has_codex_prompt_cache_extensions,
    )

    body: Dict[str, Any] = {
        "model": model,
        "instructions": build_stable_system_prefix(),
        # Pre-seed a stale key so the kill switch must actively clear it.
        "prompt_cache_key": "stale-should-be-removed",
        "prompt_cache_options": {"mode": "explicit", "ttl": "30m"},
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "OK-1 probe"}],
            }
        ],
        "tools": build_tool_manifest(),
    }
    with env_override(
        {
            "HARNESS_PROMPT_CACHE": "0",
            "HARNESS_CODEX_PROMPT_CACHE_BREAKPOINT": "off",
        }
    ):
        detail = apply_codex_responses_prompt_cache(
            body, model=model, session_id=session_id,
        )
        has_ext = body_has_codex_prompt_cache_extensions(body)
        has_key = (
            isinstance(body.get("prompt_cache_key"), str)
            and bool(str(body.get("prompt_cache_key") or "").strip())
        )
    return {
        "arm": "negative_control",
        "ok": (not has_ext) and (not has_key),
        "has_prompt_cache_extensions": has_ext,
        "has_prompt_cache_options": isinstance(body.get("prompt_cache_options"), dict),
        "has_prompt_cache_key": has_key,
        "apply_detail": detail,
        "assertion": "wire_marker_absence_only",
        "note": (
            "Negative control asserts Codex prompt_cache_key / breakpoint / "
            "options fields are absent when HARNESS_PROMPT_CACHE=0; it does "
            "NOT claim zero provider cache reads."
        ),
    }


def make_codex_driver(model: str, max_tokens: int) -> Any:
    from pmharness.drivers.codex_responses import CodexResponsesDriver

    return CodexResponsesDriver(
        name="cache-matrix-codex",
        model=model,
        max_tokens=max_tokens,
        timeout=300,
    )


def make_openrouter_driver(
    model: str,
    max_tokens: int,
    *,
    base_url: str,
) -> Any:
    from pmharness.drivers.openai_compat import OpenAICompatDriver

    return OpenAICompatDriver(
        name="cache-matrix-openrouter",
        model=model,
        base_url=base_url,
        api_key_env="OPENROUTER_API_KEY",
        temperature=0.0,
        max_tokens=max_tokens,
        timeout=300,
        extra_headers={
            "HTTP-Referer": "https://github.com/professorpalmer/marionette",
            "X-Title": "Marionette Cache Matrix Bench",
        },
        enable_reasoning=False,
    )


def make_cursor_driver(
    model: str,
    max_tokens: int,
    *,
    agent_binary: Optional[str],
    workspace: Optional[str],
) -> Any:
    from pmharness.drivers.cursor_cli import CursorCliDriver

    # Benchmark-only: installed Cursor Agent CLI accepts plan|ask and rejects
    # mode=agent. Host Marionette Autopilot still defaults to agent elsewhere.
    return CursorCliDriver(
        name="cache-matrix-cursor",
        model=model,
        max_tokens=max_tokens,
        timeout=600,
        mode="ask",
        agent_binary=agent_binary,
        cwd=workspace,
    )


def run_live_arm(
    arm: str,
    *,
    protocol: Dict[str, Any],
    models: Dict[str, str],
    workspace: Optional[str],
    openrouter_base: str,
    cursor_agent_bin: Optional[str],
    native_codex_bin: Optional[str],
    max_output_tokens: int,
    warmup_turns: int,
    idle_seconds: float,
    allow_mismatch: bool,
    skip_unavailable: bool,
    preflight: Dict[str, Any],
    driver_factory: Optional[Callable[[str], Any]] = None,
    native_runner: Optional[Callable[..., subprocess.CompletedProcess]] = None,
) -> Dict[str, Any]:
    """Run one live arm sequentially; restore env afterward."""
    a = arm.strip().lower().replace("_", "-")
    blocker = arm_blocker(a if a != "negative-control" else "codex", preflight)
    # negative_control needs Codex token only when executing live driver turns;
    # wire check itself is hermetic. For live negative_control we still need token.
    if a == "negative-control":
        blocker = arm_blocker("codex", preflight)

    if blocker:
        if skip_unavailable:
            return {
                "arm": a.replace("-", "_") if a == "native-codex" else a,
                "status": "skipped",
                "blocker": blocker,
                "billing": BILLING_BY_ARM.get(a, "unknown"),
                "cost_source": "unknown",
            }
        raise ArmUnavailableError(f"arm {arm}: {blocker}")

    # Force benchmark wire markers so ambient user kill-switches cannot
    # silently invalidate the positive Codex/OpenRouter arms. Negative
    # control overrides to off. Always restored by env_override.
    if a == "negative-control":
        env_updates: Dict[str, Optional[str]] = {
            "HARNESS_PROMPT_CACHE": "0",
            "HARNESS_CODEX_PROMPT_CACHE_BREAKPOINT": "off",
        }
    else:
        env_updates = {
            "HARNESS_PROMPT_CACHE": "1",
            "HARNESS_CODEX_PROMPT_CACHE_BREAKPOINT": "auto",
        }

    with env_override(env_updates):
        if a == "native-codex":
            binary = preflight["native_codex_cli"]["binary"] or (
                resolve_native_codex_binary(native_codex_bin)
            )
            if not binary:
                if skip_unavailable:
                    return {
                        "arm": "native_codex_cli",
                        "status": "skipped",
                        "blocker": "native codex binary not found",
                        "billing": "plan",
                        "cost_source": "unknown",
                    }
                raise ArmUnavailableError("native codex binary not found")
            result = run_native_codex_arm(
                protocol=protocol,
                requested_model=models["native_codex"],
                binary=binary,
                workspace=workspace,
                warmup_turns=warmup_turns,
                idle_seconds=idle_seconds,
                allow_mismatch=allow_mismatch,
                runner=native_runner,
            )
            result["status"] = "ok"
            return result

        if a == "negative-control":
            wire = negative_control_wire_check(
                model=models["codex"],
                session_id=_fresh_session_id("negative"),
            )
            if driver_factory is not None:
                driver = driver_factory("negative_control")
            else:
                driver = make_codex_driver(models["codex"], max_output_tokens)
            result = run_marionette_driver_arm(
                arm="negative_control",
                driver=driver,
                protocol=protocol,
                requested_model=models["codex"],
                warmup_turns=warmup_turns,
                idle_seconds=idle_seconds,
                allow_mismatch=allow_mismatch,
                billing="plan",
            )
            result["wire_check"] = wire
            result["status"] = "ok" if wire.get("ok") else "wire_check_failed"
            # Claim hygiene: do not assert zero cache reads, and never
            # headline provider cache_read as a negative-control hit rate.
            result["cache_claim"] = "wire_marker_absence_only"
            result["warmup_excluded_hit_rate"] = None
            return result

        if driver_factory is not None:
            driver = driver_factory(a)
            model = {
                "codex": models["codex"],
                "openrouter": models["openrouter"],
                "cursor": models["cursor"],
            }[a]
        elif a == "codex":
            driver = make_codex_driver(models["codex"], max_output_tokens)
            model = models["codex"]
        elif a == "openrouter":
            driver = make_openrouter_driver(
                models["openrouter"],
                max_output_tokens,
                base_url=openrouter_base,
            )
            model = models["openrouter"]
        elif a == "cursor":
            driver = make_cursor_driver(
                models["cursor"],
                max_output_tokens,
                agent_binary=cursor_agent_bin
                or preflight.get("cursor_agent", {}).get("binary"),
                workspace=workspace,
            )
            model = models["cursor"]
        else:
            raise ArmUnavailableError(f"unknown arm {arm!r}")

        result = run_marionette_driver_arm(
            arm=a,
            driver=driver,
            protocol=protocol,
            requested_model=model,
            warmup_turns=warmup_turns,
            idle_seconds=idle_seconds,
            allow_mismatch=allow_mismatch,
        )
        result["status"] = "ok"
        return result


# ---------------------------------------------------------------------------
# Receipts
# ---------------------------------------------------------------------------


def default_output_path() -> Path:
    """Temp / non-repo path for receipts (avoid committing spend artifacts)."""
    base = Path(tempfile.gettempdir()) / "marionette-cache-matrix"
    base.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return base / f"cache_matrix_{stamp}.json"


def render_markdown_summary(payload: Dict[str, Any]) -> str:
    """Compact Markdown summary — never headlines plan-vs-API USD savings."""
    lines = [
        f"# Marionette cache matrix — {payload.get('created_at', '')}",
        "",
        f"- Protocol: `{payload.get('protocol')}`",
        f"- Mode: `{payload.get('mode')}`",
        f"- Schema: `{payload.get('schema_version')}`",
        f"- Warmup turns: `{payload.get('warmup_turns')}`",
        "",
        "## Tool-surface limitation",
        "",
        str(payload.get("tool_surface_limitation") or TOOL_SURFACE_LIMITATION),
        "",
        "## Requested models",
        "",
    ]
    models = payload.get("requested_models") or {}
    for k, v in models.items():
        lines.append(f"- {k}: `{v}`")
    lines += ["", "## Billing policy", ""]
    for k, v in (payload.get("billing_policy") or {}).items():
        lines.append(f"- {k}: `{v}`")
    lines += ["", "## Arms", "", "| Arm | Status | Billing | Cost source | Hit rate (post-warmup) | Errors |", "|---|---|---|---|---:|---:|"]
    for name, arm in (payload.get("arms") or {}).items():
        if not isinstance(arm, dict):
            continue
        # Negative control never headlines a cache hit rate — wire markers only.
        if (
            name in ("negative_control", "negative-control")
            or arm.get("cache_claim") == "wire_marker_absence_only"
        ):
            hit_s = "unknown"
        else:
            hit = arm.get("warmup_excluded_hit_rate")
            hit_s = "unknown" if hit is None else f"{hit:.4f}"
        errs = (arm.get("totals") or {}).get("errors", arm.get("blocker") and "—")
        lines.append(
            f"| {name} | {arm.get('status', 'ok')} | {arm.get('billing', '?')} | "
            f"{arm.get('cost_source', '?')} | {hit_s} | {errs} |"
        )
    lines += [
        "",
        "## Claim hygiene",
        "",
        "- Plan arms report token/credit evidence; optional plan_estimated dollars "
        "only — never a plan-vs-API USD savings headline.",
        "- API arms prefer provider-reported cost; missing cost stays unknown.",
        "- Missing cache_read/cache_write stay null; never coerced to zero.",
        "- Negative control asserts wire-marker absence only.",
        "",
    ]
    return "\n".join(lines)


def write_receipt(
    payload: Dict[str, Any],
    output: Path,
) -> Tuple[Path, Path]:
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    json_path = output if output.suffix == ".json" else output.with_suffix(".json")
    md_path = json_path.with_suffix(".md")
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=False), encoding="utf-8")
    md_path.write_text(render_markdown_summary(payload), encoding="utf-8")
    return json_path, md_path


def build_receipt_skeleton(
    *,
    mode: str,
    protocol: Dict[str, Any],
    models: Dict[str, str],
    warmup_turns: int,
    arms: Sequence[str],
    preflight: Dict[str, Any],
    allow_mismatch: bool,
) -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "protocol": PROTOCOL_ID,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "requested_models": dict(models),
        "billing_policy": {
            "codex": "plan",
            "openrouter": "api",
            "cursor": "plan",
            "native-codex": "plan",
            "negative_control": "plan",
        },
        "warmup_turns": warmup_turns,
        "turns": protocol["turns"],
        "content_tokens": protocol["content_tokens"],
        "stable_prefix_tokens": protocol["stable_prefix_tokens"],
        "tool_surface_limitation": TOOL_SURFACE_LIMITATION,
        "allow_model_mismatch": bool(allow_mismatch),
        "arms_requested": list(arms),
        "preflight": preflight,
        "protocol_fingerprint": {
            "system_chars": len(protocol["system"]),
            "tools_count": len(protocol["tools"]),
            "manifest_chars": len(protocol["tool_manifest_text"]),
            "user_turn_chars": [len(u) for u in protocol["user_turns"]],
            "stable_prefix_tokens": protocol["stable_prefix_tokens"],
        },
        "arms": {},
        "claim_hygiene": {
            "no_plan_vs_api_usd_headline": True,
            "missing_cache_is_null": True,
            "negative_control_wire_markers_only": True,
        },
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_arms(raw: str) -> List[str]:
    arms = [a.strip().lower() for a in (raw or "").split(",") if a.strip()]
    if not arms:
        return list(DEFAULT_ARMS)
    # Canonical CLI names (hyphenated except negative_control receipt key).
    aliases = {
        "codex": "codex",
        "openrouter": "openrouter",
        "cursor": "cursor",
        "native-codex": "native-codex",
        "native_codex": "native-codex",
        "native_codex_cli": "native-codex",
        "negative-control": "negative_control",
        "negative_control": "negative_control",
    }
    normalized: List[str] = []
    for a in arms:
        key = a.replace("_", "-") if a not in aliases else a
        # Prefer exact alias, then hyphenated form.
        canon = aliases.get(a) or aliases.get(key)
        if not canon:
            raise SystemExit(
                f"Unknown arm {a!r}. Choose from: {', '.join(ALL_ARMS)}"
            )
        normalized.append(canon)
    return normalized


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--live",
        action="store_true",
        help="Execute provider arms (spend plan credits / API dollars)",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Safe default: build protocol + hermetic checks, no provider calls",
    )
    ap.add_argument(
        "--arms",
        default=",".join(DEFAULT_ARMS),
        help="Comma list: codex,openrouter,cursor,native-codex,negative_control",
    )
    ap.add_argument("--codex-model", default=DEFAULT_CODEX_MODEL)
    ap.add_argument("--openrouter-model", default=DEFAULT_OPENROUTER_MODEL)
    ap.add_argument("--cursor-model", default=DEFAULT_CURSOR_MODEL)
    ap.add_argument("--native-codex-model", default=DEFAULT_NATIVE_CODEX_MODEL)
    ap.add_argument(
        "--allow-model-mismatch",
        action="store_true",
        help="Do not fail when served model differs from requested",
    )
    ap.add_argument("--turns", type=int, default=DEFAULT_TURNS)
    ap.add_argument("--warmup-turns", type=int, default=DEFAULT_WARMUP_TURNS)
    ap.add_argument(
        "--content-tokens",
        type=int,
        default=DEFAULT_CONTENT_TOKENS,
        help="Deterministic content-token padding per user turn",
    )
    ap.add_argument(
        "--stable-prefix-tokens",
        type=int,
        default=DEFAULT_STABLE_PREFIX_TOKENS,
        help=(
            "Approximate token size of the shared stable system prefix "
            "(ballast to ~tokens*4 chars for cache eligibility)"
        ),
    )
    ap.add_argument("--max-output-tokens", type=int, default=DEFAULT_MAX_OUTPUT_TOKENS)
    ap.add_argument("--idle-seconds", type=float, default=0.0)
    ap.add_argument(
        "--repo",
        "--workspace",
        dest="workspace",
        default=str(REPO_ROOT),
        help="Workspace / repo path for Cursor + native Codex",
    )
    ap.add_argument("--openrouter-base-url", default=DEFAULT_OPENROUTER_BASE)
    ap.add_argument("--cursor-agent-bin", default=None)
    ap.add_argument("--native-codex-bin", default=DEFAULT_NATIVE_CODEX_BIN)
    ap.add_argument(
        "--output",
        default=None,
        help="JSON receipt path (default: temp dir under gettempdir())",
    )
    ap.add_argument(
        "--skip-unavailable",
        action="store_true",
        help="Record a blocker and continue when an arm's prerequisites are missing",
    )
    return ap


def run_dry(
    *,
    protocol: Dict[str, Any],
    models: Dict[str, str],
    arms: Sequence[str],
    warmup_turns: int,
    allow_mismatch: bool,
    cursor_agent_bin: Optional[str],
    native_codex_bin: Optional[str],
) -> Dict[str, Any]:
    """Hermetic dry-run: no auth load, no network, no provider subprocess."""
    preflight = preflight_status(
        arms=arms,
        cursor_agent_bin=cursor_agent_bin,
        native_codex_bin=native_codex_bin,
        load_keys=False,
    )
    payload = build_receipt_skeleton(
        mode="dry-run",
        protocol=protocol,
        models=models,
        warmup_turns=warmup_turns,
        arms=arms,
        preflight=preflight,
        allow_mismatch=allow_mismatch,
    )
    # Hermetic negative-control wire check (always, when requested or by default).
    wire = negative_control_wire_check(
        model=models["codex"],
        session_id="cache-matrix-dry-negative",
    )
    payload["arms"]["negative_control"] = {
        "arm": "negative_control",
        "status": "ok" if wire["ok"] else "wire_check_failed",
        "billing": "plan",
        "cost_source": "plan_estimated",
        "driver": "CodexResponsesDriver",
        "requested_model": models["codex"],
        "wire_check": wire,
        "cache_claim": "wire_marker_absence_only",
        "turns": [],
        "totals": {
            "tokens_in": 0,
            "tokens_out": 0,
            "cache_read": 0,
            "cache_write": 0,
            "errors": 0 if wire["ok"] else 1,
        },
        "warmup_excluded_hit_rate": None,
    }
    # Dry placeholders for requested arms (no live execution).
    for a in arms:
        key = "native_codex_cli" if a == "native-codex" else a
        if key in payload["arms"]:
            continue
        payload["arms"][key] = {
            "arm": key,
            "status": "dry-run",
            "billing": BILLING_BY_ARM.get(a, "unknown"),
            "cost_source": (
                "unknown" if BILLING_BY_ARM.get(a) == "api" else "plan_estimated"
            ),
            "driver": DRIVER_BY_ARM.get(a),
            "requested_model": {
                "codex": models["codex"],
                "openrouter": models["openrouter"],
                "cursor": models["cursor"],
                "native-codex": models["native_codex"],
                "negative_control": models["codex"],
            }.get(a),
            "note": "dry-run: protocol built; no provider calls",
            "turns": [],
            "warmup_excluded_hit_rate": None,
        }
    payload["dry_protocol"] = {
        "system": protocol["system"],
        "tools": protocol["tools"],
        "user_turns": protocol["user_turns"],
        "native_prompts": protocol["native_prompts"],
        "native_user_prompts": protocol["native_user_prompts"],
        "stable_prefix_tokens": protocol["stable_prefix_tokens"],
    }
    if not wire["ok"]:
        payload["dry_ok"] = False
    else:
        payload["dry_ok"] = True
    return payload


def main(argv: Optional[List[str]] = None) -> int:
    ap = build_arg_parser()
    args = ap.parse_args(argv)

    if not args.live and not args.dry_run:
        args.dry_run = True
    if args.live and args.dry_run:
        # Live wins when both passed; dry-run is the default without --live.
        args.dry_run = False

    arms = parse_arms(args.arms)
    models = {
        "codex": args.codex_model,
        "openrouter": args.openrouter_model,
        "cursor": args.cursor_model,
        "native_codex": args.native_codex_model,
    }
    protocol = build_protocol(
        turns=max(1, int(args.turns)),
        content_tokens=max(0, int(args.content_tokens)),
        stable_prefix_tokens=max(0, int(args.stable_prefix_tokens)),
    )
    out_path = Path(args.output) if args.output else default_output_path()

    if not args.live:
        payload = run_dry(
            protocol=protocol,
            models=models,
            arms=arms,
            warmup_turns=max(0, int(args.warmup_turns)),
            allow_mismatch=bool(args.allow_model_mismatch),
            cursor_agent_bin=args.cursor_agent_bin,
            native_codex_bin=args.native_codex_bin,
        )
        json_path, md_path = write_receipt(payload, out_path)
        print(f"dry-run ok={payload.get('dry_ok')} arms={','.join(arms)}")
        print(f"Wrote {json_path}")
        print(f"Wrote {md_path}")
        return 0 if payload.get("dry_ok") else 2

    # Live path — sequential arms, fresh session each.
    preflight = preflight_status(
        arms=arms,
        cursor_agent_bin=args.cursor_agent_bin,
        native_codex_bin=args.native_codex_bin,
        load_keys=True,
    )
    payload = build_receipt_skeleton(
        mode="live",
        protocol=protocol,
        models=models,
        warmup_turns=max(0, int(args.warmup_turns)),
        arms=arms,
        preflight=preflight,
        allow_mismatch=bool(args.allow_model_mismatch),
    )

    for arm in arms:
        print(f"== arm {arm} ==")
        try:
            result = run_live_arm(
                arm,
                protocol=protocol,
                models=models,
                workspace=args.workspace,
                openrouter_base=args.openrouter_base_url,
                cursor_agent_bin=args.cursor_agent_bin,
                native_codex_bin=args.native_codex_bin,
                max_output_tokens=args.max_output_tokens,
                warmup_turns=max(0, int(args.warmup_turns)),
                idle_seconds=float(args.idle_seconds),
                allow_mismatch=bool(args.allow_model_mismatch),
                skip_unavailable=bool(args.skip_unavailable),
                preflight=preflight,
            )
        except (ArmUnavailableError, ModelMismatchError) as e:
            print(f"   FAIL: {e}")
            json_path, md_path = write_receipt(payload, out_path)
            print(f"Wrote partial {json_path}")
            return 1
        key = result.get("arm") or arm
        payload["arms"][key] = result
        hit = result.get("warmup_excluded_hit_rate")
        print(
            f"   status={result.get('status')} billing={result.get('billing')} "
            f"cost_source={result.get('cost_source')} "
            f"hit_rate={hit if hit is not None else 'unknown'} "
            f"errors={(result.get('totals') or {}).get('errors', 0)}"
        )

    json_path, md_path = write_receipt(payload, out_path)
    print(f"\nWrote {json_path}")
    print(f"Wrote {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
