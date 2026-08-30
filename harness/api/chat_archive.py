from __future__ import annotations

"""HTTP peel for Marionette chat archive ingest / prune / search / read."""

from typing import Callable, Optional
from dataclasses import dataclass

from ..chat_archive import (
    archive_status,
    ingest_all,
    prune_ingested_transcripts,
    read_archived_chat,
    search_archive,
)


@dataclass
class ChatArchiveServices:
    state_dir: Callable[[], str]
    list_sessions: Optional[Callable[[], list]] = None


def _state(svc: ChatArchiveServices) -> str:
    return (svc.state_dir() or "").strip()


def _sessions(svc: ChatArchiveServices) -> list:
    if svc.list_sessions is None:
        return []
    try:
        return list(svc.list_sessions() or [])
    except Exception:
        return []


def get_archive_status(qs: dict, svc: ChatArchiveServices) -> tuple:
    return 200, archive_status(_state(svc))


def get_archive_search(qs: dict, svc: ChatArchiveServices) -> tuple:
    query = (qs.get("q", [""])[0] or qs.get("query", [""])[0] or "").strip()
    source = (qs.get("source", [""])[0] or "").strip()
    try:
        limit = int((qs.get("limit", ["20"])[0] or "20"))
    except (TypeError, ValueError):
        limit = 20
    hits = search_archive(_state(svc), query, limit=limit, source=source)
    return 200, {"ok": True, "hits": hits}


def get_archive_read(qs: dict, svc: ChatArchiveServices) -> tuple:
    chat_id = (qs.get("chat_id", [""])[0] or qs.get("id", [""])[0] or "").strip()
    try:
        max_messages = int((qs.get("max_messages", ["200"])[0] or "200"))
    except (TypeError, ValueError):
        max_messages = 200
    payload = read_archived_chat(_state(svc), chat_id, max_messages=max_messages)
    if payload is None:
        return 404, {"ok": False, "error": "archived chat not found"}
    return 200, {"ok": True, **payload}


def post_archive_ingest(body: dict, svc: ChatArchiveServices) -> tuple:
    report = ingest_all(_state(svc), sessions=_sessions(svc))
    return 200, report


def post_archive_prune(body: dict, svc: ChatArchiveServices) -> tuple:
    report = prune_ingested_transcripts(_state(svc), sessions=_sessions(svc))
    return 200, report
