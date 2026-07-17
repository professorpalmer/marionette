from __future__ import annotations

"""Review + memory-proposal mixin for ConversationalSession.

Extracted mechanically from harness/conversation.py to continue decomposing the
ConversationalSession god-object, matching ToolDispatchMixin / WikiDistillMixin
contract: these methods operate through `self` (``_pending_reviews``,
``_pending_reviews_lock``, ``_apply_lock``, ``_turn_memory_queue``,
``_pending_memory_proposals``, ``_memory``, …) provided by the concrete class --
the mixin defines no state and no __init__.

``_apply_worker_patch`` stays on ConversationalSession (checkpoint / git-apply
path tightly coupled to host state). Busy lifecycle, ``send``, and swarm drain
also stay on the host.

Method Resolution Order keeps behavior identical: ``apply_review``,
``dismiss_review``, ``accept_memory_proposal``, etc. still resolve via
inheritance.
"""


class ReviewMemoryMixin:
    """Mixin holding pending-diff review apply/dismiss and memory-proposal helpers.

    The concrete class (ConversationalSession) supplies the state these
    methods read/write via `self`. This mixin defines no __init__ and no
    instance state of its own.
    """

    def apply_review(self, review_id: str, decisions: dict) -> dict:
        with self._pending_reviews_lock:
            review = self._pending_reviews.get(review_id)
            if not review:
                return {
                    "ok": False,
                    "applied_files": [],
                    "rejected_hunks": [],
                    "checkpoint_id": None,
                    "message": "Pending review not found"
                }

        rejected_hunks = []
        all_hunks = []
        for f in review["files"]:
            for h in f["hunks"]:
                h_id = h["id"]
                all_hunks.append(h_id)
                dec = decisions.get(h_id, "reject")
                if dec == "reject":
                    rejected_hunks.append(h_id)

        # Reconstruct the accepted subset diff
        from .diffreview import reconstruct_diff
        accepted_diff = reconstruct_diff(review["files"], decisions)

        applied_files = []
        for f in review["files"]:
            if any(decisions.get(h["id"]) == "accept" for h in f["hunks"]):
                applied_files.append(f["path"])

        # If ALL hunks are rejected, do not apply anything, just remove the review
        if len(rejected_hunks) == len(all_hunks):
            with self._pending_reviews_lock:
                self._pending_reviews.pop(review_id, None)
            return {
                "ok": True,
                "applied_files": [],
                "rejected_hunks": rejected_hunks,
                "checkpoint_id": None,
                "message": "All hunks were rejected. No changes applied."
            }

        mock_artifacts = [
            {
                "type": "patch",
                "payload": {
                    "files": applied_files,
                    "unified_diff": accepted_diff
                }
            }
        ]

        with self._apply_lock:
            applied, files_changed, apply_msg = self._apply_worker_patch(mock_artifacts, review.get("job_id", ""))
            cp_id = getattr(self, "_last_checkpoint_id", None)

        if applied:
            with self._pending_reviews_lock:
                self._pending_reviews.pop(review_id, None)
            return {
                "ok": True,
                "applied_files": files_changed,
                "rejected_hunks": rejected_hunks,
                "checkpoint_id": cp_id,
                "message": f"Successfully applied: {apply_msg}"
            }
        else:
            with self._pending_reviews_lock:
                self._pending_reviews.pop(review_id, None)
            return {
                "ok": False,
                "applied_files": [],
                "rejected_hunks": rejected_hunks,
                "checkpoint_id": cp_id,
                "message": f"Failed to apply: {apply_msg}"
            }

    def dismiss_review(self, review_id: str) -> bool:
        with self._pending_reviews_lock:
            if review_id in self._pending_reviews:
                self._pending_reviews.pop(review_id)
                return True
            return False

    def _flush_turn_memory_proposals(self) -> list:
        """Move queued mid-turn memory-add hints into pending Save/Skip cards.

        Called only after assistant_done on interactive turns. Caps at 3
        proposals. Nothing is written to the store until accept_memory_proposal.
        """
        queued = list(self._turn_memory_queue or [])
        self._turn_memory_queue = []
        if not queued:
            return []
        import uuid as _uuid
        out = []
        # Exact-text dedupe against already-persisted entries and against
        # proposals already pending from earlier turns.
        existing_texts = {
            (e.text or "").strip().lower() for e in self._memory.list()
        }
        for p in self._pending_memory_proposals.values():
            existing_texts.add((p.get("text") or "").strip().lower())
        for item in queued:
            if len(out) >= 3:
                break
            text = (item.get("text") or "").strip()
            if not text:
                continue
            key = text.lower()
            if key in existing_texts:
                continue
            existing_texts.add(key)
            prop_id = "memprop_" + _uuid.uuid4().hex[:12]
            cat = (item.get("category") or "general").strip() or "general"
            prop = {
                "id": prop_id,
                "text": text,
                "category": cat,
            }
            self._pending_memory_proposals[prop_id] = prop
            out.append(prop)
        return out

    def accept_memory_proposal(self, proposal_id: str) -> dict:
        """Persist a pending end-of-turn memory proposal (source=agent)."""
        prop = self._pending_memory_proposals.pop(proposal_id, None)
        if not prop:
            return {"ok": False, "error": "proposal not found"}
        text = (prop.get("text") or "").strip()
        if not text:
            return {"ok": False, "error": "empty proposal"}
        entry = self._memory.add(
            text=text,
            category=(prop.get("category") or "general").strip() or "general",
            source="agent",
        )
        return {
            "ok": True,
            "id": entry.id,
            "text": entry.text,
            "category": entry.category,
            "source": entry.source,
            "created_at": entry.created_at,
        }

    def dismiss_memory_proposal(self, proposal_id: str) -> dict:
        """Drop a pending end-of-turn memory proposal without writing."""
        if proposal_id in self._pending_memory_proposals:
            self._pending_memory_proposals.pop(proposal_id, None)
            return {"ok": True}
        return {"ok": False, "error": "proposal not found"}
