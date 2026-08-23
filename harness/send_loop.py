from __future__ import annotations

"""Send-loop mixin for ConversationalSession.

Extracted mechanically from harness/conversation.py to continue decomposing the
ConversationalSession god-object, matching ToolDispatchMixin / BusyControlMixin
contract: these methods operate through `self` (history, busy lock, cancel,
display transcript, pilot, …) provided by the concrete class -- the mixin
defines no state and no __init__.

Owns the turn orchestration entrypoints ``send`` / ``_send_locked`` /
``_send_locked_inner`` plus the small private helpers that exist only to
support that loop (``_is_correction``, ``_get_codegraph_context``). Native /
sidecar image prep lives in ``send_image_prep``. Background thread targets,
stream-queue drain, per-step metering, prefetch pool, idle steer/queue
finalization, read-only/local tool-result assembly, auto-verify, and
action-goal labeling live in ``send_loop_phases``; the per-step action
spree (guards / prefetch / advisor / fan-out) lives in ``send_loop_actions``;
swarm/implement/parallel/route_task/memory dispatch lives in
``send_loop_dispatch`` so the kernel stays the public orchestration surface.
Busy lock lifecycle stays on BusyControlMixin; per-tool ``_do_*`` handlers
stay on ToolDispatchMixin.

CRITICAL invariants — zero behavior change:
- ConvEvent kinds/shapes unchanged
- busy acquire/release/generation unchanged
- SSE detach != cancel
- steer/queue/interrupt/resume semantics unchanged
- tool dispatch still calls mixin ``_do_*`` methods

Method Resolution Order keeps behavior identical: ``send`` still resolves via
inheritance on ConversationalSession.
"""

import os
import re
import subprocess
import sys
import time
from typing import Any, Iterator, Optional

from ._exec import _puppetmaster_available, _puppetmaster_cmd  # noqa: F401 — test patch surface
from .pilot import (
    PilotError,
    parse_pilot_turn,
)
from .pilot_tool_recovery import (
    apply_invalid_only_streak,
    invalid_only_halt_reason,
    parse_native_tool_turn,
)
from .send_image_prep import prepare_turn_images
from .send_loop_actions import execute_turn_actions
from .repeat_tool_reminder import reset_repeat_chain
from .send_loop_phases import (
    account_provider_attempt,
    apply_provider_terminal,
    classified_finish_kwargs,
    dispatch_pilot_provider_call,
    emit_classified_provider_error,
    drain_idle_turn,
    emit_loop_exit_close,
    emit_stagnation_halt,
    finalize_assistant_turn,
    native_tools_blocked,
    promote_trailing_reasoning_to_say,
    record_provider_dispatch_error_receipt,
    run_auto_verify,
)
from .terminal_cause import (
    TERMINAL_DRIVER_SWAP,
    TERMINAL_EMPTY_LOOP,
    TERMINAL_INVALID_TOOL,
    TERMINAL_NATURAL,
    TERMINAL_TURN_BUDGET,
    finalize_stop_cause,
)
from .stream_performance import (
    make_stream_timing_accumulator,
    reset_provider_step_timing,
    reset_timing_before_step,
    timed_phase,
    yield_timed_phase,
)
from .text_clean import clean_say
from .tool_dispatch import _strip_ansi, is_safe_path


POST_SWARM_SYNTHESIS_NUDGE = (
    "(system) The synchronous swarm has completed. "
    "Summarize its completed findings from the complete PM swarm artifact "
    "manifest already pushed into the result. Preserve any partial-delivery warning. "
    "Do not call tools, make plans, or emit progress updates; "
    "return only a concise user-facing synthesis."
)
POST_SWARM_SYNTHESIS_FALLBACK = (
    "The completed swarm did not return a user-facing synthesis. "
    "Its machine-owned PM artifact receipt and inspectable artifact links remain "
    "available in the swarm result above."
)

_ACTION_RESULT_DISPLAY_FIELDS = (
    "job_id", "num", "types", "adapter", "artifacts",
    "error", "duration_ms", "chars", "status", "message",
)


def post_swarm_synthesis_decision(
    *,
    synchronous_swarms: int,
    step_emitted_user_prose: bool,
    nudge_sent: bool,
) -> str:
    """Choose whether a completed synchronous swarm needs one final synthesis."""
    if not synchronous_swarms or step_emitted_user_prose:
        return "none"
    return "fallback" if nudge_sent else "nudge"


def _synthesis_only_turn(
    active: bool, turn: Any, tool_calls: list,
) -> tuple[Any, list]:
    """Strip provider tool calls from the hidden synthesis retry."""
    if not active:
        return turn, tool_calls
    turn.actions = []
    return turn, []


def _build_step_tools(synthesis_nudge_active: bool, build_tools: Any) -> list:
    """Hide visible tools during the tool-free synthesis retry."""
    return [] if synthesis_nudge_active else build_tools()


def emit_turn_task_profile(session: Any, user_message: str) -> Iterator[Any]:
    """Classify adaptive depth for a fresh user turn and emit a ConvEvent."""
    from .conversation import ConvEvent
    from .send_loop_phases import begin_turn_task_kernel

    begin_turn_task_kernel(session, user_message)
    try:
        profile = session._resolve_task_profile_for_turn(user_message)
        yield ConvEvent("task_profile", {
            "profile": profile,
            "source": getattr(session, "_task_profile_source", "") or "",
            "escalated_from": getattr(session, "_task_profile_escalated_from", None),
        })
    except Exception:
        session._task_profile = "STANDARD"
        session._task_profile_source = "heuristic"
        session._task_profile_escalated_from = None


def profile_skips_auto_inject(session: Any) -> tuple[bool, bool]:
    """MICRO skips wiki/CodeGraph auto-inject. Best-effort; never raises."""
    try:
        from .task_profile import profile_skips_codegraph, profile_skips_wiki

        profile = getattr(session, "_task_profile", "") or ""
        return profile_skips_codegraph(profile), profile_skips_wiki(profile)
    except Exception:
        return False, False


# After emergency compact, a remaining "overflow" on a tiny history is almost
# always a misclassified provider 400 (completion max_tokens), not a window blow.
OVERFLOW_RETRY_SMALL_CONTEXT_TOKENS = 16_000


def format_overflow_persist_error(
    raw: str,
    estimated_tokens: int,
    humanize: Any,
) -> str:
    """Humanize a second overflow after emergency compact. Never the raw persist string."""
    text = str(raw or "").strip() or "the provider rejected the compacted turn"
    if int(estimated_tokens or 0) < OVERFLOW_RETRY_SMALL_CONTEXT_TOKENS:
        try:
            from harness.api.redaction import redact_api_secrets

            tail = str(redact_api_secrets(text) or text)[:160]
        except Exception:
            tail = text[:160]
        return (
            "pilot: the provider rejected this turn after history was "
            "compacted, and the remaining context is small -- this is "
            "not a full context window. Try another model, or send again."
            + (f" [provider said: {tail}]" if tail else "")
        )
    try:
        return str(humanize(text) or text)
    except Exception:
        return (
            "pilot: this turn still exceeded the model's context window "
            "after compaction. Start a fresh session or pick a "
            "longer-context model."
        )


class SendLoopMixin:
    """Mixin holding send-loop orchestration for ConversationalSession.

    The concrete class supplies the state these methods read/write via `self`.
    This mixin defines no __init__ and no instance state of its own.
    """

    def _is_correction(self, text: str) -> bool:
        t = text.lower()
        patterns = ["no,", "don't", "dont", "stop", "actually", "wrong", "not like that", "should be", "instead"]
        for p in patterns:
            if p in t:
                return True
        if getattr(self, "_total_tool_calls", 0) > 0:
            action_patterns = ["fix", "correct", "incorrect", "error", "failed", "bug", "mistake", "change"]
            for ap in action_patterns:
                if ap in t:
                    return True
        return False

    @staticmethod
    def _settle_turn_native_tool_prep_cards(
        cards: dict,
        *,
        complete: bool,
        error: str = "cancelled",
    ) -> None:
        """Settle still-null Cursor-native prep cards owned by this send().

        Only the exact card objects tracked for the current turn are touched —
        older / background / unrelated ``call_id`` rows are left alone.
        """
        for card in list(cards.values()):
            if not isinstance(card, dict) or card.get("result") is not None:
                continue
            if complete:
                card["result"] = {"status": "complete"}
            else:
                card["result"] = {"status": "interrupted", "error": error}
        cards.clear()

    @staticmethod
    def _current_turn_display_start(display: list) -> int:
        """Index after the latest user message — current-turn card lookup bound."""
        for i in range(len(display) - 1, -1, -1):
            row = display[i]
            if (
                isinstance(row, dict)
                and row.get("type") == "message"
                and row.get("role") == "user"
            ):
                return i + 1
        return 0

    def _persist_cursor_tool_prep(self, data: dict) -> Optional[dict]:
        """Persist Cursor-native tool_prep into ``_display_transcript``.

        Cursor CLI/ACP tools never emit Marionette ``action_start`` /
        ``action_result``. Without this, reload drops investigation rows and
        the Explored group can reappear after final prose. Stable ``call_id``
        is the only correlation key — anonymous kind-only hints are skipped.
        Matching is confined to the current user turn so older / background
        ``call_id`` rows are never rewritten.

        Returns the card dict created or updated, or None when skipped.
        """
        if not isinstance(data, dict):
            return None
        call_id = str(data.get("id") or "").strip()
        if not call_id:
            return None
        kind = str(data.get("name") or "tool_call").strip() or "tool_call"
        goal = str(data.get("goal") or "").strip()
        status = str(data.get("status") or "").strip().lower()
        terminal = status in ("completed", "failed", "cancelled", "canceled", "error")
        display = getattr(self, "_display_transcript", None)
        if not isinstance(display, list):
            return None

        existing = None
        turn_start = self._current_turn_display_start(display)
        for row in display[turn_start:]:
            if not isinstance(row, dict) or row.get("type") != "card":
                continue
            row_call = str(row.get("call_id") or "").strip()
            row_id = str(row.get("id") or "").strip()
            if row_call == call_id or row_id == call_id or row_id == f"tool-prep:{call_id}":
                existing = row
                break

        if existing is not None:
            if kind and kind != "tool_call":
                existing["kind"] = kind
            if goal:
                existing["goal"] = goal
            existing["call_id"] = call_id
            if terminal:
                if status in ("failed", "error", "cancelled", "canceled"):
                    existing["result"] = {
                        "status": "failed" if status in ("failed", "error") else "interrupted",
                        "error": status if status in ("failed", "error") else "cancelled",
                    }
                else:
                    prior = existing.get("result") if isinstance(existing.get("result"), dict) else {}
                    existing["result"] = {**(prior or {}), "status": "complete"}
            return existing

        card = {
            "type": "card",
            "id": call_id,
            "kind": kind,
            "goal": goal,
            "call_id": call_id,
            "result": None,
        }
        if terminal:
            if status in ("failed", "error", "cancelled", "canceled"):
                card["result"] = {
                    "status": "failed" if status in ("failed", "error") else "interrupted",
                    "error": status if status in ("failed", "error") else "cancelled",
                }
            else:
                card["result"] = {"status": "complete"}

        # Late first card (started or completed): slot immediately before the
        # rightmost assistant of this turn. Cursor CLI often flushes final prose
        # before buffered tool_call events — appending after that readout puts
        # Explored under the summary. Multiple late call_ids stack in arrival
        # order before that final message; pre-tool narration stays above.
        insert_at = len(display)
        for i in range(len(display) - 1, turn_start - 1, -1):
            row = display[i]
            if not isinstance(row, dict):
                continue
            if row.get("type") == "message" and row.get("role") == "user":
                break
            if row.get("type") == "message" and row.get("role") == "assistant":
                insert_at = i
                break
        display.insert(insert_at, card)
        return card

    def send(self, user_message: str, images: Optional[list] = None, plan: bool = False, resume: bool = False) -> Iterator[ConvEvent]:
        """Process one user message: drive the pilot loop until it yields back.

        ``resume=True`` is the keep-alive continuation path: a background swarm
        finished and ``drain_swarm_results`` already appended the result record
        plus a user-role continuation to history. We generate off that existing
        history WITHOUT appending a new user turn, so the pilot autonomously
        assesses the result and takes the next step -- no new user message and no
        autopilot required.
        """
        from .conversation import ConvEvent
        # Keep-alive must not restart a turn the user just stopped. Real user /
        # autopilot sends clear the Stop hold in _mark_busy_acquired once they
        # own the lock.
        if resume and (
            getattr(self, "_stop_holds_idle", False)
            or getattr(self, "_interrupted_swarms", False)
        ):
            return
        self._cancel.clear()
        self._pending_advisor_warnings = []
        if not self._busy.acquire(blocking=False):
            # The lock is held. Normally that means a turn is genuinely streaming.
            # But if a previous turn's generator was never closed (hard crash /
            # abandoned stream), the lock LEAKS and the pilot looks dead forever.
            # Detect a stale lock -- held with no live stream for too long -- and
            # forcibly recover it so the user isn't permanently wedged.
            import time as _t
            held_for = _t.monotonic() - self._busy_since if self._busy_since else 0.0
            stale = self._busy_since and held_for > 1.5 and self._state == "idle"
            # If the user EXPLICITLY interrupted the previous turn, recover the
            # lock even when _state is still 'executing' (the abandoned turn is
            # blocked in a subprocess/tool and may never reach its finally). A
            # shorter grace here is safe because the user asked to stop -- this is
            # the "stop a chat right as it runs tool calls" case that wrongly
            # errored 'session busy'.
            if not stale and self._interrupt_requested and self._busy_since and held_for > 0.5:
                stale = True
            # Hard deadline reaper (also used by swarm drain) — Cursor CLI/ACP
            # hangs leave state=thinking, so the idle 1.5s path never fires and
            # follow-up prompts looked permanently ignored.
            if not stale:
                try:
                    if self._reap_stuck_turn():
                        stale = True
                except Exception as exc:
                    try:
                        from harness.diag import note as _diag_note
                        _diag_note("send_loop.reap_stuck_turn", exc)
                    except Exception:
                        pass
            # Shorter send-path recovery for wedged thinking/executing turns
            # (default 180s). Without this, users wait the full 600s reap.
            # Intentional race window: the interrupted turn's generator/finally may
            # still unwind on another thread after we release. Prefer recover-over-
            # wedge; _busy_gen below makes the old finally's release a no-op.
            if not stale and self._busy_since and self._state in (
                "thinking", "executing", "streaming",
            ):
                try:
                    send_stale_s = float(
                        os.environ.get("HARNESS_SEND_STALE_SECONDS", "180").strip()
                        or 180
                    )
                except ValueError:
                    send_stale_s = 180.0
                if send_stale_s > 0 and held_for > send_stale_s:
                    try:
                        pilot = getattr(self, "pilot", None)
                        on_int = getattr(pilot, "on_interrupt", None)
                        if callable(on_int):
                            on_int()
                    except Exception as exc:
                        try:
                            from harness.diag import note as _diag_note
                            _diag_note("send_loop.on_interrupt_stale", exc)
                        except Exception:
                            pass
                    # Always force-unwedge after the send-stale window even when
                    # on_interrupt raises — prefer recover-over-wedge.
                    stale = True
            if stale:
                self._interrupt_requested = False
                # Advance the generation as we force-release so the leaked holder's
                # own finally (if it ever runs) treats its release as a no-op and
                # cannot free the lock this new turn is about to take.
                with self._busy_meta:
                    self._busy_gen += 1
                    self._busy_since = 0.0
                    try:
                        self._busy.release()
                    except RuntimeError:
                        pass
                if not self._busy.acquire(blocking=False):
                    yield ConvEvent("error", {"error": "session busy: another request is in flight"})
                    return
            else:
                yield ConvEvent("error", {"error": "session busy: another request is in flight"})
                return
        busy_gen = self._mark_busy_acquired()
        # Stream any Stop-boundary honesty notices recorded by interrupt or
        # post-Stop late-steer cleanup.
        yield from self._yield_stop_boundary_notices()
        # Time-travel journal (round 6): snapshot the active check specs and
        # behavior toggles for this turn. Observability only; never raises.
        try:
            from .turn_context import record_turn_context
            from .memory_layers import (
                record_memory_layer_snapshot,
                snapshot_memory_layers,
            )

            _turn_index = sum(
                1 for m in self._history if m.get("role") == "user"
            ) + (0 if resume else 1)
            record_turn_context(
                self.state_dir,
                self.harness_session_id or "default",
                _turn_index,
                repo=self.config.repo or "",
            )
            record_memory_layer_snapshot(
                self.state_dir,
                self.harness_session_id or "default",
                _turn_index,
                snapshot_memory_layers(
                    self,
                    self.state_dir,
                    self.harness_session_id or "default",
                    repo=self.config.repo or "",
                ),
            )
        except Exception:
            pass
        if not resume and self._is_correction(user_message):
            self._corrections.append(user_message)
        original_sys = self._history[0]["content"]
        # Plan mode must NOT mutate the system prefix (busts prompt cache for
        # every provider under append-only). PLAN_SYSTEM_SUFFIX rides on the
        # user turn in _send_locked_inner instead; action filtering still uses
        # the plan= flag.
        pending_cards: dict = {}
        # Cursor-native tool_prep cards created/updated during THIS send() only.
        # Settled on assistant_done (complete) or cancel/error finally (interrupted).
        turn_native_prep_cards: dict = {}
        native_prep_settled_at_done = False
        try:
            import time
            action_starts = {}
            for ev in self._send_locked(user_message, images=images, plan=plan, resume=resume):
                if ev.kind == "tool_prep":
                    # Cursor-native tools (CLI/ACP) emit tool_prep only — persist
                    # by stable call_id so reload keeps chronological slots.
                    touched = self._persist_cursor_tool_prep(ev.data or {})
                    if isinstance(touched, dict):
                        cid = str(touched.get("call_id") or "").strip()
                        if cid:
                            turn_native_prep_cards[cid] = touched
                elif ev.kind == "action_start":
                    self._total_tool_calls += 1
                    aid = ev.data.get("id")
                    if aid:
                        action_starts[aid] = time.time()
                        goals = ev.data.get("goals")
                        if not isinstance(goals, list):
                            goals = None
                        call_id = str(ev.data.get("call_id") or "").strip()
                        if not call_id and (
                            not str(aid).startswith("a")
                            or (len(str(aid)) > 1 and not str(aid)[1:].isdigit())
                        ):
                            # Stable provider ids double as call_id for prep promotion.
                            call_id = str(aid)
                        # Promote a prior tool_prep row with the same call_id in
                        # place — never append a duplicate after Cursor-native
                        # persistence (or a streamed prep hint). Only the current
                        # user turn is searched so older call_id rows stay put.
                        existing = None
                        if call_id:
                            turn_start = self._current_turn_display_start(
                                self._display_transcript,
                            )
                            for row in self._display_transcript[turn_start:]:
                                if not isinstance(row, dict) or row.get("type") != "card":
                                    continue
                                row_call = str(row.get("call_id") or "").strip()
                                row_id = str(row.get("id") or "").strip()
                                if (
                                    row_call == call_id
                                    or row_id == call_id
                                    or row_id == f"tool-prep:{call_id}"
                                ):
                                    existing = row
                                    break
                        if existing is not None:
                            existing["id"] = aid
                            existing["kind"] = ev.data.get("kind") or existing.get("kind")
                            if ev.data.get("goal"):
                                existing["goal"] = ev.data.get("goal")
                            if ev.data.get("cwd") is not None:
                                existing["cwd"] = ev.data.get("cwd")
                            if goals is not None:
                                existing["goals"] = [
                                    str(g) for g in goals if str(g or "").strip()
                                ]
                            if call_id:
                                existing["call_id"] = call_id
                            existing["result"] = None
                            pending_cards[aid] = existing
                            # Marionette action lifecycle owns this card now.
                            if call_id:
                                turn_native_prep_cards.pop(call_id, None)
                        else:
                            card = {
                                "type": "card",
                                "id": aid,
                                "kind": ev.data.get("kind"),
                                "goal": ev.data.get("goal"),
                                "cwd": ev.data.get("cwd"),
                                # None = still running. Append immediately so session
                                # transcript polls / reattach see the tool row instead
                                # of wiping the live Investigating UI mid-command.
                                "result": None,
                            }
                            if goals is not None:
                                card["goals"] = [
                                    str(g) for g in goals if str(g or "").strip()
                                ]
                            if call_id:
                                card["call_id"] = call_id
                            pending_cards[aid] = card
                            self._display_transcript.append(card)
                elif ev.kind == "action_result":
                    aid = ev.data.get("id")
                    if aid and aid in action_starts:
                        duration_ms = int((time.time() - action_starts[aid]) * 1000)
                        ev.data["duration_ms"] = duration_ms
                    # Enrich sparse results so ring-miss / missing-start clients
                    # can still create a durable card with kind/goal/status.
                    if aid and aid in pending_cards:
                        prior = pending_cards[aid]
                        if not ev.data.get("kind") and prior.get("kind"):
                            ev.data["kind"] = prior.get("kind")
                        if not ev.data.get("goal") and prior.get("goal"):
                            ev.data["goal"] = prior.get("goal")
                        if prior.get("goals") and not ev.data.get("goals"):
                            ev.data["goals"] = list(prior.get("goals") or [])
                        if prior.get("call_id") and not ev.data.get("call_id"):
                            ev.data["call_id"] = prior.get("call_id")
                        if prior.get("cwd") and not ev.data.get("cwd"):
                            ev.data["cwd"] = prior.get("cwd")
                    # Advisor warnings (round 6): surface once, on the first
                    # action_result after the advisor ran. Advisory only.
                    pending_warnings = getattr(self, "_pending_advisor_warnings", None)
                    if pending_warnings:
                        ev.data["advisor_warnings"] = list(pending_warnings)
                        self._pending_advisor_warnings = []
                    if ev.data.get("error"):
                        self._has_tool_failure = True
                    else:
                        if getattr(self, "_has_tool_failure", False):
                            self._error_then_recovery_seen = True

                    if aid and aid in pending_cards:
                        card = pending_cards[aid]
                        res_data = {}
                        for key in _ACTION_RESULT_DISPLAY_FIELDS:
                            if key in ev.data:
                                res_data[key] = ev.data[key]
                        # In-place update of the action_start row (already in display).
                        card["result"] = res_data
                        del pending_cards[aid]
                    elif aid:
                        # Result without a tracked start -- still persist a card.
                        res_data = {}
                        for key in _ACTION_RESULT_DISPLAY_FIELDS:
                            if key in ev.data:
                                res_data[key] = ev.data[key]
                        card = {
                            "type": "card",
                            "id": aid,
                            "kind": ev.data.get("kind"),
                            "goal": ev.data.get("goal"),
                            "cwd": ev.data.get("cwd"),
                            "result": res_data,
                        }
                        goals = ev.data.get("goals")
                        if isinstance(goals, list):
                            card["goals"] = [str(g) for g in goals if str(g or "").strip()]
                        call_id = str(ev.data.get("call_id") or "").strip()
                        if call_id:
                            card["call_id"] = call_id
                        self._display_transcript.append(card)

                if ev.kind == "assistant_done":
                    self._turn_count += 1
                    # Settle leftover result:null display cards owned by this
                    # turn so reload/export cannot resurrect forever-spinning
                    # rows after a missing action_result. Background jobs that
                    # already received a dispatch ack are not in pending_cards.
                    for _aid, _card in list(pending_cards.items()):
                        if isinstance(_card, dict) and _card.get("result") is None:
                            _card["result"] = {
                                "status": "interrupted",
                                "error": "missing action_result",
                            }
                        pending_cards.pop(_aid, None)
                    # Cursor-native prep cards from THIS turn only (no whole-
                    # transcript sweep — leave older/background call_ids alone).
                    self._settle_turn_native_tool_prep_cards(
                        turn_native_prep_cards, complete=True,
                    )
                    native_prep_settled_at_done = True
                    # Emit assistant_done first so the UI paints the final answer
                    # before any non-blocking Save/Skip cards.
                    yield ev
                    if self._auto_mode:
                        # Full-auto: never propose memory (no human to Save/Skip).
                        self._turn_memory_queue.clear()
                        try:
                            self._turn_refine_queue.clear()
                        except Exception:
                            pass
                        # Full-auto mode: run synchronously to ensure sequential consistency
                        if self._auto_distill:
                            d = self._maybe_auto_distill()
                            if d:
                                yield ConvEvent("distilled", d)
                        if self._wiki_orchestrate:
                            try:
                                w = self.prepare_wiki_pages()
                                if w and w.get("status") == "prepared" and w.get("pages"):
                                    yield ConvEvent("wiki_prepared", w)
                            except Exception:
                                pass
                        # Autopilot keeps verify_cmd; quality gate only if opted in.
                        try:
                            from .quality_gate import maybe_run_quality_gates

                            gate_result = maybe_run_quality_gates(
                                self, auto_mode=True,
                            )
                            if gate_result is not None and gate_result.outcome != "disabled":
                                yield ConvEvent("quality_gate", gate_result.event_data())
                        except Exception:
                            pass
                    else:
                        # Interactive: emit non-blocking memory Save/Skip cards
                        # AFTER the final answer (never mid-tool-loop).
                        for prop in self._flush_turn_memory_proposals():
                            yield ConvEvent("memory_propose", prop)
                        # Continual harness refine cards (supplemental only).
                        try:
                            from .harness_refine import get_refine_controller

                            for prop in get_refine_controller(self).flush_queued():
                                yield ConvEvent("refine_propose", prop)
                        except Exception:
                            pass
                        # Host quality GATE before settle-to-idle (blocks finish on fail).
                        gate_blocks_idle = False
                        try:
                            from .quality_gate import (
                                gate_retry_prompt,
                                maybe_run_quality_gates,
                            )

                            gate_result = maybe_run_quality_gates(
                                self, auto_mode=False,
                            )
                            if gate_result is not None and gate_result.outcome != "disabled":
                                yield ConvEvent("quality_gate", gate_result.event_data())
                                if gate_result.block_finish and gate_result.outcome in (
                                    "failed", "skipped_unchanged",
                                ):
                                    gate_blocks_idle = True
                                    nudge = gate_retry_prompt(gate_result)
                                    if nudge and hasattr(self, "enqueue_prompt"):
                                        try:
                                            self.enqueue_prompt(nudge)
                                        except Exception:
                                            pass
                                elif gate_result.outcome == "budget_halt":
                                    gate_blocks_idle = True
                        except Exception:
                            gate_blocks_idle = False
                        # Sticky session goal: record turn usage. Optional gentle
                        # continuation is host-gated (goal_auto_continue) so an
                        # active goal cannot drain-loop the prompt queue forever.
                        try:
                            goal = getattr(self, "_session_goal", None)
                            if goal is not None and goal.is_active():
                                tokens = int(
                                    getattr(self, "_last_turn_tokens", 0)
                                    or getattr(self, "_turn_output_tokens", 0)
                                    or 0
                                )
                                goal.record_turn_usage(tokens=tokens)
                                self._persist_session_goal()
                                auto_continue = bool(
                                    getattr(self.config, "goal_auto_continue", False)
                                )
                                if (
                                    auto_continue
                                    and not gate_blocks_idle
                                    and hasattr(self, "enqueue_goal_continuation")
                                ):
                                    self.enqueue_goal_continuation()
                        except Exception:
                            pass
                        # Interactive mode: background the work to keep the UI
                        # responsive. Use housekeeping (not _submit_swarm) so
                        # distill/wiki never flip runners=running / Still working
                        # after assistant_done.
                        if self._auto_distill or self._wiki_orchestrate:
                            if not self._submit_housekeeping(
                                self._run_distill_and_wiki_background, user_message
                            ):
                                yield ConvEvent("notice", {
                                    "message": (
                                        "Could not start background distill/wiki "
                                        "this turn (best-effort)."
                                    )
                                })
                else:
                    yield ev
        finally:
            # Cancel/error unwind (or any path that skipped assistant_done): settle
            # owned result:null cards so export/reload cannot resurrect spinners.
            for _aid, _card in list(pending_cards.items()):
                if isinstance(_card, dict) and _card.get("result") is None:
                    _card["result"] = {
                        "status": "interrupted",
                        "error": "missing action_result",
                    }
                pending_cards.pop(_aid, None)
            # Cancel/error before assistant_done: interrupt still-null current-
            # turn native prep only. Never re-settle after a successful done.
            if not native_prep_settled_at_done:
                self._settle_turn_native_tool_prep_cards(
                    turn_native_prep_cards, complete=False, error="cancelled",
                )
            else:
                turn_native_prep_cards.clear()
            # Append-only freezes an enriched system prompt (MCP catalog, pilot
            # identity, …). Restoring the pre-turn base would desync history
            # from the frozen prefix and break prompt.startswith stability.
            if self._resolve_append_only() and self._frozen_system_prompt is not None:
                self._history[0]["content"] = self._frozen_system_prompt
            else:
                self._history[0]["content"] = original_sys
            self._release_busy(busy_gen)

    def _send_locked(self, user_message: str, images: Optional[list] = None, plan: bool = False, resume: bool = False) -> Iterator[ConvEvent]:
        from .conversation import ConvEvent
        self._state = "thinking"
        try:
            yield from self._send_locked_inner(user_message, images=images, plan=plan, resume=resume)
        finally:
            self._state = "idle"

    def _get_codegraph_context(self, query: str) -> str:
        """Build a relevance-ranked CodeGraph context block for ``query``.

        Shells out to ``python -m puppetmaster codegraph search <query>`` (same
        interpreter, cwd = the open repo), parses ``path:line`` hit locations,
        reads a small +/-8 line source window for the top hits, and returns a
        single <codegraph-context> ... </codegraph-context> block. Returns "" on
        any failure or when there are no hits. Fully exception-guarded: this
        NEVER raises into the pilot loop and degrades to a pure no-op.
        """
        MAX_HITS = 5
        WINDOW = 8
        MAX_BYTES = 4096
        repo = getattr(self.config, "repo", None)
        if not repo or not query or not query.strip():
            return ""
        from harness.context_budget import truncate_bytes
        try:
            cmd = [sys.executable, "-m", "puppetmaster", "codegraph", "search", query]
            p = subprocess.run(
                cmd,
                cwd=repo,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                timeout=15,
            )
            if p.returncode != 0:
                return ""
            output = _strip_ansi((p.stdout or ""))
        except Exception:
            return ""

        # Parse "path:line" hit locations (first two colon-separated fields where
        # the second is an integer line number). Dedupe, preserve rank order.
        hit_re = re.compile(r"([^\s:]+):(\d+)")
        seen: set = set()
        hits: list[tuple[str, int]] = []
        for line in output.splitlines():
            m = hit_re.search(line)
            if not m:
                continue
            path, lineno = m.group(1), int(m.group(2))
            key = (path, lineno)
            if key in seen:
                continue
            seen.add(key)
            hits.append((path, lineno))
            if len(hits) >= MAX_HITS:
                break
        if not hits:
            return ""

        blocks: list[str] = []
        for path, lineno in hits:
            try:
                abs_path = path if os.path.isabs(path) else os.path.join(repo, path)
                if not is_safe_path(abs_path, repo):
                    continue
                with open(abs_path, "r", encoding="utf-8", errors="replace") as fh:
                    lines = fh.readlines()
            except Exception:
                continue
            start = max(0, lineno - 1 - WINDOW)
            end = min(len(lines), lineno + WINDOW)
            snippet = "".join(lines[start:end]).rstrip("\n")
            blocks.append(f"# {path}:{lineno}\n{snippet}")

        if not blocks:
            return ""
        body = "\n\n".join(blocks)
        body = truncate_bytes(body, MAX_BYTES)
        return f"<codegraph-context>\n{body}\n</codegraph-context>"

    def _yield_stop_boundary_notices(self) -> Iterator[ConvEvent]:
        """Stream Stop-boundary honesty notices (steer drop + owned-command orphan).

        Prefers ``_flush_stop_boundary_notices`` when BusyControl is mixed in;
        falls back to steer-only flush for partial mixin compositions in tests.
        """
        flush = getattr(self, "_flush_stop_boundary_notices", None)
        if callable(flush):
            yield from flush()
            return
        flush_steer = getattr(self, "_flush_steer_drop_notice", None)
        if callable(flush_steer):
            yield from flush_steer()

    def _send_locked_inner(self, user_message: str, images: Optional[list] = None, plan: bool = False, resume: bool = False) -> Iterator[ConvEvent]:
        from .conversation import (
            ConvEvent,
            _format_mcp_tools_section,
            _hard_pilot_steps,
            _prewarm_worker_imports,
        )
        timing = make_stream_timing_accumulator()
        if resume:
            # Keep-alive continuation: drain_swarm_results already appended the
            # result record + a user-role continuation. Generate off that history
            # WITHOUT appending anything. If the last turn is not a user message
            # there is nothing to respond to -- bail cleanly so a stray resume
            # trigger never fabricates an empty turn.
            if not (self._history and self._history[-1].get("role") == "user"):
                return
        else:
            # Native multimodal vs sidecar transcription; abort if images unusable.
            image_prep = yield from yield_timed_phase(
                timing, "image_prep",
                prepare_turn_images(self, user_message, images),
            )
            if image_prep is None:
                return
            processed_message, native_image_paths = image_prep

            self._turn_output_tokens = 0
            self._turn_budget = None
            # Fresh turn: clear guard / stagnation / failed-objective resume state.
            self._turn_guard_state = None
            reset_repeat_chain(self)
            self._stagnation_last_prose = None
            self._stagnation_last_actions = None
            self._stagnation_streak = 0
            self._invalid_only_streak = 0
            self._failed_objective_resume_counts = {}
            self._keep_alive_waits = 0
            yield from yield_timed_phase(
                timing, "task_profile",
                emit_turn_task_profile(self, user_message),
            )
            try:
                from .turn_budget import turn_budget_enabled

                if turn_budget_enabled():
                    self._turn_budget = self._turn_economy.parse_output_directive(
                        user_message
                    )
            except Exception:
                pass

            image_encode_error = None
            # Exact order: user content, append-only trailer, plan suffix,
            # then native image encode/append. user_append_ms is additive
            # around trailer + encode/append; suffix stays outside the clock.
            if self._resolve_append_only():
                with timed_phase(timing, "user_append"):
                    processed_message = self._append_turn_context_trailer(
                        processed_message, user_message
                    )

            if plan:
                from .pilot import PLAN_SYSTEM_SUFFIX
                processed_message = (
                    processed_message.rstrip() + "\n\n" + PLAN_SYSTEM_SUFFIX
                )

            with timed_phase(timing, "user_append"):
                if native_image_paths:
                    from .vision import native_multimodal_user_content
                    try:
                        history_content = native_multimodal_user_content(
                            processed_message, native_image_paths,
                        )
                    except Exception as e:
                        image_encode_error = e
                        history_content = None
                else:
                    history_content = processed_message

                # Preserve strict user/assistant alternation in _history: if the last
                # message is already a user turn (e.g. a background job just drained a
                # pilot-resume continuation before the user typed), merge into it rather
                # than appending a second adjacent user message, which some chat APIs
                # (Anthropic) reject and the concurrency stress test forbids.
                if history_content is not None:
                    if self._history and self._history[-1].get("role") == "user":
                        from .vision import merge_user_contents
                        self._history[-1]["content"] = merge_user_contents(
                            self._history[-1].get("content"), history_content,
                        )
                    else:
                        self._history.append({"role": "user", "content": history_content})
                    self._display_transcript.append({"type": "message", "role": "user", "text": user_message})

            if image_encode_error is not None:
                yield ConvEvent("error", {
                    "error": f"Failed to load attached image(s): {image_encode_error}",
                })
                return

            # Inject relevance-ranked CodeGraph context (best-effort, exception-guarded)
            # so the driver sees the most relevant code BEFORE it starts calling tools.
            # Skip for no_delegation worker sessions (they run in a fresh worktree with
            # no CodeGraph index). Degrades to a no-op when codegraph is unavailable.
            _skip_cg, _ = profile_skips_auto_inject(self)
            if (
                not getattr(self.config, "no_delegation", False)
                and not self._resolve_append_only()
                and not _skip_cg
            ):
                with timed_phase(timing, "auto_codegraph"):
                    cg_context = self._get_codegraph_context(user_message)
                    if cg_context:
                        self._history.append({"role": "user", "content": cg_context})

        swarms = 0
        synchronous_swarms = 0
        action_seq = 0
        demo_swarms = 0  # count swarms that returned the demo substrate
        turn_findings: list = []   # accumulate real findings for wiki ingest
        turn_prose: list = []      # accumulate pilot prose for the digest
        post_swarm_nudge_sent = False
        post_swarm_nudge_active = False

        consecutive_non_productive = 0
        loop_exit_cause = None
        last_classified = None
        # AUTO-VERIFY LOOP: after a turn that edited files, run a fast, scoped
        # project check and feed a FAILURE back as a tool observation IN THE SAME
        # user message so the pilot can self-correct. Bounded per user message so
        # it cannot loop forever.
        auto_verify_iters = 0
        try:
            _auto_verify_cap = int(os.environ.get("HARNESS_AUTO_VERIFY_MAX", "2"))
        except ValueError:
            _auto_verify_cap = 2
        # Step ceiling per user message, read LIVE from the env each turn so a
        # Settings change applies without a restart. 0 (or negative) means
        # UNLIMITED -- true autopilot: loop until the pilot is done, the budget
        # governor halts it, or the user stops it. Otherwise cap at 2x the
        # configured pilot-step budget.
        import itertools as _itertools
        _hard_steps = _hard_pilot_steps()
        try:
            _pilot_steps = int(os.environ.get("HARNESS_MAX_PILOT_STEPS", str(_hard_steps)))
        except ValueError:
            _pilot_steps = _hard_steps
        if _pilot_steps <= 0:
            _step_iter = _itertools.count()
            max_steps = 0  # 0 == unlimited (used by the limit message below)
        else:
            max_steps = 2 * _pilot_steps
            _step_iter = range(max_steps)

        # Advisory compaction once per user turn (after the new user message is
        # in history), NOT at the start of every tool-loop step. Mid-turn
        # history rewrites bust prefix cache for all providers. CONTEXT_OVERFLOW
        # still force-compacts inside the step loop as a last resort.
        yield from yield_timed_phase(
            timing, "advisory_compaction",
            self._maybe_compact_history(),
        )

        for step in _step_iter:
            # Heal dangling tool pairs from a prior mid-spree abandon (steer/
            # cancel) BEFORE the cancel check so an interrupt never freezes
            # invalid history into the next request / export.
            reset_timing_before_step(timing, step)
            self._sanitize_tool_pairs()
            if self._cancel.is_set():
                yield from self._yield_stop_boundary_notices()
                yield ConvEvent("interrupted", {"reason": "session interrupted"})
                return

            # Consume any pending steer at the start of the step: it's now in
            # history and the model will see it this iteration, so clear the flag.
            # (_check_and_inject_steer itself refuses inject after Stop.)
            self._steer_pending = False
            yield from self._check_and_inject_steer()
            self._steer_pending = False

            # 1. Ask the pilot for its next conversational turn.
            step_emitted_user_prose = False
            synthesis_nudge_active = post_swarm_nudge_active
            post_swarm_nudge_active = False
            base_sys = self._history[0]["content"]
            cg_section = ""
            # Skip the per-turn CodeGraph context build for no_delegation worker sessions:
            # a worker runs in a fresh git worktree with NO .codegraph index, so this call
            # blocks on a 30s timeout EVERY turn and returns nothing -- it was ~93% of worker
            # wall-time. Workers edit directly and do not use codegraph (it is also excluded
            # from their toolset), so skipping it is pure win.
            _no_deleg = getattr(self.config, "no_delegation", False)
            cg_symbol_count = 0
            append_only = self._resolve_append_only()
            _skip_cg, _skip_wiki = profile_skips_auto_inject(self)
            cg_event = None
            if self.config.repo and not _no_deleg and not append_only and not _skip_cg:
                with timed_phase(timing, "step_codegraph"):
                    # Cache the CodeGraph slice per user message: the underlying
                    # codegraph_context() is a blocking Node subprocess (~270-500ms).
                    # Recomputing it on every step of a multi-step turn (identical
                    # query) just stacks dead time in front of the model. Compute it
                    # once on the first step, reuse it for the rest of this turn.
                    if self._cg_cache_key == user_message:
                        cg_section = self._cg_cache_section
                        cg_symbol_count = self._cg_cache_symbols
                    else:
                        try:
                            from puppetmaster.codegraph import codegraph_context, codegraph_prompt_section
                            cg_slice = codegraph_context(task=user_message, cwd=self.config.repo)
                            if cg_slice:
                                # Count located symbols (entry points + related symbols) so the
                                # UI can show that CodeGraph was consulted this turn.
                                cg_symbol_count = cg_slice.count("- **") + cg_slice.count("#### ")
                                # Prepend an AUTHORITATIVE directive so the model leans on the
                                # already-injected CodeGraph slice instead of redundantly raw-reading
                                # whole files (qwen tends to dump files even with context present).
                                authoritative = (
                                    "CODEGRAPH HAS ALREADY BEEN QUERIED FOR THIS TASK. The relevant "
                                    "symbols, definitions, and code are provided in the section below. "
                                    "USE THIS as your primary source. Do NOT re-read entire files that "
                                    "already appear here -- only read_file specific additional lines you "
                                    "still need (with start_line + limit), or call search_codegraph to "
                                    "widen the graph. Whole-file dumps when the answer is already below "
                                    "are wasteful and wrong.\n"
                                )
                                cg_section = authoritative + codegraph_prompt_section(cg_slice)
                            # Cache the result (even an empty slice) so we never re-run
                            # the subprocess for the same message this turn.
                            self._cg_cache_key = user_message
                            self._cg_cache_section = cg_section
                            self._cg_cache_symbols = cg_symbol_count
                            # Visibility: tell the UI CodeGraph was consulted -- only on
                            # the first compute, so the chip shows once per turn.
                            if cg_section and not _no_deleg:
                                cg_event = {
                                    "symbols": cg_symbol_count,
                                    "query": (user_message or "")[:120],
                                }
                        except Exception:
                            pass
            if cg_event is not None:
                yield ConvEvent("codegraph_context", cg_event)

            wiki_section = ""
            if self._wiki.configured and not append_only and not _skip_wiki:
                with timed_phase(timing, "step_wiki"):
                    if self._wiki_cache_key == user_message:
                        wiki_section = self._wiki_cache_section
                    else:
                        wiki_section = self._build_turn_wiki_section(user_message)
            vault_section, vault_cite = self._turn_vault_context(
                user_message, append_only
            )
            if vault_cite is not None:
                yield ConvEvent("vault_cite", vault_cite)

            resp = None
            self._streamed_prose = ""  # reset per step; set if this step streams
            for attempt in range(2):
                # Sanitize BEFORE rendering/dispatch so both chat() and
                # complete() see healed tool_use/tool_result pairs. (A prior
                # interrupted spree — cancel/steer/worker-ceiling/exception —
                # otherwise 400s the next provider request.)
                self._sanitize_tool_pairs()
                # prompt_tools_ms is additive: this render block plus
                # dispatch_pilot_provider_call's tool-schema assembly.
                with timed_phase(timing, "prompt_tools"):
                    if append_only:
                        sys_prompt = self._ensure_frozen_system_prompt(base_sys)
                        prompt = self._render_history()
                        self._record_prompt_stability(prompt)
                    else:
                        sys_prompt = base_sys
                        if cg_section:
                            sys_prompt += "\n\n" + cg_section
                        if wiki_section:
                            sys_prompt += "\n\n" + wiki_section
                        if vault_section:
                            sys_prompt += "\n\n" + vault_section
                        mcp_section = _format_mcp_tools_section(
                            self._mcp,
                            self._tool_catalog,
                            no_delegation=getattr(self.config, "no_delegation", False),
                            browser_enabled=getattr(self.config, "browser_enabled", True),
                        )
                        if mcp_section:
                            sys_prompt += "\n\n" + mcp_section
                        turn_note = self._turn_budget_system_note()
                        if turn_note:
                            sys_prompt += "\n\n" + turn_note
                        identity_note = self._pilot_identity_system_note()
                        if identity_note:
                            sys_prompt += "\n\n" + identity_note
                        adapter_note = self._active_adapters_system_note()
                        if adapter_note:
                            sys_prompt += "\n\n" + adapter_note

                        self._history[0]["content"] = sys_prompt
                        prompt = self._render_history()

                try:
                    streamed_prose, resp = yield from dispatch_pilot_provider_call(
                        self,
                        plan=plan,
                        sys_prompt=sys_prompt,
                        prompt=prompt,
                        synthesis_nudge_active=synthesis_nudge_active,
                        accumulator=timing,
                    )
                    self._streamed_prose = streamed_prose
                    step_emitted_user_prose = bool(streamed_prose.strip())
                except Exception as e:
                    # Humanize + redact — never stream raw exception/provider
                    # bodies (may contain URL tokens or key fragments).
                    record_provider_dispatch_error_receipt(
                        self, timing, provider_step=step, provider_attempt=attempt,
                    )
                    try:
                        msg = self._humanize_pilot_error(str(e))
                    except Exception:
                        msg = "pilot: transport failed. Try again."
                    yield ConvEvent("error", {"error": msg})
                    return
                finally:
                    if not append_only:
                        self._history[0]["content"] = base_sys

                account_provider_attempt(
                    self, resp, prompt,
                    provider_step=step, provider_attempt=attempt,
                )

                if resp and resp.error:
                    from pmharness.drivers import error_classifier
                    err_cls = error_classifier.classify(None, resp.error)
                    if err_cls == error_classifier.ErrorClass.CONTEXT_OVERFLOW:
                        if attempt == 0:
                            # Reset this attempt's provider marks only —
                            # keep closed turn-once pre-request durations.
                            reset_provider_step_timing(timing)
                            yield from yield_timed_phase(
                                timing, "advisory_compaction",
                                self._maybe_compact_history(
                                    force=True, emergency=True,
                                ),
                            )
                            continue
                        else:
                            try:
                                est = int(self._estimate_context_tokens() or 0)
                            except Exception:
                                est = 0
                            yield ConvEvent("error", {
                                "error": format_overflow_persist_error(
                                    resp.error or "",
                                    est,
                                    self._humanize_pilot_error,
                                ),
                            })
                            return

                # If there's no error or it is not context overflow, we're done
                break

            if resp and resp.error:
                yield from emit_classified_provider_error(self, resp)
                return

            last_classified, blocked = yield from apply_provider_terminal(self, resp)
            if blocked:
                return

            is_native = False
            tool_calls = []
            reasoning = ""
            pure_content = ""

            if hasattr(self.pilot, "chat"):
                tool_calls = resp.meta.get("tool_calls") or []
                reasoning = resp.meta.get("reasoning") or ""
                pure_content = resp.text or ""

                if tool_calls or reasoning:
                    is_native = True
                elif pure_content:
                    from .pilot import _extract_json_object
                    obj = _extract_json_object(pure_content)
                    if obj and isinstance(obj, dict) and ("say" in obj or "actions" in obj or "thinking" in obj):
                        is_native = False
                    else:
                        is_native = True
                else:
                    is_native = True

            if is_native:
                try:
                    schema = getattr(self, "_step_tools_schema", None)
                    turn, tool_calls, pure_content = parse_native_tool_turn(
                        pure_content, tool_calls, reasoning, schema,
                        session=self,
                    )
                except Exception as e:
                    yield ConvEvent("error", {"error": f"native tool parsing error: {e}"})
                    return
            else:
                try:
                    turn = parse_pilot_turn(resp.text)
                except PilotError as e:
                    if synthesis_nudge_active:
                        from .pilot import PilotTurn
                        turn = PilotTurn(say="", actions=[])
                    else:
                        # One lenient retry: tell the pilot to fix its envelope.
                        self._history.append({"role": "user",
                            "content": f"(system) Your last reply was not valid. {e}. "
                                       f"Reply with the JSON envelope {{\"say\":...,\"actions\":[...]}}."})
                        continue

            turn, tool_calls = _synthesis_only_turn(
                synthesis_nudge_active, turn, tool_calls,
            )
            apply_invalid_only_streak(self, turn)

            # 2. Emit the pilot's prose to the user.
            # Do not emit a post-answer "thinking"/reasoning ConvEvent — live
            # deltas already painted reasoning mid-turn. Pilot JSON "thinking"
            # stays on turn.thinking for internals only.
            #
            # Exception: Cursor CLI/ACP (Grok) often ends the step with the
            # real readout only on the thought channel. Promote that into a
            # message so the turn gets a finished summary bubble.

            cleaned_say_text = clean_say(turn.say) if turn.say else ""
            _extracted_secret = None
            if cleaned_say_text:
                from .secret_vault import extract_secret_request_message
                cleaned_say_text, _extracted_secret = extract_secret_request_message(cleaned_say_text)
            _resp_meta = getattr(resp, "meta", None) or {}
            if not isinstance(_resp_meta, dict):
                _resp_meta = {}
            _promoted_say = promote_trailing_reasoning_to_say(
                say_text=cleaned_say_text,
                streamed_reasoning=str(_resp_meta.get("streamed_reasoning") or ""),
                stream_ended_on_reasoning=bool(
                    _resp_meta.get("stream_ended_on_reasoning")
                ),
                meta_reasoning=str(
                    _resp_meta.get("reasoning") or turn.thinking or ""
                ),
            )
            if _promoted_say:
                _promoted_say = clean_say(_promoted_say) or _promoted_say
            if cleaned_say_text:
                step_emitted_user_prose = True
                # If this prose was already streamed token-by-token, flag it so the
                # frontend finalizes the existing streaming bubble in place instead
                # of treating it as a brand-new message (which would re-dump it).
                _already_streamed = bool(self._streamed_prose.strip())
                yield ConvEvent("message", {"role": "assistant", "text": cleaned_say_text, "streamed": _already_streamed})
                turn_prose.append(cleaned_say_text)
                self._display_transcript.append({"type": "message", "role": "assistant", "text": cleaned_say_text})
            if _promoted_say and _promoted_say.strip() != (cleaned_say_text or "").strip():
                step_emitted_user_prose = True
                yield ConvEvent("message", {
                    "role": "assistant",
                    "text": _promoted_say,
                    "streamed": False,
                    "promoted_from_reasoning": True,
                })
                turn_prose.append(_promoted_say)
                self._display_transcript.append({
                    "type": "message",
                    "role": "assistant",
                    "text": _promoted_say,
                })
            _history_text = cleaned_say_text or ""
            if _promoted_say:
                if _history_text and _promoted_say.strip() != _history_text.strip():
                    _history_text = f"{_history_text}\n\n{_promoted_say}"
                else:
                    _history_text = _promoted_say
            # record the pilot's turn in transcript (prose only -- the conversation)
            if is_native:
                assistant_msg: dict[str, Any] = {"role": "assistant"}
                if _history_text:
                    assistant_msg["content"] = _history_text
                else:
                    assistant_msg["content"] = ""
                if tool_calls:
                    assistant_msg["tool_calls"] = tool_calls
                self._history.append(assistant_msg)
            else:
                self._history.append({"role": "assistant", "content": _history_text or "(acting)"})

            if _extracted_secret:
                from .secret_request import already_present, register_pending_secret_request
                if not any(getattr(a, "kind", "") == "request_secret" for a in turn.actions):
                    if not already_present(self, _extracted_secret["connector"], _extracted_secret["field"]):
                        pending = register_pending_secret_request(self, _extracted_secret)
                        yield ConvEvent("secret_request", {
                            **(pending or _extracted_secret),
                            "session_id": getattr(self, "harness_session_id", "") or "default",
                            "ends_turn": True,
                        })
                        self._sanitize_tool_pairs()
                        yield from finalize_assistant_turn(
                            self, user_message=user_message, step=step,
                            swarms=swarms, turn_prose=turn_prose,
                            turn_findings=turn_findings,
                            extra={"secret_request": True},
                            stop_cause=TERMINAL_NATURAL,
                            **classified_finish_kwargs(last_classified),
                        )
                        return
            if self._turn_budget_exhausted() and not (
                synchronous_swarms
                and not step_emitted_user_prose
                and not turn.has_actions
            ):
                # Close the turn for the UI before wiki ingest (network I/O).
                yield from finalize_assistant_turn(
                    self, user_message=user_message, step=step, swarms=swarms,
                    turn_prose=turn_prose, turn_findings=turn_findings,
                    extra={"turn_budget_exhausted": True},
                    stop_cause=TERMINAL_TURN_BUDGET,
                    **classified_finish_kwargs(last_classified),
                )
                return

            if (
                len(turn.actions) > 0
                or (cleaned_say_text and len(cleaned_say_text.strip()) > 0)
                or (_promoted_say and len(_promoted_say.strip()) > 0)
            ):
                consecutive_non_productive = 0
            else:
                consecutive_non_productive += 1

            if consecutive_non_productive >= 3:
                loop_exit_cause = TERMINAL_EMPTY_LOOP
                break

            # Stagnation governor: repeated normalized assistant prose plus the
            # same action fingerprint with no new progress ends the turn calmly
            # (including when HARNESS_MAX_PILOT_STEPS=0). Distinct actions or
            # real progress reset the streak. Invalid-only steps have their
            # own 3-strike guard after results are appended — skip them here
            # so the third pair still closes before auto_halt.
            from .pilot import is_invalid_only_step as _is_invalid_only_step
            if not _is_invalid_only_step(getattr(turn, "actions", None)):
                try:
                    from .pilot_guards import (
                        fingerprint_turn_actions,
                        normalize_assistant_prose,
                        stagnation_streak_cap,
                    )
                    prose_key = normalize_assistant_prose(_history_text or cleaned_say_text)
                    action_key = fingerprint_turn_actions(turn.actions)
                    # Empty turns (no prose, no actions) are handled by the
                    # consecutive_non_productive break above — skip fingerprinting.
                    if prose_key or action_key:
                        prev_prose = getattr(self, "_stagnation_last_prose", None)
                        prev_actions = getattr(self, "_stagnation_last_actions", None)
                        if (
                            prev_prose is not None
                            and prev_actions is not None
                            and prose_key == prev_prose
                            and action_key == prev_actions
                        ):
                            self._stagnation_streak = int(
                                getattr(self, "_stagnation_streak", 0) or 0
                            ) + 1
                        else:
                            # First sighting of this fingerprint starts the streak.
                            self._stagnation_streak = 1
                        self._stagnation_last_prose = prose_key
                        self._stagnation_last_actions = action_key
                        if self._stagnation_streak >= stagnation_streak_cap():
                            # Heal any tool_call pairing from this assistant turn
                            # before exiting so history stays valid for the next send.
                            self._sanitize_tool_pairs()
                            yield from emit_stagnation_halt(
                                self, last_classified=last_classified,
                                user_message=user_message, step=step,
                                swarms=swarms, turn_prose=turn_prose,
                                turn_findings=turn_findings,
                            )
                            return
                except Exception:
                    pass

            # 3. No actions => the pilot is done talking unless a wait tool
            # kept the step productive. drain_idle_turn delivers pending
            # steers or finalizes. Mid-spree / step-start inject in
            # _check_and_inject_steer is the other steer delivery point.
            if not turn.has_actions:
                synthesis_decision = post_swarm_synthesis_decision(
                    synchronous_swarms=synchronous_swarms,
                    step_emitted_user_prose=step_emitted_user_prose,
                    nudge_sent=post_swarm_nudge_sent,
                )
                if synthesis_decision == "nudge":
                    post_swarm_nudge_sent = True
                    post_swarm_nudge_active = True
                    self._history.append({
                        "role": "user",
                        "content": POST_SWARM_SYNTHESIS_NUDGE,
                    })
                    continue

                if synthesis_decision == "fallback":
                    fallback = POST_SWARM_SYNTHESIS_FALLBACK
                    if self._history and self._history[-1].get("role") == "assistant":
                        self._history[-1]["content"] = fallback
                    else:
                        self._history.append({"role": "assistant", "content": fallback})
                    yield ConvEvent("message", {
                        "role": "assistant",
                        "text": fallback,
                    })
                    turn_prose.append(fallback)
                    self._display_transcript.append({
                        "type": "message",
                        "role": "assistant",
                        "text": fallback,
                    })
                disposition, user_message = yield from drain_idle_turn(
                    self,
                    user_message=user_message,
                    step=step,
                    swarms=swarms,
                    turn_prose=turn_prose,
                    turn_findings=turn_findings,
                    stop_cause=finalize_stop_cause(last_classified),
                    **classified_finish_kwargs(last_classified),
                )
                if disposition == "continue":
                    continue
                if disposition == "break":
                    loop_exit_cause = TERMINAL_DRIVER_SWAP
                    break
                return

            # 4. Execute each action as a collapsible tool-call.
            if native_tools_blocked(last_classified, resp, tool_calls, is_native=is_native, has_actions=turn.has_actions):
                yield ConvEvent("error", {
                    "error": "Incomplete tool arguments cannot be executed.",
                })
                return
            _action_counters = {
                "action_seq": action_seq,
                "swarms": swarms,
                "demo_swarms": demo_swarms,
                "synchronous_swarms": synchronous_swarms,
            }
            _action_disposition, turn_changed_files = yield from execute_turn_actions(
                self,
                turn=turn,
                user_message=user_message,
                is_native=is_native,
                plan=plan,
                counters=_action_counters,
                step=step,
                turn_findings=turn_findings,
            )
            action_seq = _action_counters["action_seq"]
            swarms = _action_counters["swarms"]
            demo_swarms = _action_counters["demo_swarms"]
            synchronous_swarms = _action_counters["synchronous_swarms"]
            if _action_disposition == "secret_request":
                self._sanitize_tool_pairs()
                yield from finalize_assistant_turn(
                    self, user_message=user_message, step=step,
                    swarms=swarms, turn_prose=turn_prose,
                    turn_findings=turn_findings,
                    extra={"secret_request": True},
                    stop_cause=TERMINAL_NATURAL,
                    **classified_finish_kwargs(last_classified),
                )
                return
            if _action_disposition == "return":
                # Cancel mid-spree: heal unanswered tool_calls before exit so
                # the next send/resume/export never sees a dangling tool_use.
                self._sanitize_tool_pairs()
                return

            halt_reason = invalid_only_halt_reason(self)
            if halt_reason:
                self._sanitize_tool_pairs()
                yield ConvEvent("auto_halt", {"reason": halt_reason})
                yield ConvEvent("error", {"error": halt_reason})
                yield from finalize_assistant_turn(
                    self, user_message=user_message, step=step,
                    swarms=swarms, turn_prose=turn_prose,
                    turn_findings=turn_findings,
                    extra={"invalid_tool_halt": True},
                    stop_cause=TERMINAL_INVALID_TOOL,
                    **classified_finish_kwargs(last_classified),
                )
                return

            # ---- AUTO-VERIFY LOOP ----------------------------------------
            # After this batch of actions, IF the pilot edited any files AND
            # auto-verify is enabled, run a FAST, scoped project check and, on
            # FAILURE, inject the output as a tool observation into history and
            # re-ask the model IN THE SAME user message so it self-corrects
            # without the user pointing out the mistake. Bounded by
            # _auto_verify_cap so it cannot loop forever. Silent on pass.
            auto_verify_iters, _verify_again = yield from run_auto_verify(
                self,
                turn_changed_files=turn_changed_files,
                auto_verify_iters=auto_verify_iters,
                auto_verify_cap=_auto_verify_cap,
                plan=plan,
            )
            if _verify_again:
                continue

        # Distinct post-loop closes: empty-loop, driver-swap, or true step cap.
        yield from emit_loop_exit_close(
            self,
            loop_exit_cause=loop_exit_cause,
            last_classified=last_classified,
            user_message=user_message,
            step=step,
            swarms=swarms,
            turn_prose=turn_prose,
            turn_findings=turn_findings,
        )

