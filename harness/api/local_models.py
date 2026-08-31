"""Local-model HTTP route bodies (snapshot, commands, resumable events)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Union

from ..local_model_manager import LocalModelError, LocalModelManager
from ..local_models import parse_command, redact_mapping
from .sse import sse_write

JsonPayload = Union[dict, list]
EVENT_KEEPALIVE_SECONDS = 5.0


@dataclass
class LocalModelServices:
    """Explicit deps for local-model HTTP handlers (injected by server.py)."""

    manager: LocalModelManager
    cfg: Any
    rebuild_pilot_and_session: Callable[[], None]
    save_workspace_driver: Callable[[Any, str], None]
    resync_driver_after_model_curation: Callable[[], dict]


def get_local_models(svc: LocalModelServices) -> tuple[int, JsonPayload]:
    """GET /api/local-models — authoritative snapshot."""
    return 200, svc.manager.snapshot()


def post_local_models(body: dict, svc: LocalModelServices) -> tuple[int, JsonPayload]:
    """POST /api/local-models — discriminated LocalModelCommand."""
    try:
        command = parse_command(body)
    except ValueError as exc:
        return 400, {"error": str(exc)}
    manager = svc.manager
    try:
        kind = command["type"]
        if kind == "probe":
            payload = manager.probe(
                command.get("url") or "",
                api_key=command.get("api_key") or "",
                accept_lan=bool(command.get("accept_lan")),
                accept_remote=bool(command.get("accept_remote")),
            )
            return 200, payload
        if kind == "save_external":
            snapshot = manager.save_external(
                command.get("url") or "",
                api_key=command.get("api_key") or "",
                accept_lan=bool(command.get("accept_lan")),
                accept_remote=bool(command.get("accept_remote")),
                model=command.get("model") or "",
                name=command.get("name") or "",
                context_length=command.get("context_length"),
            )
            return 200, snapshot
        if kind == "install":
            return 200, manager.install(
                command.get("target") or "all",
                model_id=command.get("model_id") or "",
            )
        if kind == "cancel":
            return 200, manager.cancel(command.get("target") or "all")
        if kind == "start":
            return 200, manager.start()
        if kind == "stop":
            return 200, manager.stop()
        if kind == "restart":
            return 200, manager.restart()
        if kind == "remove":
            return 200, manager.remove(
                command.get("target") or "all",
                endpoint_id=command.get("endpoint_id") or "",
            )
        if kind == "activate":
            snapshot = manager.activate(command["spec"])
            spec = snapshot.get("active_spec") or command["spec"]
            try:
                svc.cfg.driver = spec
                svc.rebuild_pilot_and_session()
                svc.save_workspace_driver(getattr(svc.cfg, "repo", None), spec)
            except Exception as exc:
                return 500, {"error": "Activated, but the pilot could not swap: %s" % exc}
            try:
                svc.resync_driver_after_model_curation()
            except Exception:
                pass
            return 200, snapshot
        if kind == "verify_tool_calling":
            return 200, manager.verify_tool_calling(command["spec"])
        return 400, {"error": "Unknown local-model command"}
    except LocalModelError as exc:
        return 400, redact_mapping({"error": str(exc), "code": exc.code})
    except Exception as exc:
        return 500, redact_mapping({"error": str(exc)})


def get_local_model_events(
    svc: LocalModelServices,
    since: str = "0",
) -> tuple[int, JsonPayload]:
    """GET /api/local-models/events — JSON replay of progress/state events."""
    try:
        cursor = int(since or 0)
    except (TypeError, ValueError):
        cursor = 0
    events = svc.manager.events_since(cursor)
    snapshot = svc.manager.snapshot()
    return 200, {
        "ok": True,
        "events": events,
        "cursor": snapshot.get("event_cursor") or 0,
        "snapshot": snapshot,
    }


def stream_local_model_events(handler: Any, svc: LocalModelServices, since: str = "0") -> None:
    """GET /api/local-models/events?watch=1 — push-first resumable SSE."""
    try:
        cursor = int(since or 0)
    except (TypeError, ValueError):
        cursor = 0
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler._cors()
    handler.end_headers()

    def _write(payload: dict) -> bool:
        frame = ("data: %s\n\n" % json.dumps(payload)).encode("utf-8")
        return sse_write(handler.wfile, frame)

    snapshot = svc.manager.snapshot()
    if not _write({
        "kind": "snapshot",
        "cursor": int(snapshot.get("event_cursor") or 0),
        "snapshot": snapshot,
    }):
        return
    for event in svc.manager.events_since(cursor):
        if not _write(event):
            return
        cursor = max(cursor, int(event.get("cursor") or 0))
    cursor = max(cursor, int(snapshot.get("event_cursor") or 0))
    while True:
        nxt = svc.manager.wait_events_since(cursor, EVENT_KEEPALIVE_SECONDS)
        if nxt:
            snapshot = svc.manager.snapshot()
            if not _write({
                "kind": "snapshot",
                "cursor": int(snapshot.get("event_cursor") or 0),
                "snapshot": snapshot,
            }):
                return
            for event in nxt:
                if (
                    event.get("kind") == "snapshot"
                    and (event.get("data") or {}).get("reason") == "replay_unavailable"
                ):
                    cursor = max(cursor, int(event.get("cursor") or 0))
                    continue
                if not _write(event):
                    return
                cursor = max(cursor, int(event.get("cursor") or 0))
        else:
            if not _write({"kind": "keepalive", "cursor": cursor}):
                return
