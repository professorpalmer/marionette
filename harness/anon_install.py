"""Local anonymous install id. Never a network identifier."""
from __future__ import annotations

import json
import os
import uuid
from typing import Optional

from .secure_files import restrict_to_owner

_FILENAME = "anon-id.json"


def anon_id_path(state_dir: Optional[str] = None) -> str:
    root = (state_dir or os.environ.get("HARNESS_STATE_DIR") or "").strip()
    if not root:
        root = os.path.join(os.path.expanduser("~"), ".pmharness")
    return os.path.join(os.path.expanduser(root), _FILENAME)


def load_or_create_anon_id(state_dir: Optional[str] = None) -> str:
    path = anon_id_path(state_dir)
    try:
        raw = json.loads(open(path, encoding="utf-8").read())
        value = str((raw or {}).get("install_id") or "").strip()
        uuid.UUID(value)
        return value
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    value = str(uuid.uuid4())
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"install_id": value}, handle)
    restrict_to_owner(path)
    return value
