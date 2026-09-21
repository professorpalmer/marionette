from __future__ import annotations

from .pilot_replacement import (input_projection, input_conversion, input_publication,
                                replacement_gate, note_input_stop, check_input_owner)

from .prompt_queue import PromptQueueError

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
    SessionActionIllegalTransition,
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
        if hasattr(self, '_queue_lock'):
            def retain(action):
                from .input_receipts import session_input_store
                receipt = session_input_store(self).admit(
                    action.text, images=action.images,
                    upload_root=getattr(self, '_input_upload_root', None), input_id=action.id)
                action.id = receipt['id']
            store.retain_input = retain
        return store

    @input_projection
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
            store = self._action_store()
            if hasattr(self, '_queue_lock'):
                from .input_receipts import session_input_store
                receipts = session_input_store(self)
                known = {r['id']: r for r in receipts.list()}
                for action in list(store):
                    if action.id in known and known[action.id]['status'] not in ('injected', 'dropped'):
                        receipts.transition(action.id, 'dropped', 'stop')
            dropped = store.clear()
        try:
            self._steer_pending = False
        except Exception:
            pass
        return [action.text for action in dropped if str(action.text or "").strip()]

    def retire_input_receipts_after_stop(self, preserve_input_id=None):
        with replacement_gate(self):
            check_input_owner(self)
            note_input_stop(self, preserve_input_id)
            receipts = getattr(self, '_input_receipts', None)
            if receipts is not None:
                receipts.retire_after_stop(preserve_input_id)

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

    @input_conversion
    def steer_with_images(self, text: str, images: Optional[list] = None, *, input_id=None, expected_turn_id=None) -> str:
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
            receipt_id = input_id
            if receipt_id is None and hasattr(self, '_queue_lock'):
                from .input_receipts import session_input_store
                with input_publication(self):
                    receipt_id = session_input_store(self).admit(
                        text or '', images=paths, upload_root=getattr(self, '_input_upload_root', None))['id']
                input_id = receipt_id
            try:
                from .vision import session_supports_native_images
                if session_supports_native_images(self):
                    # Preserve pixels; do not degrade to a weaker sidecar VLM.
                    # Queue-only: enqueue_prompt carries text + paths. A steer
                    # notice here would double-paint chrome on busy Enter.
                    if hasattr(self, "enqueue_prompt"):
                        self.enqueue_prompt(
                            text or "(see attached image)",
                            images=paths, upload_root=getattr(self, "_input_upload_root", None), input_id=input_id,
                        )
                        return DeliveryAction.ENQUEUE_PROMPT.value
                    notice = (
                        cleaned + "\n\n" if cleaned else ""
                    ) + (
                        f"[user attached {len(paths)} image(s); this pilot is "
                        "vision-capable but mid-turn steers cannot carry "
                        "pixels — send as a follow-up turn]"
                    )
                    self.enqueue_steer(notice, input_id=input_id, expected_turn_id=expected_turn_id)
                    return DeliveryAction.ENQUEUE_STEER.value
                if receipt_id is not None:
                    from .input_receipts import session_input_store
                    _, paths = session_input_store(self).delivery_content(receipt_id, '')
                from .vision import transcribe_images
                parts = [cleaned] if cleaned else []
                for r in transcribe_images(paths):
                    if getattr(r, "error", None):
                        parts.append(f"[attached image could not be read: {r.error}]")
                    elif getattr(r, "text", ""):
                        parts.append(f"[attached image]\n{r.text}")
                combined = "\n\n".join(p for p in parts if p) or "[attached image produced no transcription]"
                if combined:
                    self.enqueue_steer(combined, input_id=receipt_id, expected_turn_id=expected_turn_id)
                return DeliveryAction.ENQUEUE_STEER.value
            except (PromptQueueError, SessionActionIllegalTransition):
                raise
            except Exception as e:
                parts = [cleaned] if cleaned else []
                parts.append(f"[attached image transcription failed: {e}]")
                combined = "\n\n".join(p for p in parts if p)
                if combined:
                    self.enqueue_steer(combined, input_id=receipt_id, expected_turn_id=expected_turn_id)
                return DeliveryAction.ENQUEUE_STEER.value
        if cleaned:
            self.enqueue_steer(text, input_id=input_id, expected_turn_id=expected_turn_id)
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

    @input_projection
    def enqueue_steer(self, text: str, *, expected_turn_id: Optional[str] = None, input_id=None):
        """Append a pending mid-turn user steer.

        Thin adapter: admits ``kind=steer`` with ``delivery=next_turn_boundary``.
        While an abandoned generator still holds ``_busy`` after Stop, refuse
        to queue: a steer has nowhere truthful to go. Ready/idle sessions keep
        standard enqueue/drain even if ``_stop_holds_idle`` is still sticky for
        runners chrome (cleared on the next real user send).
        """
        cleaned = (text or "").strip()
        if input_id and hasattr(self, '_queue_lock'):
            from .input_receipts import session_input_store
            receipts = session_input_store(self)
            receipt = receipts.get(input_id)
            if receipt['status'] != 'accepted' or receipt['owner_instance'] != receipts.instance:
                from .input_receipts import InputReceiptError
                raise InputReceiptError('input_held', 'Input delivery was stopped or held; keep your draft and review it before sending again.')
            text, _ = receipts.delivery_content(input_id, text)
            cleaned = text.strip()
        if not cleaned:
            return
        with self._steer_lock:
            if self._abandoned_turn_blocks_steer_enqueue():
                if hasattr(self, '_queue_lock'):
                    from .input_receipts import session_input_store
                    receipts = session_input_store(self)
                    receipt = receipts.get(input_id) if input_id else receipts.admit(text)
                    receipts.transition(receipt['id'], 'dropped', 'stop')
                self._record_steer_drop_notice([cleaned])
                return
            return self._action_store().admit(
                ActionKind.STEER,
                text,
                delivery=DeliveryPolicy.NEXT_TURN_BOUNDARY,
                expected_turn_id=expected_turn_id,
                input_id=input_id,
            )

    def _drain_ready_input_actions(self, policy, kinds):
        with self._steer_lock:
            store = self._action_store()
            ready = [a for a in store if a.kind in kinds and a.delivery is policy]
            if hasattr(self, '_queue_lock'):
                from .input_receipts import session_input_store
                receipts = session_input_store(self)
                known = {r['id']: r for r in receipts.list()}
                for action in ready:
                    if action.id not in known:
                        receipts.admit(action.text, images=action.images, input_id=action.id,
                                       upload_root=getattr(self, '_input_upload_root', None))
                    receipts.transition(action.id, 'delivering')
            return store.drain_ready(policy, kinds=kinds)

    def _drain_steer_actions(self) -> List[SessionAction]:
        return self._drain_ready_input_actions(DeliveryPolicy.NEXT_TURN_BOUNDARY, injectable_kinds())

    def drain_steer(self) -> list[str]:
        return [action.text for action in self._drain_steer_actions()]

    def _drain_mailbox_actions(self):
        return self._drain_ready_input_actions(DeliveryPolicy.WHEN_RUN_IDLE, (ActionKind.MAILBOX,))

    def drain_mailbox(self) -> list[str]:
        return [a.text for a in self._drain_mailbox_actions() if a.text.strip()]

    def _publish_input_actions(self, actions, contents, *, display=True):
        """Append a complete batch and persist its IDs before releasing events."""
        with self._steer_lock:
            receipts = None
            if hasattr(self, '_queue_lock'):
                from .input_receipts import session_input_store
                receipts = session_input_store(self)
            if self._steer_boundary_blocks_inject():
                if receipts is not None:
                    for action in actions:
                        receipts.transition(action.id, 'dropped', 'stop_before_injection')
                self._settle_input_actions(actions)
                self._record_steer_drop_notice([a.text for a in actions])
                return False
            for action, content in zip(actions, contents):
                self._history.append({'role': 'user', 'content': content, 'input_id': action.id})
                transcript = getattr(self, '_display_transcript', None)
                if display and transcript is not None:
                    transcript.append({'type': 'message', 'role': 'user', 'text': action.text, 'input_id': action.id})
            if receipts is not None:
                from .input_receipts import publish_session_injected
                publish_session_injected(self, [a.id for a in actions])
            self._settle_input_actions(actions)
            return True

    def _settle_input_actions(self, actions) -> None:
        store = self._action_store()
        settle = getattr(store, "settle", None)
        if not callable(settle):
            return
        for action in actions:
            action_id = getattr(action, "id", None)
            if action_id:
                settle(action_id)

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
        if not self._steer_inject_boundary_is_safe():
            # Keep pending until pairs complete (or sanitize heals them). Signal
            # the action loop to abandon remaining tools so the next step can
            # inject at a safe boundary — do NOT insert a user row between
            # assistant tool_use and tool_result.
            self._steer_pending = True
            return
        actions = self._drain_steer_actions()
        if not actions:
            return
        contents = []
        for action in actions:
            content = self._format_steer_user_content(action.text)
            try:
                from .task_transaction import context_block
                extra = context_block(getattr(self, '_task_tx', None))
                if extra:
                    content += '\n\n' + extra
            except Exception:
                pass
            contents.append(content)
        if self._publish_input_actions(actions, contents):
            self._steer_pending = True
            for action in actions:
                yield ConvEvent('steer', {'text': action.text, 'input_id': action.id})

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
