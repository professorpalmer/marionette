"""O(1) TTFT / provider-output timing for the interactive pilot stream.

Measures provider-callback time and backend event-ready time only:

- ``request_to_first_content_callback_ms`` — first content-bearing provider
  callback (reasoning or answer), including raw JSON envelope prefixes.
- ``request_to_first_answer_callback_ms`` — first answer-classified callback
  (still the raw provider payload, not cleaned say).
- ``request_to_first_visible_answer_ms`` — first cleaned-answer instant at
  the ``StreamingSayExtractor`` boundary, immediately before the first
  answer ``message_delta``. Backend event-ready time, not browser paint.

True browser first-paint and renderer paint acknowledgment are later
frontend waves — not measured here.

``provider_output_tokens_per_second`` is provider ``tokens_out`` over the
first-content → last-content callback window, not answer-token decode TPS
and not total-request throughput (``provider_call_total_ms`` / wall-clock
from request start). ``tokens_out`` may include reasoning and tool output;
the fixed ``throughput_basis`` string records that.

``provider_call_total_ms`` is request dispatch → provider return.
``backend_ready_total_ms`` is request dispatch → drain terminal (stream
path only). Visible answer may land after provider return; that is valid
when compared with the backend-ready boundary, not the provider-call
total. Sync / complete attach provider-call total only.

Pre-request phase keys (when a provider-step origin and named phases exist)
are best-effort monotonic durations on the same accumulator. Existing
request-relative keys stay relative to ``mark_request_start``, not turn start.

``prompt_tools_ms`` is a composite Wave 3A key: additive sys-prompt / history
render plus tool-schema assembly, not one contiguous block.
``user_append_ms`` is user-message / context assembly (optional context
trailer, native image encode, history append). Plan-mode suffix work is
outside this key.
"""

from __future__ import annotations

import math
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, Optional

from .stream_identity import normalize_delta_payload

STREAM_PERFORMANCE_KEY = "stream_performance"

# Honest callback / visible / throughput keys (no production readers yet).
FIRST_CONTENT_CALLBACK_MS = "request_to_first_content_callback_ms"
FIRST_ANSWER_CALLBACK_MS = "request_to_first_answer_callback_ms"
FIRST_VISIBLE_ANSWER_MS = "request_to_first_visible_answer_ms"
PROVIDER_OUTPUT_TPS = "provider_output_tokens_per_second"
PROVIDER_CALL_TOTAL_MS = "provider_call_total_ms"
BACKEND_READY_TOTAL_MS = "backend_ready_total_ms"
THROUGHPUT_BASIS_KEY = "throughput_basis"
THROUGHPUT_BASIS = "provider_tokens_out/content_callback_window"

# Fixed schema — snapshot never emits arbitrary caller-chosen keys.
# prompt_tools_ms is additive (prompt render + tool-schema), not contiguous.
PRE_REQUEST_PHASE_NAMES = (
    "image_prep",
    "task_profile",
    "user_append",
    "auto_codegraph",
    "advisory_compaction",
    "step_codegraph",
    "step_wiki",
    "prompt_tools",
    "thread_start",
)
PRE_REQUEST_PHASES = frozenset(PRE_REQUEST_PHASE_NAMES)
# prompt_tools / thread_start are per provider attempt. The rest are reusable
# turn/step setup kept across a step-0 CONTEXT_OVERFLOW retry.
PER_ATTEMPT_PHASES = frozenset({"prompt_tools", "thread_start"})
REUSABLE_PRE_REQUEST_PHASES = PRE_REQUEST_PHASES - PER_ATTEMPT_PHASES

# Callback / payload markers that are never content tokens.
_NON_CONTENT_KINDS = frozenset({
    "tool_hint", "wait", "item_done", "done", "error",
    "usage", "header", "keepalive", "ping", "heartbeat",
    "complete", "completion", "stop", "finish",
    "progress", "tool", "tool_call",
})
_NON_CONTENT_MARKERS = frozenset({
    "usage", "header", "headers", "keepalive", "keep-alive",
    "ping", "heartbeat", "done", "complete", "completion",
    "stop", "finish", "item_done", "tool_hint", "wait",
    "progress", "tool", "tool_call",
})
_NON_CONTENT_CHANNELS = frozenset({
    "progress", "tool", "tool_call",
})


def classify_stream_event(kind: Any, payload: Any = None) -> Optional[str]:
    """Return ``reasoning``, ``answer``, or None if the event is not content.

    Empty deltas, tool hints, headers, usage, keepalives, completion markers,
    progress/tool kinds, and progress/tool channels do not count.
    """
    kind_s = str(kind or "").strip().lower()
    if kind_s in _NON_CONTENT_KINDS:
        return None
    if isinstance(payload, dict):
        marker = payload.get("type") or payload.get("event") or payload.get("kind")
        if isinstance(marker, str) and marker.strip().lower() in _NON_CONTENT_MARKERS:
            return None
        # Usage-only chunks sometimes arrive with an empty text field.
        if payload.get("usage") is not None:
            text, _meta = normalize_delta_payload(payload)
            if not str(text).strip():
                return None
    text, meta = normalize_delta_payload(payload)
    if not str(text).strip():
        return None
    channel = str(meta.get("channel") or "").strip().lower()
    if channel in _NON_CONTENT_CHANNELS:
        return None
    if kind_s in ("reasoning", "thinking") or channel == "reasoning":
        return "reasoning"
    if kind_s in ("delta", "answer") or channel in ("", "answer"):
        return "answer"
    return None


def _finite_ms(seconds: Any) -> Optional[float]:
    try:
        ms = float(seconds) * 1000.0
    except (TypeError, ValueError):
        return None
    if not math.isfinite(ms):
        return None
    # Snap binary noise so JSON snapshots stay exact under a fake clock.
    return round(ms, 6)


def _nonneg_ms(seconds: Any) -> Optional[float]:
    ms = _finite_ms(seconds)
    if ms is None or ms < 0:
        return None
    return ms


def _provider_tokens_out(tokens_out: Any) -> Optional[int]:
    """Return a positive provider token count, or None when unusable.

    ``bool`` is rejected (``True`` is a truthy ``int`` subclass). Never raises.
    """
    if tokens_out is None or isinstance(tokens_out, bool):
        return None
    try:
        if isinstance(tokens_out, str):
            text = tokens_out.strip()
            if not text:
                return None
            n_out = int(text)
        else:
            n_out = int(tokens_out)
    except (TypeError, ValueError, OverflowError):
        return None
    if n_out <= 0:
        return None
    return n_out


class StreamTimingAccumulator:
    """O(1) stream clock: start, first/last content, count, max inter-delta gap.

    Does not retain every timestamp. ``clock`` is a monotonic callable so tests
    can inject a deterministic timeline.

    Construction is the provider-step origin. ``mark_request_start`` moves the
    TTFT/TPS origin to the actual provider dispatch boundary. When that marker
    is never called, snapshot semantics match Wave 1 (origin == request start).

    Send thread, stream thread, and callback thread may all touch this object.
    Mutations take a small lock and never hold it while calling the clock or
    other external code. Timing failures never raise to the hot path.
    """

    def __init__(self, clock: Optional[Callable[[], float]] = None) -> None:
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        now = self._now()
        self._origin = now
        self._t0 = now
        self._request_marked = False
        self._first_content: Optional[float] = None
        self._first_answer: Optional[float] = None
        self._first_visible_answer: Optional[float] = None
        self._last_content: Optional[float] = None
        self._prev_content: Optional[float] = None
        self._end: Optional[float] = None
        self._backend_end: Optional[float] = None
        self._count = 0
        self._max_gap: Optional[float] = None
        self._phase_open: Dict[str, float] = {}
        self._phase_s: Dict[str, float] = {}

    def _now(self) -> float:
        return float(self._clock())

    def mark_request_start(self) -> None:
        """Mark the provider dispatch boundary. Never raises.

        Subsequent ``request_to_first_*`` keys are relative to this instant.
        """
        try:
            now = self._now()
            with self._lock:
                self._t0 = now
                self._request_marked = True
        except Exception:
            return

    def begin_phase(self, name: Any) -> None:
        """Open a named pre-request phase. Unknown names are ignored. Never raises."""
        try:
            key = str(name or "").strip()
            if key not in PRE_REQUEST_PHASES:
                return
            now = self._now()
            with self._lock:
                if key in self._phase_open:
                    return
                self._phase_open[key] = now
        except Exception:
            return

    def end_phase(self, name: Any) -> None:
        """Close a named pre-request phase. Unopened names are omitted. Never raises.

        Repeated open/close of the same name adds durations (composite phases
        such as ``prompt_tools`` and suspension-excluding generator segments).
        """
        try:
            key = str(name or "").strip()
            now = self._now()
            with self._lock:
                start = self._phase_open.pop(key, None)
                if start is None:
                    return
                delta = now - start
                if not math.isfinite(delta) or delta < 0:
                    return
                prev = self._phase_s.get(key)
                self._phase_s[key] = delta if prev is None else prev + delta
        except Exception:
            return

    def reset_for_next_provider_call(self) -> None:
        """Fresh origin for the next provider call. Never raises.

        Drops phase durations (including turn-once) and streaming marks so
        later tool-loop steps do not blend receipts. Step-0 overflow retries
        use ``reset_provider_attempt_marks`` instead, which keeps closed
        reusable setup phases and rebases the origin.
        """
        try:
            now = self._now()
            with self._lock:
                self._origin = now
                self._t0 = now
                self._request_marked = False
                self._first_content = None
                self._first_answer = None
                self._first_visible_answer = None
                self._last_content = None
                self._prev_content = None
                self._end = None
                self._backend_end = None
                self._count = 0
                self._max_gap = None
                self._phase_open = {}
                self._phase_s = {}
        except Exception:
            return

    def reset_provider_attempt_marks(self) -> None:
        """Drop this attempt's stream marks and per-attempt phases. Never raises.

        Clears request start, callbacks, visible, provider/backend ends,
        count, and inter-delta gap. Drops ``prompt_tools`` / ``thread_start``
        durations and every open phase so they cannot span the retry.
        Closed reusable turn/step setup phases survive. Rebases the
        construction origin so ``pre_request_total_ms`` is retry preparation
        to retry dispatch, not the failed provider-call window.
        """
        try:
            now = self._now()
            with self._lock:
                self._origin = now
                self._t0 = now
                self._request_marked = False
                self._first_content = None
                self._first_answer = None
                self._first_visible_answer = None
                self._last_content = None
                self._prev_content = None
                self._end = None
                self._backend_end = None
                self._count = 0
                self._max_gap = None
                self._phase_open = {}
                self._phase_s = {
                    key: value
                    for key, value in self._phase_s.items()
                    if key in REUSABLE_PRE_REQUEST_PHASES
                }
        except Exception:
            return

    def note(self, kind: Any, payload: Any = None) -> None:
        """Record a provider callback. Never raises."""
        try:
            role = classify_stream_event(kind, payload)
            if role is None:
                return
            now = self._now()
            with self._lock:
                if self._first_content is None or now < self._first_content:
                    self._first_content = now
                if role == "answer" and (
                    self._first_answer is None or now < self._first_answer
                ):
                    self._first_answer = now
                if self._prev_content is not None:
                    gap = now - self._prev_content
                    if (
                        math.isfinite(gap)
                        and gap >= 0
                        and (self._max_gap is None or gap > self._max_gap)
                    ):
                        self._max_gap = gap
                if self._prev_content is None or now >= self._prev_content:
                    self._prev_content = now
                if self._last_content is None or now > self._last_content:
                    self._last_content = now
                self._count += 1
        except Exception:
            return

    def mark_first_visible_answer(self) -> None:
        """First cleaned-answer / message_delta-ready instant. Never raises.

        Backend event-ready time at the say-extractor boundary — not browser
        paint and not the raw provider callback.
        """
        try:
            now = self._now()
            with self._lock:
                if (
                    self._first_visible_answer is None
                    or now < self._first_visible_answer
                ):
                    self._first_visible_answer = now
        except Exception:
            return

    def finish(self) -> None:
        """Mark provider-call return. Never raises.

        This is request dispatch → provider return, not drain terminal.
        """
        try:
            now = self._now()
            with self._lock:
                self._end = now
        except Exception:
            return

    def mark_backend_ready(self) -> None:
        """Mark drain-terminal / backend event-ready. Never raises.

        Stream path only. Sync / complete must not call this.
        """
        try:
            now = self._now()
            with self._lock:
                if self._backend_end is None:
                    self._backend_end = now
        except Exception:
            return

    def snapshot(self, tokens_out: Any = 0) -> Dict[str, Any]:
        """JSON-safe metrics. Omit keys that lack evidence; never emit inf.

        ``provider_output_tokens_per_second`` uses provider output tokens over
        the first-content → last-content callback window, not request-start →
        stream-end and not answer-only decode. Callback TTFT keys stay
        relative to request start (``mark_request_start`` or construction).
        Malformed ``tokens_out`` omits the rate; it does not drop the receipt.
        """
        try:
            with self._lock:
                t0 = self._t0
                origin = self._origin
                request_marked = self._request_marked
                first_content = self._first_content
                first_answer = self._first_answer
                first_visible = self._first_visible_answer
                last_content = self._last_content
                end = self._end
                backend_end = self._backend_end
                count = int(self._count)
                max_gap = self._max_gap
                phase_s = dict(self._phase_s)
        except Exception:
            return {"content_delta_count": 0}

        out: Dict[str, Any] = {"content_delta_count": count}
        try:
            if end is not None:
                total = _nonneg_ms(end - t0)
                if total is not None:
                    out[PROVIDER_CALL_TOTAL_MS] = total
            if backend_end is not None:
                backend_ms = _nonneg_ms(backend_end - t0)
                if backend_ms is not None:
                    out[BACKEND_READY_TOTAL_MS] = backend_ms
            if first_content is not None:
                first_event = _nonneg_ms(first_content - t0)
                if first_event is not None:
                    out[FIRST_CONTENT_CALLBACK_MS] = first_event
            if first_answer is not None:
                first_answer_ms = _nonneg_ms(first_answer - t0)
                if first_answer_ms is not None:
                    out[FIRST_ANSWER_CALLBACK_MS] = first_answer_ms
            if first_visible is not None:
                visible_ms = _nonneg_ms(first_visible - t0)
                if visible_ms is not None:
                    out[FIRST_VISIBLE_ANSWER_MS] = visible_ms
            window_ms = None
            if first_content is not None and last_content is not None:
                window_ms = _nonneg_ms(last_content - first_content)
                if window_ms is not None and window_ms > 0:
                    out["decode_window_ms"] = window_ms
            if max_gap is not None:
                gap_ms = _nonneg_ms(max_gap)
                if gap_ms is not None:
                    out["max_inter_delta_ms"] = gap_ms
            n_out = _provider_tokens_out(tokens_out)
            if (
                n_out is not None
                and count >= 2
                and window_ms is not None
                and window_ms > 0
            ):
                tps = n_out / (window_ms / 1000.0)
                if math.isfinite(tps) and tps > 0:
                    out[PROVIDER_OUTPUT_TPS] = round(tps, 6)
                    out[THROUGHPUT_BASIS_KEY] = THROUGHPUT_BASIS
            if request_marked:
                pre = _nonneg_ms(t0 - origin)
                if pre is not None:
                    out["pre_request_total_ms"] = pre
            for name in PRE_REQUEST_PHASE_NAMES:
                seconds = phase_s.get(name)
                if seconds is None:
                    continue
                phase_ms = _nonneg_ms(seconds)
                if phase_ms is not None:
                    out[f"{name}_ms"] = phase_ms
        except Exception:
            return out
        return out


def make_stream_timing_accumulator(
    clock: Optional[Callable[[], float]] = None,
) -> Optional[StreamTimingAccumulator]:
    """Construct an accumulator. Returns None if the clock fails. Never raises."""
    try:
        return StreamTimingAccumulator(clock=clock)
    except Exception:
        return None


def reset_provider_step_timing(acc: Any) -> None:
    """Reset provider-attempt marks; keep closed reusable phases. Never raises.

    Used by step-0 CONTEXT_OVERFLOW retry. Drops per-attempt phases and
    rebases origin. Later tool-loop steps call ``reset_timing_before_step``,
    which drops every phase.
    """
    if acc is None:
        return
    try:
        reset = getattr(acc, "reset_provider_attempt_marks", None)
        if callable(reset):
            reset()
            return
        acc.reset_for_next_provider_call()
    except Exception:
        return


def reset_timing_before_step(acc: Any, step: Any) -> None:
    """Loop-boundary reset: drop the prior receipt when ``step`` is past 0.

    Step 0 keeps turn-once phases on the first provider snapshot. Later
    steps start a fresh origin and drop phases. Overflow retries call
    ``reset_provider_step_timing`` instead so closed reusable durations
    survive and per-attempt phases are measured fresh. Never raises.
    """
    if acc is None:
        return
    try:
        if step:
            acc.reset_for_next_provider_call()
    except Exception:
        return


def _safe_begin_phase(acc: Any, name: str) -> None:
    if acc is None:
        return
    try:
        acc.begin_phase(name)
    except Exception:
        return


def _safe_end_phase(acc: Any, name: str) -> None:
    if acc is None:
        return
    try:
        acc.end_phase(name)
    except Exception:
        return


@contextmanager
def timed_phase(acc: Any, name: str) -> Iterator[None]:
    """Time a function-phase block. Timing failures degrade to normal execution.

    Do not wrap ``yield`` / ``yield from`` with this helper — consumer
    suspension would enter ``phase_ms``. Use ``yield_timed_phase`` or split
    the yield outside the timed block.
    """
    _safe_begin_phase(acc, name)
    try:
        yield
    finally:
        _safe_end_phase(acc, name)


def call_timed_phase(acc: Any, name: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Time a function call and return its value exactly. Never raises from timing."""
    with timed_phase(acc, name):
        return fn(*args, **kwargs)


def _delegate_inner(it: Any, sent: Any, throwing: bool) -> Any:
    """Advance ``it`` with send/throw, matching PEP 380 for this wrapper."""
    if throwing:
        thrower = getattr(it, "throw", None)
        if thrower is None:
            raise sent
        return thrower(type(sent), sent, sent.__traceback__)
    sender = getattr(it, "send", None)
    if sender is None:
        if sent is not None:
            raise TypeError("can't send non-None value to a non-generator iterator")
        return next(it)
    return sender(sent)


def yield_timed_phase(acc: Any, name: str, iterator: Iterator[Any]) -> Iterator[Any]:
    """Time harness work between yields; exclude consumer suspension.

    True PEP 380-style delegation for this usage: ``send``, ``throw``, and
    ``close`` / ``GeneratorExit`` are forwarded; ``StopIteration.value`` and
    inner ``finally`` cleanup stay exact. Each resume → yield (or resume →
    return) segment is added into the named phase. Time spent suspended at
    ``yield`` is not. Timing failures never raise. Python 3.9 compatible.
    """
    it = iter(iterator)
    sent: Any = None
    throwing = False
    started = False
    while True:
        _safe_begin_phase(acc, name)
        try:
            if not started:
                value = next(it)
            else:
                value = _delegate_inner(it, sent, throwing)
        except StopIteration as stop:
            _safe_end_phase(acc, name)
            return stop.value
        except BaseException:
            _safe_end_phase(acc, name)
            raise
        _safe_end_phase(acc, name)
        started = True
        throwing = False
        try:
            sent = yield value
        except GeneratorExit:
            closer = getattr(it, "close", None)
            if closer is not None:
                _safe_begin_phase(acc, name)
                try:
                    closer()
                except BaseException:
                    _safe_end_phase(acc, name)
                    raise
                _safe_end_phase(acc, name)
            raise
        except BaseException as exc:
            throwing = True
            sent = exc


def attach_stream_performance(
    resp: Any,
    snapshot: Dict[str, Any],
) -> None:
    """Merge ``stream_performance`` into ``resp.meta`` without clobbering keys.

    Key-wise fill-if-absent: keep every pre-existing nested key, add only
    missing snapshot keys. Leaves ``latency_ms`` and unrelated meta untouched.
    Never raises.
    """
    try:
        if not isinstance(snapshot, dict):
            return
        meta = getattr(resp, "meta", None)
        if not isinstance(meta, dict):
            meta = {}
            resp.meta = meta
        existing = meta.get(STREAM_PERFORMANCE_KEY)
        if not isinstance(existing, dict):
            meta[STREAM_PERFORMANCE_KEY] = dict(snapshot)
            return
        for key, value in snapshot.items():
            if key not in existing:
                existing[key] = value
    except Exception:
        return
