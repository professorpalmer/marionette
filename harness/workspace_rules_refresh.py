from __future__ import annotations

"""Live workspace-instruction refresh without restarting the session.

Baseline ``load_workspace_rules`` still injects at session init. After a
read/write/edit of an instruction file, reload that block and splice it into
the live system prompt (and the frozen append-only copy when present).
"""

import os
from typing import Any, Optional

_INSTRUCTION_BASENAMES = frozenset(("AGENTS.md", "CLAUDE.md", ".cursorrules"))


def is_instruction_path(path: str, repo: Optional[str]) -> bool:
    """True for the files ``load_workspace_rules`` already consults."""
    raw = (path or "").strip()
    if not raw:
        return False
    base = os.path.basename(raw)
    if base in _INSTRUCTION_BASENAMES:
        return True
    norm = raw.replace("\\", "/")
    if norm.endswith(".github/copilot-instructions.md") or base == "copilot-instructions.md":
        if ".github/" in norm or norm.endswith(".github/copilot-instructions.md"):
            return True
        if repo:
            try:
                rel = os.path.relpath(os.path.abspath(raw), os.path.abspath(repo))
            except Exception:
                rel = norm
            if rel.replace("\\", "/") == ".github/copilot-instructions.md":
                return True
    if "/.cursor/rules/" in ("/" + norm) and base.endswith(".md"):
        return True
    if repo:
        try:
            rel = os.path.relpath(os.path.abspath(raw), os.path.abspath(repo)).replace("\\", "/")
        except Exception:
            return False
        if rel.startswith(".cursor/rules/") and rel.endswith(".md"):
            return True
        if rel == ".github/copilot-instructions.md":
            return True
        if os.path.basename(rel) in _INSTRUCTION_BASENAMES:
            return True
    return False


def _swap_rules_block(text: str, old: str, new: str) -> str:
    body = text if isinstance(text, str) else ""
    if old and old in body:
        return body.replace(old, new, 1)
    if old and not new:
        return body
    if new and old != new:
        return body + new
    return body


def reconcile_workspace_rules(session: Any) -> bool:
    """Reload repo instruction files into the live system prompt.

    Updates ``history[0]`` and ``_frozen_system_prompt`` in place so append-only
    freeze is not rebuilt (rebuilding would double-append identity/MCP). Returns
    True when the block changed. Never raises.
    """
    try:
        from .conversation import load_workspace_rules

        repo = getattr(getattr(session, "config", None), "repo", None)
        new_block = load_workspace_rules(repo) or ""
        old_block = getattr(session, "_workspace_rules_block", None)
        if old_block is None:
            old_block = ""
        if new_block == old_block:
            return False
        history = getattr(session, "_history", None)
        if isinstance(history, list) and history:
            first = history[0]
            if isinstance(first, dict) and first.get("role") == "system":
                first["content"] = _swap_rules_block(
                    first.get("content") or "", old_block, new_block,
                )
        frozen = getattr(session, "_frozen_system_prompt", None)
        if isinstance(frozen, str):
            session._frozen_system_prompt = _swap_rules_block(frozen, old_block, new_block)
        session._workspace_rules_block = new_block
        return True
    except Exception:
        return False


def maybe_refresh_workspace_rules(session: Any, path: Optional[str]) -> bool:
    """Reconcile only when ``path`` is an instruction file we already load."""
    try:
        repo = getattr(getattr(session, "config", None), "repo", None)
        if not is_instruction_path(path or "", repo):
            return False
        return reconcile_workspace_rules(session)
    except Exception:
        return False
