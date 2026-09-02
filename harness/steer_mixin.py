from __future__ import annotations

"""Steer mixin: mid-turn interrupt enqueue/drain/inject helpers.

Extracted mechanically from harness/conversation.py to continue decomposing the
ConversationalSession god-object, matching ToolDispatchMixin / PromptQueueMixin
contract: these methods operate through `self` (``_session_actions`` /
``_steer_queue``, ``_steer_lock``, ``_steer_pending``, ``_history``)
provided by the concrete class -- the mixin defines no state and no __init__.
Pending mid-turn input is stored as SessionAction objects under ``_steer_lock``.

Prompt-queue playlist CRUD stays on PromptQueueMixin. Busy lifecycle lives
on BusyControlMixin; ``_send_locked_inner`` control flow lives on
SendLoopMixin.

Method Resolution Order keeps behavior identical: steer_with_images /
enqueue_steer / drain_steer / _check_and_inject_steer still resolve via
inheritance.
"""

from typing import Iterator, List, Optional

from .session_actions import (
    ActionKind,
    DeliveryPolicy,
    SessionAction,
    SessionActionStore,
    SteerQueueView,
    injectable_kinds,
)


class SteerMixin:
    """Mixin holding mid-turn steer enqueue, drain, and inject helpers.

    The concrete class (ConversationalSession) supplies the state these
    methods read/write via `self`. This mixin defines no __init__ and no
    instance state of its own.
    """

    def _steer_boundary_blocks_inject(self) -> bool:
        """True when Stop has abandoned the active turn — steers must not inject.

        Covers both the sticky idle hold (``_stop_holds_idle``) and the
        cooperative cancel window while an abandoned generator may still be
        unwinding mid-spree.
        """
        if getattr(self, "_stop_holds_idle", False):
            return True
        try:
            cancel = getattr(self, "_cancel", None)
            if cancel is not None and cancel.is_set() and getattr(
                self, "_interrupt_requested", False
            ):
                return True
        except Exception:
            pass
        return False

    def _action_store(self) -> SessionActionStore:
        """Return the session admission store, installing one on minimal hosts."""
        store = getattr(self, "_session_actions", None)
        if not isinstance(store, SessionActionStore):
            view = getattr(self, "_steer_queue", None)
            if isinstance(view, SteerQueueView):
                store = view._store
            elif isinstance(view, SessionActionStore):
                store = view
            else:
                store = SessionActionStore()
                if view is not None:
                    for item in list(view):
                        text = (
                            item.text
                            if isinstance(item, SessionAction)
                            else str(item or "").strip()
                        )
                        if text:
                            store.admit(
                                ActionKind.STEER,
                                text,
                                delivery=DeliveryPolicy.NEXT_TURN_BOUNDARY,
                            )
            self._session_actions = store
        if not isinstance(getattr(self, "_steer_queue", None), SteerQueueView):
            self._steer_queue = SteerQueueView(store)
        return store

    def admit_session_action(self, kind, text: str = "", **kwargs) -> SessionAction:
        """Locked admit into the session store (HTTP RecoverTurn / turn input)."""
        with self._steer_lock:
            return self._action_store().admit(kind, text, **kwargs)

    def drop_queued_steers(self) -> list[str]:
        """Atomically discard pending steers and clear the mid-spree pending flag.

        Used at the Stop/interrupt boundary so queued content cannot inject into
        an abandoned generator or contaminate a later unrelated user send.
        """
        with self._steer_lock:
            dropped = self._action_store().clear()
        try:
            self._steer_pending = False
        except Exception:
            pass
        return [action.text for action in dropped if str(action.text or "").strip()]

    @staticmethod
    def _steer_drop_notice_text(dropped: list[str]) -> str:
        n = len(dropped)
        return (
            f"Dropped {n} queued steer message(s) after Stop. "
            "They were not injected into the interrupted turn and will not "
            "apply to the next send."
        )

    def _record_steer_drop_notice(self, dropped: list[str]) -> Optional[str]:
        """Persist a durable + streamable notice that steers were dropped."""
        if not dropped:
            return None
        text = self._steer_drop_notice_text(dropped)
        try:
            display = getattr(self, "_display_transcript", None)
            if display is not None:
                display.append({
                    "type": "message",
                    "role": "assistant",
                    "text": text,
                })
        except Exception:
            pass
        # Omit ConvEvent data.kind so the UI wait-hint path surfaces the
        # message live; reason carries the machine-readable drop cause.
        self._pending_steer_drop_notice = {
            "message": text,
            "reason": "steer_dropped",
            "count": len(dropped),
        }
        return text

    def _flush_steer_drop_notice(self) -> Iterator["ConvEvent"]:
        """Yield a streamed notice for a prior drop, if one is pending."""
        pending = getattr(self, "_pending_steer_drop_notice", None)
        if not pending:
            return
        self._pending_steer_drop_notice = None
        from .conversation import ConvEvent
        yield ConvEvent("notice", dict(pending))

    def steer_with_images(self, text: str, images: Optional[list] = None) -> str:
        """Enqueue a steer with attached images.

        Mid-turn steers are text-only user messages at a safe boundary, so they
        cannot carry raw image blocks. Policy:

        - Vision-capable pilots (gpt-5.6-luna, etc.): NEVER run the vision
          sidecar (weaker VLM paraphrase). Queue a follow-up turn via
          ``enqueue_prompt`` so the next turn gets native multimodal pixels.
          Do not enqueue a mid-turn steer notice — that would paint a second
          chrome row (``steer:`` plus QUEUED TO SEND) for one Enter.
        - Text-only pilots: transcribe via sidecar into the steer text (same
          path as view_image for non-vision models).

        Returns the action actually taken (``enqueue_prompt`` or
        ``enqueue_steer``) so HTTP/UI chrome can match. Empty string if
        nothing was enqueued.
        """
        from .delivery_mode import DeliveryAction

        cleaned = text.strip() if text and text.strip() else ""
        paths = [p for p in (images or []) if p]
        if paths:
            try:
                from .vision import session_supports_native_images
                if session_supports_native_images(self):
                    # Preserve pixels; do not degrade to a weaker sidecar VLM.
                    # Queue-only: enqueue_prompt carries text + paths. A steer
                    # notice here would double-paint chrome on busy Enter.
                    if hasattr(self, "enqueue_prompt"):
                        self.enqueue_prompt(
                            cleaned or "(see attached image)",
                            images=paths,
                        )
                        return DeliveryAction.ENQUEUE_PROMPT.value
                    notice = (
                        cleaned + "\n\n" if cleaned else ""
                    ) + (
                        f"[user attached {len(paths)} image(s); this pilot is "
                        "vision-capable but mid-turn steers cannot carry "
                        "pixels — send as a follow-up turn]"
                    )
                    self.enqueue_steer(notice)
                    return DeliveryAction.ENQUEUE_STEER.value
                from .vision import transcribe_images
                parts = [cleaned] if cleaned else []
                for r in transcribe_images(paths):
                    if getattr(r, "error", None):
                        parts.append(f"[attached image could not be read: {r.error}]")
                    elif getattr(r, "text", ""):
                        parts.append(f"[attached image]\n{r.text}")
                combined = "\n\n".join(p for p in parts if p)
                if combined:
                    self.enqueue_steer(combined)
                return DeliveryAction.ENQUEUE_STEER.value
            except Exception as e:
                parts = [cleaned] if cleaned else []
                parts.append(f"[attached image transcription failed: {e}]")
                combined = "\n\n".join(p for p in parts if p)
                if combined:
                    self.enqueue_steer(combined)
                return DeliveryAction.ENQUEUE_STEER.value
        if cleaned:
            self.enqueue_steer(cleaned)
            return DeliveryAction.ENQUEUE_STEER.value
        return ""

    def _abandoned_turn_blocks_steer_enqueue(self) -> bool:
        """True only while a Stop-abandoned generator may still own the turn.

        ``_stop_holds_idle`` alone is sticky for UI/resume suppression and can
        linger on an otherwise ready idle session (including across tests that
        share the module pilot). Refuse enqueue only when that hold coincides
        with ``_busy`` still locked — the actually abandoned generation.
        """
        if not getattr(self, "_stop_holds_idle", False):
            return False
        busy = getattr(self, "_busy", None)
        if busy is None:
            # Minimal hosts without a busy lock: honor the explicit abandon mark.
            return bool(getattr(self, "_steer_boundary_drop_on_acquire", False))
        try:
            return bool(busy.locked())
        except Exception:
            # Fail closed under Stop hold if the lock is unreadable.
            return True

    def enqueue_steer(self, text: str, *, expected_turn_id: Optional[str] = None) -> None:
        """Append a pending mid-turn user steer.

        Thin adapter: admits ``kind=steer`` with ``delivery=next_turn_boundary``.
        While an abandoned generator still holds ``_busy`` after Stop, refuse
        to queue: a steer has nowhere truthful to go. Ready/idle sessions keep
        standard enqueue/drain even if ``_stop_holds_idle`` is still sticky for
        runners chrome (cleared on the next real user send).
        """
        cleaned = (text or "").strip()
        if not cleaned:
            return
        if self._abandoned_turn_blocks_steer_enqueue():
            self._record_steer_drop_notice([cleaned])
            return
        with self._steer_lock:
            self._action_store().admit(
                ActionKind.STEER,
                cleaned,
                delivery=DeliveryPolicy.NEXT_TURN_BOUNDARY,
                expected_turn_id=expected_turn_id,
            )

    def _drain_steer_actions(self) -> List[SessionAction]:
        """Pop steer/mailbox actions ready at the next-turn boundary."""
        with self._steer_lock:
            return self._action_store().drain_ready(
                DeliveryPolicy.NEXT_TURN_BOUNDARY,
                kinds=injectable_kinds(),
            )

    def drain_steer(self) -> list[str]:
        """Atomically pop and return all pending steer/mailbox texts (empty if none)."""
        return [action.text for action in self._drain_steer_actions()]

    def drain_mailbox(self) -> list[str]:
        """Pop mailbox actions due when the run is idle."""
        with self._steer_lock:
            actions = self._action_store().drain_ready(
                DeliveryPolicy.WHEN_RUN_IDLE,
                kinds=(ActionKind.MAILBOX,),
            )
        return [action.text for action in actions if str(action.text or "").strip()]

    @staticmethod
    def _format_steer_user_content(text: str) -> str:
        """Clamp and hard-wrap steer text for a first-class role=user message.

        Shared by the safe-boundary inject path and finalization-time delivery
        so both use the same bounded content rules without OUT-OF-BAND wrapping.
        """
        # Lazy imports avoid a conversation <-> steer_mixin cycle at module load.
        from .conversation import _clamp_tool_result, _hardwrap_long_tokens
        text = _clamp_tool_result(text)
        return _hardwrap_long_tokens(text, width=200)

    @staticmethod
    def _steer_marker(text: str) -> str:
        """Legacy OUT-OF-BAND wrapper kept for reading old history / tests.

        Happy-path inject no longer uses this. Prefer
        ``_format_steer_user_content`` for new role=user steers.
        """
        body = SteerMixin._format_steer_user_content(text)
        return (
            "\n\n[OUT-OF-BAND USER MESSAGE - a direct message from the user, "
            "delivered mid-turn; not tool output. Stop your current line of work, "
            "address THIS now, and do not resume the previous task unless the user "
            f"asks.]\n{body}\n[/OUT-OF-BAND USER MESSAGE]"
        )

    def _steer_inject_boundary_is_safe(self) -> bool:
        """True when appending role=user will not break tool_use/tool_result pairing.

        Unsafe while the most recent assistant tool_use still has unanswered
        tool_calls in its contiguous adjacent result run (mid-tool / unpaired).
        Safe after that pair completes, after a prose-only assistant, or when
        no open tool_use exists.
        """
        history = getattr(self, "_history", None) or []
        if not history:
            return True
        last_tool_use = None
        for i in range(len(history) - 1, -1, -1):
            m = history[i]
            role = m.get("role")
            if role == "assistant":
                if m.get("tool_calls"):
                    last_tool_use = i
                break
        if last_tool_use is None:
            return True
        expected = {
            tc.get("id")
            for tc in (history[last_tool_use].get("tool_calls") or [])
            if tc.get("id")
        }
        if not expected:
            return True
        answered: set[str] = set()
        j = last_tool_use + 1
        while j < len(history) and history[j].get("role") == "tool":
            tcid = history[j].get("tool_call_id")
            if tcid:
                answered.add(tcid)
            j += 1
        # A non-tool message already sits between the tool_use and further
        # results (or after a partial run). Only inject when every id is
        # answered in the contiguous adjacent run — never wedge a user row
        # into an open pair.
        if j < len(history) and history[last_tool_use + 1].get("role") != "tool":
            # Something non-tool already follows the assistant tool_use with
            # no adjacent results — pairing is already broken; refuse inject.
            return False
        return answered == expected

    def _check_and_inject_steer(self) -> Iterator["ConvEvent"]:
        """Drain pending steers into a first-class user message at a safe boundary.

        Safe boundary: after the current assistant+tool-pair step is complete
        (all tool_calls answered by contiguous adjacent tool results), before
        the next chat() call. Appends ``role=user`` with clamped/hardwrapped
        content so the next model step sees a normal user message — not a
        piggyback inside tool output.

        If the boundary is not yet safe (mid-tool / unpaired calls), steers
        stay pending. ``_steer_pending`` is still set so the action loop can
        abandon the remaining spree, sanitize dangling pairs, and retry inject
        on the next step once the boundary is safe.

        After Stop / cooperative interrupt, queued steers are dropped (never
        injected into an abandoned generator) and a durable/streamed notice is
        emitted instead.
        """
        from .conversation import ConvEvent
        if self._steer_boundary_blocks_inject():
            dropped = self.drop_queued_steers()
            if dropped:
                self._record_steer_drop_notice(dropped)
            flush_all = getattr(self, "_flush_stop_boundary_notices", None)
            if callable(flush_all):
                yield from flush_all()
            else:
                yield from self._flush_steer_drop_notice()
            return
        actions = self._drain_steer_actions()
        if not actions:
            return
        if not self._steer_inject_boundary_is_safe():
            # Keep pending until pairs complete (or sanitize heals them). Signal
            # the action loop to abandon remaining tools so the next step can
            # inject at a safe boundary — do NOT insert a user row between
            # assistant tool_use and tool_result.
            with self._steer_lock:
                self._action_store().requeue_front(actions)
            self._steer_pending = True
            return
        for action in actions:
            steer = action.text
            content = self._format_steer_user_content(steer)
            try:
                from .task_transaction import context_block

                extra = context_block(getattr(self, "_task_tx", None))
                if extra:
                    content = content + "\n\n" + extra
            except Exception:
                pass
            yield ConvEvent("steer", {"text": steer})
            self._history.append({"role": "user", "content": content})
            try:
                display = getattr(self, "_display_transcript", None)
                if display is not None:
                    display.append({
                        "type": "message",
                        "role": "user",
                        "text": steer,
                    })
            except Exception:
                pass
            self._steer_pending = True

    def _tool_result_is_adjacent(self, i: int) -> bool:
        """True when the tool-role message at history index ``i`` is part of the
        contiguous run of tool results IMMEDIATELY following an assistant
        tool_use, with no non-tool message wedged between that assistant and
        ``i``. Kept for pairing diagnostics / callers that still reason about
        adjacent tool runs."""
        history = self._history
        if not (0 <= i < len(history)) or history[i].get("role") != "tool":
            return False
        j = i - 1
        while j >= 0 and history[j].get("role") == "tool":
            j -= 1
        # history[j] must be the assistant tool_use that opened this run.
        return j >= 0 and history[j].get("role") == "assistant" and bool(history[j].get("tool_calls"))
