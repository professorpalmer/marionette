"""Review queue + inline-edit HTTP route bodies (peeled from ``harness.server``)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, List, Union


@dataclass
class ReviewServices:
    """Explicit deps for review / inline-edit HTTP handlers."""

    cfg: Any
    get_pilot: Callable[[], Any]
    resolve_editor_path: Callable[[str, str], Any]
    strip_markdown_fences: Callable[[str], str]


JsonPayload = Union[dict, list]


def post_reviews_apply(body: dict, svc: ReviewServices) -> tuple[int, JsonPayload]:
    """POST /api/reviews/apply."""
    review_id = (body.get("id") or "").strip()
    decisions = body.get("decisions", {})
    if not review_id:
        return 400, {"error": "Missing review id"}
    res = svc.get_pilot().apply_review(review_id, decisions)
    return 200, res


def post_reviews_dismiss(body: dict, svc: ReviewServices) -> tuple[int, JsonPayload]:
    """POST /api/reviews/dismiss."""
    review_id = (body.get("id") or "").strip()
    if not review_id:
        return 400, {"error": "Missing review id"}
    success = svc.get_pilot().dismiss_review(review_id)
    return 200, {"ok": success}


def get_reviews(svc: ReviewServices) -> tuple[int, List[Any]]:
    """GET /api/reviews."""
    pilot = svc.get_pilot()
    lock = getattr(pilot, "_pending_reviews_lock", None)
    pending = getattr(pilot, "_pending_reviews", None)
    if lock is None or pending is None:
        return 200, []
    with lock:
        return 200, list(pending.values())


def post_inline_edit(body: dict, svc: ReviewServices) -> tuple[int, JsonPayload]:
    """POST /api/inline-edit."""
    repo = svc.cfg.repo
    if not repo or not os.path.exists(repo):
        return 400, {"error": "No open workspace"}
    rel_path = (body.get("path") or "").strip()
    if not rel_path:
        return 400, {"error": "Missing path parameter"}
    try:
        svc.resolve_editor_path(repo, rel_path)
    except ValueError as e:
        return 400, {"error": str(e)}

    selection = body.get("selection", "")
    instruction = body.get("instruction", "")
    prefix = body.get("prefix", "")
    suffix = body.get("suffix", "")
    language = body.get("language", "")

    if len(selection) > 20000:
        return 400, {"error": "Selection size exceeds 20000 characters limit"}
    if len(prefix) > 4000:
        return 400, {"error": "Prefix size exceeds 4000 characters limit"}
    if len(suffix) > 4000:
        return 400, {"error": "Suffix size exceeds 4000 characters limit"}

    system_msg = (
        "You are a precise code-editing assistant. You rewrite ONLY the user's SELECTED code per their instruction. "
        "Output ONLY the replacement code for the selection -- no markdown fences, no explanation, no surrounding code. "
        "Preserve the surrounding indentation style. If the instruction cannot apply, output the selection unchanged."
    )

    task_prompt = (
        f"We are editing a file of language: {language or 'unknown'}.\n"
        f"File Path: {rel_path}\n\n"
        f"CONTEXT BEFORE THE SELECTION (Do not modify this, only use for context):\n"
        f"---BEGIN PREFIX---\n{prefix}\n---END PREFIX---\n\n"
        f"SELECTED CODE TO REWRITE:\n"
        f"---BEGIN SELECTION---\n{selection}\n---END SELECTION---\n\n"
        f"CONTEXT AFTER THE SELECTION (Do not modify this, only use for context):\n"
        f"---BEGIN SUFFIX---\n{suffix}\n---END SUFFIX---\n\n"
        f"INSTRUCTION: {instruction}\n\n"
        f"Please output ONLY the new rewritten code that will replace the SELECTED CODE TO REWRITE. "
        f"Do not output prefix context, suffix context, explanation, or markdown fences. Output the replacement code directly."
    )

    try:
        pilot = svc.get_pilot()
        if not hasattr(pilot, "pilot") or not pilot.pilot:
            return 200, {"ok": False, "error": "No pilot driver configured"}

        resp = pilot.pilot.complete(task_prompt, system=system_msg)
        if getattr(resp, "error", None):
            return 200, {"ok": False, "error": resp.error}

        cleaned_text = svc.strip_markdown_fences(resp.text)
        return 200, {"ok": True, "edit": cleaned_text}
    except Exception as e:
        return 200, {
            "ok": False,
            "error": f"Failed during inline edit pilot execution: {str(e)}",
        }
