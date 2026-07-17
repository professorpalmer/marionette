from __future__ import annotations

"""Steer mixin: mid-turn interrupt enqueue/drain/inject helpers.

Extracted mechanically from harness/conversation.py to continue decomposing the
ConversationalSession god-object, matching ToolDispatchMixin / PromptQueueMixin
contract: these methods operate through `self` (``_steer_queue``, ``_steer_lock``,
``_steer_pending``, ``_history``) provided by the concrete class -- the mixin
defines no state and no __init__.

Prompt-queue playlist CRUD stays on PromptQueueMixin. Busy lifecycle and
``_send_locked_inner`` control flow stay on ConversationalSession.

Method Resolution Order keeps behavior identical: steer_with_images /
enqueue_steer / drain_steer / _check_and_inject_steer still resolve via
inheritance.
"""

from typing import Iterator, Optional


class SteerMixin:
    """Mixin holding mid-turn steer enqueue, drain, and inject helpers.

    The concrete class (ConversationalSession) supplies the state these
    methods read/write via `self`. This mixin defines no __init__ and no
    instance state of its own.
    """

    def steer_with_images(self, text: str, images: Optional[list] = None) -> None:
        """Enqueue a steer, transcribing any attached images into the steer text.

        A steer injects as TEXT into the active turn's tool-output stream, so it
        cannot carry raw image blocks mid-run. Previously an image attached to a
        steer was dropped and only its screenshot id/path survived as opaque
        text. We now run the same vision transcription used by view_image and
        append it, so 'look at this + <image>' actually reaches the model.
        """
        parts = [text.strip()] if text and text.strip() else []
        paths = [p for p in (images or []) if p]
        if paths:
            try:
                from .vision import transcribe_images
                for r in transcribe_images(paths):
                    if getattr(r, "error", None):
                        parts.append(f"[attached image could not be read: {r.error}]")
                    elif getattr(r, "text", ""):
                        parts.append(f"[attached image]\n{r.text}")
            except Exception as e:
                parts.append(f"[attached image transcription failed: {e}]")
        combined = "\n\n".join(p for p in parts if p)
        if combined:
            self.enqueue_steer(combined)

    def enqueue_steer(self, text: str) -> None:
        """Append an out-of-band user message."""
        with self._steer_lock:
            self._steer_queue.append(text)

    def drain_steer(self) -> list[str]:
        """Atomically pop and return all pending steer messages (empty list if none)."""
        with self._steer_lock:
            items = list(self._steer_queue)
            self._steer_queue.clear()
            return items

    @staticmethod
    def _steer_marker(text: str) -> str:
        """Single definition of the OUT-OF-BAND USER MESSAGE marker wrapping a
        steer. Shared by both delivery points (mid-spree piggyback in
        _check_and_inject_steer, and finalization-time user-message append in
        the step loop) so the literal is never duplicated.

        The incoming text is clamped (bounded length) and any single unbroken
        run of >200 non-whitespace chars (e.g. a pasted key/sha) is hard-wrapped
        so it cannot overflow. This covers BOTH delivery points because both
        route through this one helper."""
        # Lazy imports avoid a conversation <-> steer_mixin cycle at module load.
        from .conversation import _clamp_tool_result, _hardwrap_long_tokens
        text = _clamp_tool_result(text)
        text = _hardwrap_long_tokens(text, width=200)
        return (
            "\n\n[OUT-OF-BAND USER MESSAGE - a direct message from the user, "
            "delivered mid-turn; not tool output. Stop your current line of work, "
            "address THIS now, and do not resume the previous task unless the user "
            f"asks.]\n{text}\n[/OUT-OF-BAND USER MESSAGE]"
        )

    def _check_and_inject_steer(self) -> Iterator["ConvEvent"]:
        """Drain pending steers and surface them to the model WITHOUT breaking
        message role alternation or injecting a synthetic user turn mid-loop.

        Mirrors the Hermes design (agent/conversation_loop.py pre-API steer
        drain): a steer is appended to the LAST tool-result message's content,
        so the model sees it as part of the tool output on its next iteration.
        A synthetic user message mid-loop (what this used to do) breaks strict
        user/assistant alternation -- providers like Moonshot reject it and
        return empty content, wedging the loop. If there is no tool/result
        message to piggyback on yet, the steer is put back as pending for the
        next drain rather than forced in.

        Sets self._steer_pending so the action loop can stop the current spree
        and re-ask the model, which now sees the steer in the tool output.
        """
        from .conversation import ConvEvent
        steers = self.drain_steer()
        if not steers:
            return
        for steer in steers:
            marker_text = self._steer_marker(steer)
            yield ConvEvent("steer", {"text": steer})
            # Inject into the last result-bearing message (tool role for native
            # tool-calling, or the user-role result the JSON-envelope path appends).
            #
            # Adjacency safety: a tool-role result may only be piggybacked on
            # when it belongs to the CONTIGUOUS run of tool results IMMEDIATELY
            # following the last assistant tool_use. Injecting into a tool
            # message that already has a non-tool message after it (before the
            # next assistant) would leave that assistant tool_use no longer
            # directly followed by its tool_result -- the steer itself would
            # create the non-adjacent tool_use/tool_result Anthropic rejects. In
            # that case defer the steer (put it back pending), exactly like the
            # no-target case.
            injected = False
            for i in range(len(self._history) - 1, -1, -1):
                m = self._history[i]
                role = m.get("role")
                if role == "tool":
                    # Only safe if this tool message traces back through a
                    # contiguous tool-result run to an assistant tool_use with no
                    # non-tool gap. Since we scan from the end, a non-tool
                    # message after it would have been hit first, so reaching a
                    # tool message here means nothing non-tool follows it.
                    if self._tool_result_is_adjacent(i):
                        m["content"] = (m.get("content") or "") + marker_text
                        injected = True
                    break
                if role == "user" and i > 0:
                    m["content"] = (m.get("content") or "") + marker_text
                    injected = True
                    break
                if role == "assistant":
                    # Hit an assistant turn before any tool result -- nothing to
                    # piggyback on this iteration; put the steer back as pending.
                    break
            if injected:
                self._steer_pending = True
            else:
                # No result message to inject into yet -- keep it pending so the
                # next drain (after a tool batch) picks it up. Never force a
                # synthetic user turn.
                with self._steer_lock:
                    self._steer_queue.appendleft(steer)

    def _tool_result_is_adjacent(self, i: int) -> bool:
        """True when the tool-role message at history index ``i`` is part of the
        contiguous run of tool results IMMEDIATELY following an assistant
        tool_use, with no non-tool message wedged between that assistant and
        ``i``. Piggybacking a steer onto such a message keeps the tool_use ->
        tool_result adjacency Anthropic requires."""
        history = self._history
        if not (0 <= i < len(history)) or history[i].get("role") != "tool":
            return False
        j = i - 1
        while j >= 0 and history[j].get("role") == "tool":
            j -= 1
        # history[j] must be the assistant tool_use that opened this run.
        return j >= 0 and history[j].get("role") == "assistant" and bool(history[j].get("tool_calls"))
