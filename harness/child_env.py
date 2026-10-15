from __future__ import annotations

"""Environment for processes that the model starts (run_command, command jobs).

Marionette mirrors provider keys and OAuth tokens into ``os.environ`` so its
drivers can read them. A model-driven shell must not inherit them: ``env``
output lands in the transcript, and any tool the model runs can read them.
Outer agent session identity (Claude Code, Codex) is removed too, so an agent
CLI that the model starts does not attach itself to the host session.

Names come from Marionette's own provider registry, not from a pattern, so
the user's own tokens (``GH_TOKEN``, ``NPM_TOKEN``) still reach the child.
``HARNESS_CHILD_ENV_PASS`` (comma list) keeps named variables.
"""

import os
import re
from typing import FrozenSet, Mapping, Optional

_STATIC_SECRET_NAMES = frozenset({
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "HARNESS_TOKEN",
    "OWNER_TOKEN",
    "WIKI_OWNER_TOKEN",
})

AGENT_IDENTITY_NAMES = frozenset({
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CODEX_THREAD_ID",
    "OMPCODE",
})

_LOCAL_ENDPOINT_KEY = re.compile(r"^LOCAL_[A-Z0-9_]*API_KEY$")


def managed_secret_names() -> FrozenSet[str]:
    """Env var names that hold credentials Marionette manages."""
    names = set(_STATIC_SECRET_NAMES)
    try:
        from .keys import BEDROCK_SECRET_FIELDS
        names.update(BEDROCK_SECRET_FIELDS)
    except Exception:
        pass
    try:
        from .providers import PROVIDERS
        for p in PROVIDERS:
            names.update(p.env_vars or ())
    except Exception:
        pass
    try:
        from .credential_pool import _PROVIDER_TO_ENV, _PROVIDER_TO_ENV_EXTRAS
        names.update(_PROVIDER_TO_ENV.values())
        for extras in _PROVIDER_TO_ENV_EXTRAS.values():
            names.update(extras)
    except Exception:
        pass
    return frozenset(n for n in names if n)


def model_child_env(base: Optional[Mapping[str, str]] = None) -> dict:
    """Copy of ``base`` (default ``os.environ``) without managed secrets or agent identity."""
    source = dict(os.environ if base is None else base)
    keep = {
        n.strip() for n in (source.get("HARNESS_CHILD_ENV_PASS") or "").split(",") if n.strip()
    }
    drop = managed_secret_names() | AGENT_IDENTITY_NAMES
    return {
        k: v for k, v in source.items()
        if k in keep or not (k in drop or _LOCAL_ENDPOINT_KEY.match(k))
    }
