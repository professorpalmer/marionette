"""Optional stdlib-only observability side export beside savings/event owners.

Emits OTEL-inspired JSON Lines envelopes to a configured local file and/or an
HTTP endpoint. Gated by ``HARNESS_OBSERVABILITY_EXPORT`` (default off). Never
imports OpenTelemetry, never mutates ledger SQLite schemas, and never raises on
the chat hot path — bounded queue, short I/O timeout, failures swallowed.

Env (do not reuse Puppetmaster ``OTEL_*`` names):

- ``HARNESS_OBSERVABILITY_EXPORT`` — master switch (0/off default)
- ``HARNESS_OBSERVABILITY_EXPORT_FILE`` — append JSONL path (optional)
- ``HARNESS_OBSERVABILITY_EXPORT_ENDPOINT`` — HTTP POST URL (optional)
- ``HARNESS_OBSERVABILITY_EXPORT_TIMEOUT_MS`` — per-request timeout (default 50)
- ``HARNESS_OBSERVABILITY_EXPORT_MAX_ATTRS`` — attribute cap (default 32)
- ``HARNESS_OBSERVABILITY_EXPORT_MAX_ATTR_LEN`` — string value cap (default 512)
"""
from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Mapping, Optional

_SCOPE_NAME = "harness.observability_export"
_SCOPE_VERSION = "1.0.0"
_SERVICE_NAME = "marionette-harness"

_SECRET_FRAGMENTS = (
    re.compile(r"password", re.IGNORECASE),
    re.compile(r"secret", re.IGNORECASE),
    re.compile(r"api[_-]?key", re.IGNORECASE),
    re.compile(r"authorization", re.IGNORECASE),
    re.compile(r"credential", re.IGNORECASE),
    re.compile(r"bearer", re.IGNORECASE),
    re.compile(r"(^|_)token($|_)", re.IGNORECASE),
    re.compile(r"access[_-]?token", re.IGNORECASE),
    re.compile(r"auth[_-]?token", re.IGNORECASE),
)
_BEARER_RE = re.compile(r"^Bearer\s+\S+", re.IGNORECASE)
_SK_RE = re.compile(r"^sk-[A-Za-z0-9_-]{8,}")


def _is_secret_key(key: str) -> bool:
    normalized = key.replace("-", "_")
    return any(p.search(normalized) for p in _SECRET_FRAGMENTS)

_DEFAULT_TIMEOUT_MS = 50
_DEFAULT_MAX_ATTRS = 32
_DEFAULT_MAX_ATTR_LEN = 512
_QUEUE_MAX = 256

_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=_QUEUE_MAX)
_worker_started = False
_worker_lock = threading.Lock()


def _truthy(name: str) -> bool:
    return os.environ.get(name, "0").strip().lower() in ("1", "true", "yes", "on")


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    if not raw.strip():
        return default
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return default


def export_enabled() -> bool:
    """Return True when side export is configured and switched on."""
    if not _truthy("HARNESS_OBSERVABILITY_EXPORT"):
        return False
    file_path = (os.environ.get("HARNESS_OBSERVABILITY_EXPORT_FILE") or "").strip()
    endpoint = (os.environ.get("HARNESS_OBSERVABILITY_EXPORT_ENDPOINT") or "").strip()
    return bool(file_path or endpoint)


def _redact_string(value: str, max_len: int) -> str:
    text = value
    if _BEARER_RE.match(text) or _SK_RE.match(text):
        return "[REDACTED]"
    if len(text) > max_len:
        return text[: max_len - 3] + "..."
    return text


def _sanitize_attributes(
    attrs: Mapping[str, Any],
    *,
    max_attrs: int,
    max_len: int,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, raw in attrs.items():
        if len(out) >= max_attrs:
            break
        k = str(key)
        if _is_secret_key(k):
            out[k] = "[REDACTED]"
            continue
        if raw is None:
            out[k] = None
        elif isinstance(raw, bool):
            out[k] = raw
        elif isinstance(raw, (int, float)):
            out[k] = raw
        else:
            out[k] = _redact_string(str(raw), max_len)
    return out


def build_envelope(
    name: str,
    attributes: Mapping[str, Any],
    *,
    kind: str = "event",
    timestamp_ns: Optional[int] = None,
) -> dict[str, Any]:
    """Build an OTEL-inspired JSON envelope (stdlib-only, no OTEL import)."""
    max_attrs = _int_env("HARNESS_OBSERVABILITY_EXPORT_MAX_ATTRS", _DEFAULT_MAX_ATTRS)
    max_len = _int_env(
        "HARNESS_OBSERVABILITY_EXPORT_MAX_ATTR_LEN", _DEFAULT_MAX_ATTR_LEN
    )
    ts = timestamp_ns if timestamp_ns is not None else time.time_ns()
    return {
        "resource": {
            "attributes": {
                "service.name": _SERVICE_NAME,
            }
        },
        "instrumentation_scope": {
            "name": _SCOPE_NAME,
            "version": _SCOPE_VERSION,
        },
        "time_unix_nano": str(int(ts)),
        "kind": kind,
        "name": name,
        "attributes": _sanitize_attributes(
            attributes, max_attrs=max_attrs, max_len=max_len
        ),
    }


def _ensure_worker() -> None:
    global _worker_started
    if _worker_started:
        return
    with _worker_lock:
        if _worker_started:
            return
        thread = threading.Thread(target=_worker_loop, name="observability-export", daemon=True)
        thread.start()
        _worker_started = True


def _worker_loop() -> None:
    while True:
        try:
            envelope = _queue.get()
        except Exception:
            continue
        try:
            _deliver(envelope)
        except Exception:
            pass
        finally:
            try:
                _queue.task_done()
            except Exception:
                pass


def _deliver(envelope: dict[str, Any]) -> None:
    line = json.dumps(envelope, separators=(",", ":"), ensure_ascii=False) + "\n"
    file_path = (os.environ.get("HARNESS_OBSERVABILITY_EXPORT_FILE") or "").strip()
    endpoint = (os.environ.get("HARNESS_OBSERVABILITY_EXPORT_ENDPOINT") or "").strip()
    timeout_s = _int_env("HARNESS_OBSERVABILITY_EXPORT_TIMEOUT_MS", _DEFAULT_TIMEOUT_MS) / 1000.0

    if file_path:
        try:
            parent = os.path.dirname(os.path.abspath(file_path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(file_path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(line)
        except OSError:
            pass

    if endpoint:
        body = json.dumps(envelope, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            endpoint,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                resp.read(256)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError):
            pass


def emit_event(name: str, attributes: Mapping[str, Any], *, kind: str = "event") -> None:
    """Best-effort enqueue of one observability event; never raises."""
    if not export_enabled():
        return
    try:
        envelope = build_envelope(name, attributes, kind=kind)
        _ensure_worker()
        _queue.put_nowait(envelope)
    except Exception:
        pass


def export_tool_output_savings(
    *,
    session_id: str,
    tool_call_id: str,
    original_chars: int,
    compact_chars: int,
    tokens_saved: int,
    reason: str = "",
    job_id: Optional[str] = None,
    basis: str = "measured",
) -> None:
    """Side export after tool-output spill savings are recorded."""
    emit_event(
        "harness.tool_output.savings",
        {
            "session_id": session_id or "default",
            "tool_call_id": tool_call_id,
            "original_chars": int(original_chars),
            "compact_chars": int(compact_chars),
            "tokens_saved": int(tokens_saved),
            "reason": reason or "compact",
            "job_id": job_id or "",
            "basis": basis,
        },
    )


def export_routing_savings(
    *,
    job_id: str,
    session_id: str = "",
    routing_saved_usd: float = 0.0,
    routing_savings_basis: str = "",
    model_id: str = "",
    baseline_model_id: str = "",
    tokens_compared: int = 0,
) -> None:
    """Side export when routing savings are stamped (preflight or realized)."""
    if routing_saved_usd <= 0 and not routing_savings_basis:
        return
    emit_event(
        "harness.routing.savings",
        {
            "job_id": job_id,
            "session_id": session_id or "",
            "routing_saved_usd": round(float(routing_saved_usd), 6),
            "routing_savings_basis": routing_savings_basis or "unknown",
            "model_id": model_id or "",
            "baseline_model_id": baseline_model_id or "",
            "tokens_compared": int(tokens_compared or 0),
        },
    )


def export_history_compaction(
    *,
    session_id: str,
    event_kind: str,
    messages_compacted: int = 0,
    chars_before: int = 0,
    chars_after: int = 0,
    tokens_before: int = 0,
    tokens_after: int = 0,
    cache_read_tokens: int = 0,
    cache_bust_tokens: int = 0,
    estimated_cost_usd: Optional[float] = None,
    savings_pct: Optional[float] = None,
    compact_policy: Optional[str] = None,
    basis: str = "measured",
) -> None:
    """Side export after history compaction journal append."""
    attrs: dict[str, Any] = {
        "session_id": session_id or "default",
        "event_kind": event_kind or "compact",
        "messages_compacted": int(messages_compacted),
        "chars_before": int(chars_before),
        "chars_after": int(chars_after),
        "tokens_before": int(tokens_before or 0),
        "tokens_after": int(tokens_after or 0),
        "cache_read_tokens": max(0, int(cache_read_tokens or 0)),
        "cache_bust_tokens": max(0, int(cache_bust_tokens or 0)),
        "basis": basis,
    }
    if estimated_cost_usd is not None:
        attrs["estimated_cost_usd"] = round(float(estimated_cost_usd), 6)
    if savings_pct is not None:
        attrs["savings_pct"] = float(savings_pct)
    if compact_policy:
        attrs["compact_policy"] = compact_policy
    emit_event("harness.history.compaction", attrs)


def export_local_job_terminal(
    *,
    job_id: str,
    session_id: str = "",
    status: str,
    engine: str = "",
    model: str = "",
    tokens: int = 0,
    est_cost_usd: float = 0.0,
    cost_provenance: str = "",
    routing_saved_usd: float = 0.0,
    routing_savings_basis: str = "",
    summary: str = "",
) -> None:
    """Side export when an in-process local job reaches a terminal state."""
    emit_event(
        "harness.local_job.terminal",
        {
            "job_id": job_id,
            "session_id": session_id or "",
            "status": status,
            "engine": engine or "",
            "model": model or "",
            "tokens": int(tokens or 0),
            "est_cost_usd": round(float(est_cost_usd or 0.0), 6),
            "cost_provenance": cost_provenance or "",
            "routing_saved_usd": round(float(routing_saved_usd or 0.0), 6),
            "routing_savings_basis": routing_savings_basis or "",
            "summary": (summary or "")[:240],
        },
    )


def _reset_for_tests() -> None:
    """Drain queue and reset worker flag (tests only)."""
    global _worker_started
    while True:
        try:
            _queue.get_nowait()
            _queue.task_done()
        except queue.Empty:
            break
    _worker_started = False
