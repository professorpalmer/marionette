"""Encrypted sessions: status and unlock. The master key is never returned."""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from typing import Any, Callable

from .. import seal


@dataclass
class SealServices:
    get_sessions: Callable[[], Any]


def get_seal_status(svc: SealServices) -> tuple[int, dict]:
    """GET /api/seal/status."""
    return 200, seal.status()


def post_seal_unlock(body: dict, svc: SealServices) -> tuple[int, dict]:
    """POST /api/seal/unlock — Electron main posts the master key after setup,
    recovery, or adoption of a backend it did not spawn."""
    encoded = body.pop("master_key", None)
    if not isinstance(encoded, str) or not encoded:
        return 400, {"ok": False, "error": "master_key is required"}
    try:
        key = base64.b64decode(encoded, validate=True)
        seal.unlock(key)
    except (binascii.Error, ValueError):
        return 403, {"ok": False, "code": "seal_key_rejected",
                     "error": "The key does not unlock encrypted sessions on this profile."}
    finally:
        encoded = None
    sessions = svc.get_sessions()
    if sessions is not None:
        sessions.reopen_sealed_titles()
    return 200, {"ok": True, **seal.status()}
