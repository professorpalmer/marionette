"""Slash-command HTTP route bodies (peeled from ``harness.server``)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Union


@dataclass
class CommandsServices:
    """Explicit deps for command HTTP handlers."""

    commands: Any
    cfg: Any


JsonPayload = Union[dict, list]


def post_commands_render(body: dict, svc: CommandsServices) -> tuple[int, JsonPayload]:
    """POST /api/commands/render."""
    name = (body.get("name") or "").strip()
    args = body.get("args", "")
    if not name:
        return 400, {"error": "Missing name parameter"}
    rendered = svc.commands.render(name, args, repo=svc.cfg.repo)
    if rendered is None:
        return 404, {"error": "unknown command"}
    return 200, {"name": name, "prompt": rendered}


def get_commands(repo_override: Optional[str], svc: CommandsServices) -> tuple[int, JsonPayload]:
    """GET /api/commands."""
    repo = (repo_override or "").strip() or svc.cfg.repo
    cmds = svc.commands.list(repo=repo)
    return 200, {
        "commands": [
            {"name": c.name, "description": c.description, "scope": c.scope}
            for c in cmds
        ]
    }
