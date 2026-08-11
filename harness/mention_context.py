"""Bounded @-mention expansion for chat send (files / folders / symbols / codebase).

Folder mentions must fail closed outside the workspace and never dump an
unbounded recursive tree into the model context.

File mentions use the same honesty seam: truncation, budget skips, and IO
failures are surfaced in the resolved block (never silent-dropped).

``@codebase`` (optional ``@codebase:query``) pins repo-wide CodeGraph context
and uses the same skip/failure honesty seam — never silent bare-token fallthrough.

Spaced paths use a quoted token convention shared with the composer:
    ``@"path with spaces.ts"`` / ``@folder:"my folder"``.
"""

from __future__ import annotations

import os
import re
from typing import Optional

# Same noise dirs the workspace file listing skips.
_SKIP_DIRS = frozenset({
    ".git", "node_modules", ".venv", ".codegraph", "dist", "build",
    ".pytest_cache", "__pycache__", ".mypy_cache", ".ruff_cache", ".idea",
    ".vscode", "venv", ".next", "coverage", ".hermes", "release",
    "backend-dist",
})

DEFAULT_FOLDER_ENTRY_CAP = 40
FILE_PER_MENTION_CAP = 50 * 1024
MENTION_TOTAL_BUDGET = 150 * 1024
SYMBOL_SNIPPET_CAP = 8 * 1024

# @codebase | @codebase:query | @"quoted path" | @folder:"…" | @symbol:"…" | @bare/path
_MENTION_TOKEN_RE = re.compile(
    r"@"
    r"(?:"
    r"(?P<codebase>codebase)(?::(?:"
    r'"(?P<codebase_quoted>[^"]+)"'
    r"|"
    r"(?P<codebase_bare>[a-zA-Z0-9_\-\.\/]+)"
    r"))?(?![a-zA-Z0-9_\-\.\/:])"
    r"|"
    r"(?P<prefix>(?:folder|symbol):)?"
    r"(?:"
    r'"(?P<quoted>[^"]+)"'
    r"|"
    r"(?P<bare>[a-zA-Z0-9_\-\.\/:]+)"
    r")"
    r")"
)


def extract_mention_tokens(message: str) -> list[str]:
    """Parse @-mention tokens, supporting quoted paths that contain spaces.

    Returns tokens in the same shape the resolver already expects:
    ``path/to/file``, ``folder:rel``, ``symbol:Name``, ``codebase``,
    ``codebase:query`` (quotes stripped).
    """
    if not message:
        return []
    out: list[str] = []
    for match in _MENTION_TOKEN_RE.finditer(message):
        if match.group("codebase") is not None:
            query = match.group("codebase_quoted")
            if query is None:
                query = match.group("codebase_bare") or ""
            out.append(f"codebase:{query}" if query else "codebase")
            continue
        prefix = match.group("prefix") or ""
        body = match.group("quoted")
        if body is None:
            body = match.group("bare") or ""
        if not body:
            continue
        out.append(f"{prefix}{body}")
    return out


def is_codebase_mention(token: str) -> bool:
    """True when token is ``codebase`` or ``codebase:<query>``."""
    return token == "codebase" or token.startswith("codebase:")


def codebase_mention_query(token: str) -> str:
    """Optional filter from a codebase mention token (empty when bare)."""
    if token.startswith("codebase:"):
        return token[len("codebase:"):]
    return ""


def codebase_mention_label(token: str) -> str:
    """Display label for honesty blocks (``@codebase`` / ``@codebase:query``)."""
    query = codebase_mention_query(token)
    return f"@codebase:{query}" if query else "@codebase"


def folder_entry_cap(env: Optional[dict] = None) -> int:
    """Max relative paths listed for a single @folder mention."""
    src = env if env is not None else os.environ
    try:
        cap = int(src.get("HARNESS_FOLDER_MENTION_CAP", str(DEFAULT_FOLDER_ENTRY_CAP)) or DEFAULT_FOLDER_ENTRY_CAP)
    except (TypeError, ValueError):
        cap = DEFAULT_FOLDER_ENTRY_CAP
    return max(1, min(cap, 500))


def resolve_repo_dir(repo: str, rel_or_token: str) -> Optional[str]:
    """Return realpath of a workspace directory, or None if outside / missing."""
    if not repo or not rel_or_token:
        return None
    # Strip optional folder: prefix used by the composer token.
    token = rel_or_token[7:] if rel_or_token.startswith("folder:") else rel_or_token
    token = token.strip().strip("/")
    if not token or ".." in token.split("/"):
        return None
    try:
        repo_real = os.path.realpath(repo)
        full_real = os.path.realpath(os.path.join(repo_real, token))
        common = os.path.commonpath([repo_real, full_real])
        if common != repo_real:
            return None
        if not os.path.isdir(full_real):
            return None
        return full_real
    except Exception:
        return None


def list_folder_entries(
    repo: str,
    folder_abs: str,
    *,
    entry_cap: Optional[int] = None,
) -> tuple[list[str], int, bool]:
    """List relative file paths under folder_abs, sorted then capped.

    Returns (entries, total_uncapped, truncated).
    """
    cap = entry_cap if entry_cap is not None else folder_entry_cap()
    repo_real = os.path.realpath(repo)
    folder_real = os.path.realpath(folder_abs)
    found: list[str] = []
    try:
        for root, dirs, files in os.walk(folder_real):
            dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS)
            for name in files:
                full = os.path.join(root, name)
                rel = os.path.relpath(full, repo_real).replace(os.sep, "/")
                if rel.startswith(".."):
                    continue
                found.append(rel)
    except OSError:
        return [], 0, False
    found.sort()
    total = len(found)
    truncated = total > cap
    return found[:cap], total, truncated


def format_folder_mention_block(
    folder_token: str,
    entries: list[str],
    *,
    total: int,
    truncated: bool,
) -> str:
    """Honest listing block: paths plus an explicit truncation note when capped."""
    display = folder_token[7:] if folder_token.startswith("folder:") else folder_token
    lines = [f"--- Folder: {display} ---"]
    if not entries and total == 0:
        lines.append("(empty directory)")
    else:
        lines.extend(entries)
        if truncated:
            lines.append(
                f"... truncated: showing {len(entries)} of {total} files "
                f"(cap {len(entries)}; use a narrower @folder or @file mentions)"
            )
        else:
            lines.append(f"({total} file{'s' if total != 1 else ''})")
    return "\n".join(lines) + "\n"


def expand_folder_mention(
    repo: str,
    token: str,
    *,
    entry_cap: Optional[int] = None,
) -> Optional[str]:
    """Expand an @folder token into a bounded listing, or None if rejected."""
    folder_abs = resolve_repo_dir(repo, token)
    if not folder_abs:
        return None
    entries, total, truncated = list_folder_entries(
        repo, folder_abs, entry_cap=entry_cap,
    )
    return format_folder_mention_block(
        token if token.startswith("folder:") else f"folder:{token}",
        entries,
        total=total,
        truncated=truncated,
    )


def format_file_mention_block(
    token: str,
    content: str,
    *,
    file_size: int,
    bytes_read: int,
    truncated: bool,
) -> str:
    """Honest file block with an explicit truncation note when capped."""
    lines = [f"--- File: {token} ---", content.rstrip("\n")]
    if truncated:
        lines.append(
            f"... truncated: showing first {bytes_read} of {file_size} bytes "
            f"(50KB per-file cap)"
        )
    return "\n".join(lines) + "\n"


def format_file_mention_skip(token: str, *, reason: str) -> str:
    """Honesty note when a file mention is not attached to context."""
    return f"--- File: {token} ---\n... skipped: {reason}\n"


def format_file_mention_failure(token: str, *, error: str) -> str:
    """Honesty note when reading a mentioned file fails."""
    return f"--- File: {token} ---\n... failed to read: {error}\n"


def read_file_mention(
    file_path: str,
    token: str,
    *,
    total_size: int,
    per_file_cap: int = FILE_PER_MENTION_CAP,
    total_budget: int = MENTION_TOTAL_BUDGET,
) -> tuple[str, int]:
    """Read a mentioned file into an honest context block.

    Returns (block, added_bytes). Confinement is the caller's job; this only
    handles IO / caps / budget honesty. ``added_bytes`` is 0 for skip/failure
    notes so later mentions still get a chance at remaining budget.
    """
    budget_left = total_budget - total_size
    if budget_left <= 0:
        return format_file_mention_skip(
            token,
            reason="mention context budget exhausted (150KB total across @-mentions)",
        ), 0
    try:
        file_size = os.path.getsize(file_path)
        read_cap = min(per_file_cap, budget_left)
        with open(file_path, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read(read_cap)
        bytes_read = len(content.encode("utf-8"))
        truncated = file_size > read_cap
        if not truncated:
            return format_file_mention_block(
                token,
                content,
                file_size=file_size,
                bytes_read=bytes_read,
                truncated=False,
            ), bytes_read
        if read_cap < per_file_cap:
            reason = "mention context budget"
        else:
            reason = "50KB per-file cap"
        block = (
            f"--- File: {token} ---\n"
            f"{content.rstrip(chr(10))}\n"
            f"... truncated: showing first {bytes_read} of {file_size} bytes "
            f"({reason})\n"
        )
        return block, bytes_read
    except OSError as exc:
        return format_file_mention_failure(token, error=str(exc) or type(exc).__name__), 0
    except Exception as exc:
        return format_file_mention_failure(token, error=str(exc) or type(exc).__name__), 0


def format_symbol_mention_block(
    name: str,
    file_path: str,
    start_line: int,
    snippet: str,
    *,
    truncated: bool,
    original_bytes: int,
) -> str:
    """Honest symbol snippet block."""
    lines = [f"--- Symbol: {name} ({file_path}:{start_line}) ---", snippet.rstrip("\n")]
    if truncated:
        lines.append(
            f"... truncated: showing first {SYMBOL_SNIPPET_CAP} of {original_bytes} bytes "
            f"(8KB per-symbol cap)"
        )
    return "\n".join(lines) + "\n"


def format_symbol_mention_skip(symbol_name: str, *, reason: str) -> str:
    return f"--- Symbol: {symbol_name} ---\n... skipped: {reason}\n"


def format_symbol_mention_failure(symbol_name: str, *, error: str) -> str:
    return f"--- Symbol: {symbol_name} ---\n... failed to read: {error}\n"


def format_codebase_mention_block(token: str, content: str) -> str:
    """Honest CodeGraph context block for an @codebase mention."""
    label = codebase_mention_label(token)
    body = (content or "").rstrip("\n")
    return f"--- Codebase: {label} ---\n{body}\n"


def format_codebase_mention_skip(token: str, *, reason: str) -> str:
    """Honesty note when @codebase context is not attached."""
    label = codebase_mention_label(token)
    return f"--- Codebase: {label} ---\n... skipped: {reason}\n"


def format_codebase_mention_failure(token: str, *, error: str) -> str:
    """Honesty note when @codebase CodeGraph resolution fails."""
    label = codebase_mention_label(token)
    return f"--- Codebase: {label} ---\n... failed to resolve: {error}\n"


def expand_codebase_mention(
    repo: str,
    token: str,
    *,
    task_fallback: str = "",
) -> str:
    """Resolve @codebase via CodeGraph context with skip/failure honesty.

    Never returns empty: operators always see success, skip, or failure.
    """
    if not is_codebase_mention(token):
        return format_codebase_mention_failure(
            token or "codebase",
            error="not a codebase mention token",
        )
    if not repo or not os.path.isdir(repo):
        return format_codebase_mention_skip(
            token,
            reason="no workspace repository configured",
        )
    query = codebase_mention_query(token)
    task = query or (task_fallback or "").strip() or "repository structure"
    try:
        import puppetmaster.codegraph as cg

        if not cg.codegraph_available():
            return format_codebase_mention_skip(
                token,
                reason="CodeGraph unavailable",
            )
        if not cg.codegraph_ready(repo):
            return format_codebase_mention_skip(
                token,
                reason="CodeGraph index not ready (run codegraph init / wait for indexing)",
            )
        cg_slice = cg.codegraph_context(task=task, cwd=repo)
        if not cg_slice:
            return format_codebase_mention_skip(
                token,
                reason="CodeGraph returned no context for this query",
            )
        section = cg.codegraph_prompt_section(cg_slice).strip()
        if not section:
            return format_codebase_mention_skip(
                token,
                reason="CodeGraph returned no context for this query",
            )
        return format_codebase_mention_block(token, section)
    except Exception as exc:
        return format_codebase_mention_failure(
            token,
            error=str(exc) or type(exc).__name__,
        )
