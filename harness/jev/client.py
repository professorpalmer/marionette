from __future__ import annotations

"""OpenRouter Decisions client. Parse at the boundary; never raise to callers."""

import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

from .questions import ENDPOINT, MODEL, TIMEOUT_SECONDS


def resolve_openrouter_key() -> str:
    """Return a usable OpenRouter key or empty. Never logs the secret.

    Prefer the Marionette state store over ``OPENROUTER_API_KEY``. Cursor and
    shell env often keep a leftover export that 401s on Decisions, while
    ``~/.pmharness/state/keys.json`` is the key Settings actually uses.
    Inspect fixtures stay env-only so tests cannot read a developer key.
    """
    try:
        from harness.keys import inspect_mode

        if inspect_mode():
            return (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    except Exception:
        pass
    paths = []
    state_dir = os.environ.get("HARNESS_STATE_DIR")
    if state_dir:
        paths.append(os.path.join(state_dir, "keys.json"))
    else:
        home = os.path.expanduser("~")
        paths.append(os.path.join(home, ".pmharness", "state", "keys.json"))
        paths.append(os.path.join(home, ".pmharness", "keys.json"))
    seen = set()
    for path in paths:
        try:
            abs_path = os.path.abspath(path)
        except Exception:
            continue
        if abs_path in seen:
            continue
        seen.add(abs_path)
        try:
            data = json.loads(open(path, encoding="utf-8").read())
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        value = data.get("openrouter")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return (os.environ.get("OPENROUTER_API_KEY") or "").strip()


def decide(state: Any, questions: Dict[str, Any], key: str = "") -> Optional[dict]:
    """POST one System One request. None on any failure."""
    token = (key or resolve_openrouter_key()).strip()
    if not token or not questions:
        return None
    payload = {"model": MODEL, "state": state, "questions": questions}
    req = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": "Bearer %s" % token,
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/professorpalmer/marionette",
            "X-OpenRouter-Title": "Marionette Jev",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, OSError):
        return None
    except Exception:
        return None
    if not isinstance(body, dict) or not isinstance(body.get("answers"), dict):
        return None
    return body
