"""Authenticated metaharness scoring / leader-election HTTP handlers."""
from __future__ import annotations

from typing import Any, Union

from ..metaharness import ScoreError, get_latch, score_report

JsonPayload = Union[dict, list]


def get_metaharness_status() -> tuple[int, JsonPayload]:
    """GET /api/metaharness/status — current leader latch."""
    return 200, get_latch().status()


def post_metaharness_score(body: dict) -> tuple[int, JsonPayload]:
    """POST /api/metaharness/score — report a peer score (also a heartbeat)."""
    if not isinstance(body, dict):
        return 400, {"ok": False, "error": "body must be an object"}
    try:
        value = score_report(body)
        snap = get_latch().report_score(body.get("peer_id") or body.get("id"), value)
    except ScoreError as exc:
        return 400, {"ok": False, "error": str(exc)}
    return 200, snap


def post_metaharness_heartbeat(body: dict) -> tuple[int, JsonPayload]:
    """POST /api/metaharness/heartbeat — refresh liveness, optional new score."""
    if not isinstance(body, dict):
        return 400, {"ok": False, "error": "body must be an object"}
    peer_id = body.get("peer_id") or body.get("id")
    score: Any = None
    if "score" in body or "ok" in body or "success" in body:
        try:
            score = score_report(body)
        except ScoreError as exc:
            return 400, {"ok": False, "error": str(exc)}
    try:
        snap = get_latch().heartbeat(peer_id, score)
    except ScoreError as exc:
        return 400, {"ok": False, "error": str(exc)}
    return 200, snap
