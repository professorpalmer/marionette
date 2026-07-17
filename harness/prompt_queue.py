from __future__ import annotations

"""Prompt-queue mixin: playlist CRUD + persistence used by the turn loop.

Extracted mechanically from harness/conversation.py to continue decomposing the
ConversationalSession god-object, matching ToolDispatchMixin's contract: these
methods operate purely through `self` (``_prompt_queue``, ``_prompt_queue_lock``,
``_prompt_queue_path``, ``config``) provided by the concrete class -- the mixin
defines no state and no __init__.

Method Resolution Order keeps behavior identical: enqueue/list/remove/reorder/
clear and the drain helpers still resolve via inheritance.
"""

from typing import Optional


class PromptQueueMixin:
    """Mixin holding prompt-queue persistence and playlist operations.

    The concrete class (ConversationalSession) supplies the state these
    methods read/write via `self`. This mixin defines no __init__ and no
    instance state of its own.
    """

    def _save_prompt_queue(self) -> None:
        """Atomically mirror the current _prompt_queue to disk. Writes a .tmp
        then os.replace so a crash mid-write never leaves a corrupt file. Reads a
        snapshot under the lock, then writes OUTSIDE the lock so callers that
        already hold self._prompt_queue_lock do not deadlock (the lock is not
        reentrant). Best-effort: a persistence failure must never raise."""
        import json
        import os
        try:
            with self._prompt_queue_lock:
                items = [dict(x) for x in self._prompt_queue]
        except Exception:
            return
        try:
            tmp = self._prompt_queue_path + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                json.dump({"queue": items}, f)
            os.replace(tmp, self._prompt_queue_path)
        except Exception:
            # Persistence is a convenience; never let it take down the session.
            pass

    def _load_prompt_queue(self) -> None:
        """Reload the prompt queue written by a prior process. Tolerates a
        missing or corrupt file by leaving the queue empty. Each item must be a
        dict with a text key to be kept."""
        import json
        try:
            with open(self._prompt_queue_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except Exception:
            # Corrupt/unreadable file: start empty rather than crash on restart.
            return
        queue = data.get("queue") if isinstance(data, dict) else None
        if not isinstance(queue, list):
            return
        restored: list[dict] = []
        for it in queue:
            if not isinstance(it, dict) or "text" not in it:
                continue
            restored.append({
                "id": str(it.get("id") or ""),
                "text": str(it.get("text") or ""),
                "images": [str(p) for p in (it.get("images") or []) if p],
                "model": str(it.get("model") or ""),
            })
        with self._prompt_queue_lock:
            self._prompt_queue = restored

    # ------------------------------------------------------------------
    # Prompt queue: sequential playlist of full user turns. Distinct from
    # the steer queue (which is a mid-turn interrupt). See __init__ note.
    # All methods are lock-guarded and never raise; they return neutral
    # values on unexpected input rather than exploding the caller.
    # ------------------------------------------------------------------
    def enqueue_prompt(self, text: str, images: Optional[list] = None,
                       model: Optional[str] = None) -> dict:
        """Append a full prompt to the queue and return the created item.

        Empty/whitespace-only text is rejected with an empty item -- callers
        (server / UI) validate before invoking, this is defense in depth.

        A queued prompt runs as its own fresh user turn, so it can carry image
        attachments (list of file paths). They are stored verbatim after basic
        sanitization and delivered when the prompt drains (see the turn loop).

        ``model`` (optional) stamps the pilot driver that should run this item
        when it drains -- Hermes-style per-prompt selection. Mid-turn playlist
        drain skips items whose model differs from the live pilot so the turn
        can end and the deferred swap can apply before the next turn starts.
        """
        try:
            t = (text or "").strip()
            if not t:
                return {"id": "", "text": "", "images": [], "model": ""}
            imgs = [str(p) for p in (images or []) if p and str(p).strip()]
            import uuid as _uuid
            m = (model or "").strip()
            item = {"id": _uuid.uuid4().hex[:8], "text": t, "images": imgs, "model": m}
            with self._prompt_queue_lock:
                self._prompt_queue.append(item)
            self._save_prompt_queue()
            return dict(item)
        except Exception:
            return {"id": "", "text": "", "images": [], "model": ""}

    def list_prompts(self) -> list:
        """Return a snapshot copy of the queue in order."""
        try:
            with self._prompt_queue_lock:
                return [dict(x) for x in self._prompt_queue]
        except Exception:
            return []

    def remove_prompt(self, id: str) -> bool:
        """Remove the item with the given id. Returns False if not found."""
        try:
            if not id:
                return False
            with self._prompt_queue_lock:
                found = False
                for i, it in enumerate(self._prompt_queue):
                    if it.get("id") == id:
                        del self._prompt_queue[i]
                        found = True
                        break
            if found:
                self._save_prompt_queue()
            return found
        except Exception:
            return False

    def reorder_prompts(self, ordered_ids: list) -> list:
        """Reorder the queue to match the given id order.

        Unknown ids are ignored. Any items whose ids are NOT mentioned in
        ordered_ids are appended at the end in their existing relative order.
        Returns the new snapshot.
        """
        try:
            ids = [str(x) for x in (ordered_ids or []) if x]
            with self._prompt_queue_lock:
                by_id = {it.get("id"): it for it in self._prompt_queue}
                new_order: list[dict] = []
                seen: set = set()
                for id_ in ids:
                    it = by_id.get(id_)
                    if it is not None and id_ not in seen:
                        new_order.append(it)
                        seen.add(id_)
                for it in self._prompt_queue:
                    iid = it.get("id")
                    if iid not in seen:
                        new_order.append(it)
                        seen.add(iid)
                self._prompt_queue = new_order
                snapshot = [dict(x) for x in self._prompt_queue]
            self._save_prompt_queue()
            return snapshot
        except Exception:
            with self._prompt_queue_lock:
                return [dict(x) for x in self._prompt_queue]

    def clear_prompts(self) -> int:
        """Empty the queue. Returns the number of items removed."""
        try:
            with self._prompt_queue_lock:
                n = len(self._prompt_queue)
                self._prompt_queue = []
            self._save_prompt_queue()
            return n
        except Exception:
            return 0

    def _next_queued_needs_driver_swap(self) -> bool:
        """True if the head queue item is stamped for a different pilot model.

        Mid-turn playlist drain must not run a mismatched model inside the
        current step loop -- leave the item queued so the turn ends and the
        deferred swap can apply before the next turn starts.
        """
        try:
            with self._prompt_queue_lock:
                if not self._prompt_queue:
                    return False
                m = str(self._prompt_queue[0].get("model") or "").strip()
                if not m:
                    return False
                return m != str(self.config.driver or "").strip()
        except Exception:
            return False

    def _pop_next_prompt(self) -> dict:
        """Pop and return the first queued prompt, or {} if the queue is empty.

        Internal helper for the turn-completion drain. Never raises.
        """
        try:
            with self._prompt_queue_lock:
                if not self._prompt_queue:
                    return {}
                popped = self._prompt_queue.pop(0)
            self._save_prompt_queue()
            return popped
        except Exception:
            return {}
