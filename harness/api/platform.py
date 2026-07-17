"""Platform adapter toggle + Bedrock BYOK HTTP bodies (peeled from server)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Callable, Union


@dataclass
class PlatformServices:
    """Explicit deps for platform HTTP handlers."""

    get_platform_json_path: Callable[[], str]
    write_platform_json_atomic: Callable[[str, dict], None]
    get_platform_adapters: Callable[[], dict]
    diag: Callable[..., Any]


JsonPayload = Union[dict, list]

_ALLOWED_ADAPTERS = (
    "agentic",
    "cursor",
    "hermes",
    "claude-code",
    "codex",
    "openai",
)


def post_platform(body: dict, svc: PlatformServices) -> tuple[int, JsonPayload]:
    """POST /api/platform."""
    name = body.get("name")
    enabled = body.get("enabled")
    if name not in _ALLOWED_ADAPTERS:
        return 400, {"error": f"Unknown adapter: {name}"}
    if not isinstance(enabled, bool):
        return 400, {"error": "enabled must be a boolean"}

    path_file = svc.get_platform_json_path()
    pdata = {}
    if os.path.exists(path_file):
        try:
            with open(path_file, "r", encoding="utf-8") as f:
                pdata = json.load(f)
        except Exception as e:
            svc.diag("server.platform_toggle_load", e)
    if not isinstance(pdata, dict):
        pdata = {}
    if "disabled" not in pdata or not isinstance(pdata["disabled"], list):
        pdata["disabled"] = []

    disabled_list = pdata["disabled"]
    if enabled:
        pdata["disabled"] = [x for x in disabled_list if x != name]
    else:
        if name not in disabled_list:
            pdata["disabled"] = disabled_list + [name]

    try:
        svc.write_platform_json_atomic(path_file, pdata)
    except Exception as e:
        return 500, {"error": f"Failed to save platform.json: {str(e)}"}

    return 200, svc.get_platform_adapters()


def get_platform(svc: PlatformServices) -> tuple[int, JsonPayload]:
    """GET /api/platform."""
    return 200, svc.get_platform_adapters()


def post_bedrock(body: dict) -> tuple[int, JsonPayload]:
    """POST /api/bedrock."""
    from ..keys import clear_bedrock_credentials, set_bedrock_credentials

    action = str(body.get("action", "")).strip().lower()
    if action == "clear" or body.get("clear") is True:
        res = clear_bedrock_credentials()
    else:
        fields = body.get("bedrock") if isinstance(body.get("bedrock"), dict) else body
        allowed = (
            "AWS_BEARER_TOKEN_BEDROCK",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "AWS_REGION",
            "BEDROCK_REGION",
            "BEDROCK_MODEL_ID",
        )
        patch = {k: fields.get(k) for k in allowed if k in fields}
        if not patch:
            return 400, {
                "error": "bedrock credentials required "
                "(AWS_BEARER_TOKEN_BEDROCK or access key + secret)"
            }
        res = set_bedrock_credentials(patch)
    from ..auto_registry import sync_agentic_registry_safe
    sync_agentic_registry_safe()
    return 200, {"ok": True, **res}


def get_bedrock() -> tuple[int, JsonPayload]:
    """GET /api/bedrock."""
    from ..keys import get_bedrock_status
    return 200, get_bedrock_status()
