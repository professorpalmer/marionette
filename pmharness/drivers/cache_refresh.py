"""One bounded cache refresh; idle scheduling belongs to the session runner.

Only an accepted request on an explicitly supported transport can seed this
capability. A refresh replays that input, never a synthetic conversation turn.
"""
from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from typing import Optional
from functools import wraps

from .request_boundary import http_request


# Deliberately finite. New models/endpoints need an explicit capability review.
ANTHROPIC_MODELS = frozenset({
    "claude-sonnet-4-20250514", "claude-opus-4-20250514",
    "claude-opus-4-1-20250805", "claude-sonnet-4-5", "claude-sonnet-4-5-20250929",
    "claude-haiku-4-5", "claude-haiku-4-5-20251001",
    "claude-sonnet-4-6", "claude-opus-4-6", "claude-opus-4-7", "claude-opus-5",
})
OPENAI_MODELS = frozenset({
    "gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano", "gpt-4o", "gpt-4o-mini",
    "gpt-5", "gpt-5-mini", "gpt-5-nano", "gpt-5.1", "gpt-5.2", "gpt-5.4",
    "gpt-5.6", "gpt-5.6-mini", "gpt-6-astra", "gpt-6-astra-mini",
})
OPENAI_30M_MODELS = frozenset({"gpt-5.6", "gpt-5.6-mini", "gpt-6-astra", "gpt-6-astra-mini"})
REFRESH_TIMEOUT = 8.0
MAX_RESPONSE_BYTES = 1_000_000


def _markers(value):
    if isinstance(value, dict):
        marker = value.get("cache_control")
        if isinstance(marker, dict):
            yield marker
        for child in value.values():
            yield from _markers(child)
    elif isinstance(value, list):
        for child in value:
            yield from _markers(child)


def _text_only(value) -> bool:
    if isinstance(value, dict):
        if value.get("type") in {"image", "image_url", "input_image", "input_audio", "document", "input_file"}:
            return False
        return all(_text_only(v) for v in value.values())
    if isinstance(value, list):
        return all(_text_only(v) for v in value)
    return True


@dataclass(frozen=True)
class CacheSnapshot:
    provider: str
    endpoint: str
    model: str
    body: bytes = field(repr=False)
    headers: tuple = field(repr=False)
    started: float
    ttl_seconds: int
    ttl_guaranteed: bool
    reason: str = ""
    cached_tokens: Optional[int] = None

    @property
    def max_cost_usd(self) -> float:
        # Admission reserve, NOT measured spend. Text tokens cannot exceed UTF-8
        # bytes; $100/M input (including cache writes) + $200/M output is a
        # conservative ceiling for the finite standard model list above.
        return len(self.body) * 100 / 1_000_000 + 200 / 1_000_000


@dataclass(frozen=True)
class RefreshResult:
    http_status: int
    started: float
    usage: Optional[dict]
    cached_tokens: Optional[int]


class RefreshCancelled(Exception):
    pass


def cache_foreground(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        identity = threading.get_ident()
        with self._cache_gate:
            nested = self._cache_owner == identity
            attempt = self._cache_attempt
            self._cache_waiters += 1
        try:
            if attempt is not None:
                attempt.cancel()
                attempt.finished.wait(REFRESH_TIMEOUT + 1)
            with self._cache_gate:
                if not nested and (self._cache_owner is not None or self._cache_attempt is not None):
                    from .base import DriverResponse
                    return DriverResponse(text="", model=self.name,
                                          error="The previous provider request is still stopping; retry shortly.")
                self._cache_owner = identity
            try:
                return method(self, *args, **kwargs)
            finally:
                if not nested:
                    with self._cache_gate:
                        self._cache_owner = None
        finally:
            with self._cache_gate:
                self._cache_waiters -= 1
    return guarded


class RefreshAttempt:
    """Cancellation is independent of the controller's admission lock."""
    def __init__(self):
        self.cancelled = threading.Event()
        self.finished = threading.Event()
        self._socket = None

    def cancel(self):
        self.cancelled.set()
        sock = self._socket
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def attach(self, response):
        self._socket = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
        if self.cancelled.is_set():
            self.cancel()
            raise RefreshCancelled()


def _cached_tokens(provider: str, usage: dict) -> Optional[int]:
    value = (usage.get("cache_read_input_tokens") if provider == "anthropic"
             else (usage.get("input_tokens_details") or {}).get("cached_tokens"))
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


class CacheRefreshDriver:
    """Driver seam: capture a successful request and replay it once, capped."""
    _cache_snapshot: Optional[CacheSnapshot] = None

    def init_cache_refresh(self):
        self._cache_gate = threading.Lock()
        self._cache_owner = None
        self._cache_attempt = None
        self._cache_waiters = 0

    def remember_cache_request(self, provider, body, headers, started, usage):
        endpoint = self.base_url.rstrip("/")
        model = self.model
        reason = ""
        ttl = 0
        guaranteed = True
        if provider == "anthropic":
            if endpoint != "https://api.anthropic.com/v1" or model not in ANTHROPIC_MODELS:
                reason = "Keep warm requires a known Anthropic endpoint and model."
            elif body.get("thinking", {}).get("type") in {"enabled", "adaptive"}:
                reason = "Thinking requires a larger output budget; minimal refresh is unsupported."
            else:
                markers = list(_markers(body))
                if not markers or any(m.get("type") != "ephemeral" or m.get("ttl", "5m") not in {"5m", "1h"} for m in markers):
                    reason = "No accepted cache lifetime in the request."
                else:
                    ttl = min(3600 if m.get("ttl") == "1h" else 300 for m in markers)
        elif provider == "responses":
            if getattr(self, "chatgpt_backend", False):
                reason = "ChatGPT Codex has no accepted hard output cap; keep warm is unsupported."
            elif endpoint != "https://api.openai.com/v1" or model not in OPENAI_MODELS:
                reason = "Keep warm requires a known OpenAI API endpoint and model."
            elif not body.get("max_output_tokens"):
                reason = "The accepted request has no hard output cap."
            else:
                ttl = 1800 if model in OPENAI_30M_MODELS else 300
                guaranteed = model in OPENAI_30M_MODELS
        else:
            reason = "This transport has no bounded cache refresh capability."
        if not reason and not _text_only(body):
            reason = "Keep warm currently supports text requests only."
        # Service tiers and server tools can add non-token charges outside our
        # conservative text-token reserve; never admit those implicitly.
        if not reason and (body.get("service_tier") not in (None, "default") or any(
            t.get("type", "function") != "function" for t in body.get("tools", [])
        )):
            reason = "This request has charges outside the bounded text refresh capability."
        self._cache_snapshot = CacheSnapshot(
            provider, endpoint, model, json.dumps(body, ensure_ascii=False).encode("utf-8"),
            tuple(headers.items()), started, ttl, guaranteed, reason,
            _cached_tokens(provider, usage or {}),
        )

    def cache_snapshot(self) -> Optional[CacheSnapshot]:
        snapshot = self._cache_snapshot
        if snapshot is not None and (self.base_url.rstrip("/"), self.model) != (snapshot.endpoint, snapshot.model):
            return None
        return snapshot

    def refresh_cache(self, snapshot: CacheSnapshot, attempt: RefreshAttempt) -> RefreshResult:
        with self._cache_gate:
            if self._cache_owner is not None or self._cache_attempt is not None or self._cache_waiters:
                raise RefreshCancelled()
            self._cache_attempt = attempt
        try:
            return self._refresh_cache_once(snapshot, attempt)
        finally:
            with self._cache_gate:
                self._cache_attempt = None
            attempt.finished.set()

    def _refresh_cache_once(self, snapshot: CacheSnapshot, attempt: RefreshAttempt) -> RefreshResult:
        if snapshot.reason or self.cache_snapshot() is not snapshot:
            raise ValueError("Cache request capability changed")
        body = json.loads(snapshot.body)
        body["stream"] = False
        body["max_tokens" if snapshot.provider == "anthropic" else "max_output_tokens"] = 1
        path = "/messages" if snapshot.provider == "anthropic" else "/responses"
        started = time.monotonic()
        timer = threading.Timer(REFRESH_TIMEOUT, attempt.cancel)
        timer.daemon = True
        timer.start()
        try:
            if attempt.cancelled.is_set():
                raise RefreshCancelled()
            request = http_request(self, snapshot.endpoint + path, data=json.dumps(body).encode(),
                                   headers=dict(snapshot.headers), method="POST")
            with urllib.request.urlopen(request, timeout=REFRESH_TIMEOUT) as response:
                attempt.attach(response)
                status = response.status
                data = response.read(MAX_RESPONSE_BYTES + 1)
                if len(data) > MAX_RESPONSE_BYTES:
                    raise ValueError("Refresh response exceeded its byte bound")
                raw = json.loads(data)
            if attempt.cancelled.is_set():
                raise RefreshCancelled()
            if status != 200 or raw.get("error"):
                raise ValueError("Refresh did not return a successful provider response")
            usage = raw.get("usage")
            usage = usage if isinstance(usage, dict) else None
            return RefreshResult(status, started, usage, _cached_tokens(snapshot.provider, usage or {}))
        finally:
            timer.cancel()
            attempt._socket = None

    def accept_cache_refresh(self, snapshot: CacheSnapshot, result: RefreshResult):
        if self._cache_snapshot is snapshot:
            self._cache_snapshot = replace(snapshot, started=result.started, cached_tokens=result.cached_tokens)
