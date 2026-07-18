from __future__ import annotations

"""Hard dispatch guards for run_implement / run_parallel.

The harness's product edge is fanning hard work across workers. A single
``run_implement`` that asks one worker to REWRITE a 700+ line file is the
anti-pattern we exist to prevent -- refuse it at the tool layer and tell the
pilot to split via ``run_parallel``.
"""

import os
import re
import tempfile
from typing import Optional

# Default ceiling: one worker may own this many lines of a single-file rewrite
# before the harness refuses and forces a multi-worker split.
_DEFAULT_MAX_LINES = 250

# Goals that clearly ask for a full-file rewrite / regenerate, not a small edit.
_REWRITE_RE = re.compile(
    r"\b(?:"
    r"rewrite|re-?write|regenerate|re-?generate|"
    r"complete\s+rewrite|full\s+rewrite|"
    r"replace\s+(?:the\s+)?(?:entire|whole)\s+(?:file|contents?)|"
    r"from\s+scratch"
    r")\b",
    re.IGNORECASE,
)

# Path-like tokens: posix or windows, with a common source / asset extension.
# Longer extensions MUST precede shorter prefixes (html before h, mm before m).
_PATH_RE = re.compile(
    r"(?:"
    r"[A-Za-z]:[\\/][^\s'\"`|;<>]+"  # Windows abs
    r"|"
    r"(?:\.{0,2}[\\/])?[A-Za-z0-9_.-]+(?:[\\/][A-Za-z0-9_.-]+)+"  # rel multi-seg
    r"|"
    r"[A-Za-z0-9_.-]+\.(?:tsx|ts|jsx|js|py|go|rs|java|rb|php|lua|cpp|cs|swift|kt|"
    r"html?|css|scss|less|md|json|toml|ya?ml|vue|svelte|txt|sql|ps1|bat|xml|svg|"
    r"mm|m|c|h)"
    r")"
)


def max_single_file_rewrite_lines() -> int:
    raw = (os.environ.get("HARNESS_IMPLEMENT_MAX_FILE_LINES", "") or "").strip()
    if not raw:
        return _DEFAULT_MAX_LINES
    try:
        return max(50, int(raw))
    except (TypeError, ValueError):
        return _DEFAULT_MAX_LINES


def extract_goal_paths(goal: str) -> list[str]:
    """Best-effort path tokens from a worker goal string (deduped, order preserved)."""
    found: list[str] = []
    seen: set[str] = set()
    for m in _PATH_RE.finditer(goal or ""):
        raw = m.group(0).strip().strip("'\"`")
        # Drop trailing punctuation the regex may have absorbed.
        raw = raw.rstrip(").,;:]}")
        if not raw:
            continue
        key = raw.replace("\\", "/").lower()
        if key in seen:
            continue
        seen.add(key)
        found.append(raw)
    return found


def resolve_repo_file(repo: str, rel_or_abs: str) -> Optional[str]:
    """Return an absolute path under ``repo`` when the candidate exists as a file."""
    if not repo or not rel_or_abs:
        return None
    repo_abs = os.path.abspath(repo)
    cand = rel_or_abs
    if os.path.isabs(cand):
        path = os.path.abspath(cand)
    else:
        path = os.path.abspath(os.path.join(repo_abs, cand.replace("\\", os.sep)))
    try:
        # Must stay inside the repo (no .. escapes).
        common = os.path.commonpath([repo_abs, path])
        if os.path.normcase(common) != os.path.normcase(repo_abs):
            return None
    except ValueError:
        return None
    if os.path.isfile(path):
        return path
    return None


def file_line_count(path: str) -> int:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return sum(1 for _ in f)
    except Exception:
        return 0


def check_oversized_single_file_rewrite(goal: str, repo: str) -> Optional[str]:
    """Return a refusal message when ``goal`` asks one worker to rewrite a huge file.

    Returns ``None`` when the goal is fine to dispatch as-is. Disable with
    ``HARNESS_IMPLEMENT_FANOUT_GUARD=0``.
    """
    if (os.environ.get("HARNESS_IMPLEMENT_FANOUT_GUARD", "1") or "1").strip() in (
        "0", "false", "no", "off",
    ):
        return None
    goal = (goal or "").strip()
    if not goal or not repo:
        return None
    if not _REWRITE_RE.search(goal):
        return None

    # Sectioned goals ("lines 1-200", "part 2 of 4") are already fan-out shaped.
    if re.search(r"\b(?:lines?\s+\d+\s*[-–—]\s*\d+|part\s+\d+\s+of\s+\d+|section\s+\d+)\b",
                 goal, re.IGNORECASE):
        return None

    ceiling = max_single_file_rewrite_lines()
    oversized: list[tuple[str, int]] = []
    for token in extract_goal_paths(goal):
        path = resolve_repo_file(repo, token)
        if not path:
            continue
        lines = file_line_count(path)
        if lines > ceiling:
            try:
                rel = os.path.relpath(path, os.path.abspath(repo)).replace("\\", "/")
            except Exception:
                rel = path
            oversized.append((rel, lines))

    if not oversized:
        return None

    parts = ", ".join(f"{rel} ({n} lines)" for rel, n in oversized)
    return (
        f"REFUSED: single-worker rewrite of oversized file(s): {parts}. "
        f"Harness ceiling is {ceiling} lines per worker "
        f"(HARNESS_IMPLEMENT_MAX_FILE_LINES). Split into run_parallel goals "
        f"that each own a disjoint section (e.g. lines 1-{ceiling}, "
        f"{ceiling + 1}-{ceiling * 2}, ...) or supporting files only. "
        f"This is the fan-out the harness exists to enforce -- do not re-issue "
        f"the same whole-file rewrite on one worker."
    )


def _git_work_tree(path: str) -> bool:
    """True when ``path`` is (or is inside) a git work tree."""
    if not path:
        return False
    try:
        abs_path = os.path.abspath(path)
    except Exception:
        return False
    if not os.path.isdir(abs_path):
        return False
    git_marker = os.path.join(abs_path, ".git")
    if os.path.isdir(git_marker) or os.path.isfile(git_marker):
        return True
    try:
        import subprocess
        r = subprocess.run(
            ["git", "-C", abs_path, "rev-parse", "--is-inside-work-tree"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
        return r.returncode == 0 and (r.stdout or "").strip() == "true"
    except Exception:
        return False


def is_home_or_ephemeral_workspace(path: str) -> bool:
    """True for Marionette Home / harness state trees (not a real project)."""
    if not path:
        return False
    try:
        norm = os.path.normcase(os.path.abspath(path)).replace("\\", "/")
    except Exception:
        return False
    # ~/.pmharness/home or {HARNESS_STATE_DIR}/home
    if norm.rstrip("/").endswith("/home"):
        if "/.pmharness/" in norm or "/pmharness/" in norm:
            return True
        state = (os.environ.get("HARNESS_STATE_DIR") or "").strip()
        if state:
            try:
                home = os.path.normcase(
                    os.path.abspath(os.path.join(state, "home"))
                ).replace("\\", "/")
                if norm.rstrip("/") == home.rstrip("/"):
                    return True
            except Exception:
                pass
    return False


_ANALYSIS_ONLY_RE = re.compile(
    r"\b(?:"
    r"compar(?:e|ison)|diff\b|list\s+(?:all\s+)?files|"
    r"byte-?level|identical|report\s+which|"
    r"find\s+where|directory\s+tree|os\.walk|filecmp|"
    r"read-?only\s+analysis|investigate|audit\b"
    r")\b",
    re.IGNORECASE,
)

_EDIT_INTENT_RE = re.compile(
    r"\b(?:"
    r"edit|fix|patch|implement|rewrite|refactor|migrate|"
    r"delete|remove|create|add\s+file|write\s+file|apply\b"
    r")\b",
    re.IGNORECASE,
)


def looks_like_analysis_only_goal(goal: str) -> bool:
    """Heuristic: goal asks for compare/list/report, not an edit."""
    g = (goal or "").strip()
    if not g:
        return False
    if _EDIT_INTENT_RE.search(g):
        return False
    return bool(_ANALYSIS_ONLY_RE.search(g))


def check_implement_workspace(repo: str, *, goal: str = "") -> Optional[str]:
    """Refuse run_implement/run_parallel when the target cannot host a worktree.

    Hermes-style soft fail: return a clear tool error the pilot can act on in
    the SAME turn (use run_command, pass repo=, Open Project). Never dispatch a
    background worker that dies in <1s with ``not a git repo``.
    """
    if (os.environ.get("HARNESS_IMPLEMENT_GIT_GUARD", "1") or "1").strip().lower() in (
        "0", "false", "no", "off",
    ):
        return None
    repo = (repo or "").strip()
    if not repo:
        tmp_dir = tempfile.gettempdir()
        return (
            "REFUSED: no workspace directory is open. Open a Project (a git "
            "checkout) in Marionette, or pass repo=<absolute path to a git "
            f"repo>. For ad-hoc filesystem tasks (clone/compare under {tmp_dir}), "
            "use run_command instead of run_implement."
        )
    if not os.path.isdir(repo):
        return (
            f"REFUSED: workspace {repo} is not an existing directory. Pass "
            f"repo=<absolute path to a git checkout>, or use run_command."
        )
    if is_home_or_ephemeral_workspace(repo):
        hint = ""
        if looks_like_analysis_only_goal(goal):
            hint = (
                " This goal looks analysis-only (compare/list/report) — "
                "prefer run_command against the clone path, or Open Project "
                "on that clone."
            )
        return (
            f"REFUSED: workspace {repo} is Marionette Home (not a project "
            f"git repo). run_implement needs an isolated git worktree."
            f"{hint} Open the real project, or pass "
            f"repo=<absolute path to the git clone>, or use run_command."
        )
    if not _git_work_tree(repo):
        hint = ""
        if looks_like_analysis_only_goal(goal):
            hint = (
                " For compare/list/report tasks use run_command directly; "
                "do not dispatch run_implement."
            )
        return (
            f"REFUSED: {repo} is not a git repository, so run_implement "
            f"cannot create a worktree."
            f"{hint} Pass repo=<absolute path to a git checkout>, Open "
            f"Project on that path, or use run_command / run_swarm with a "
            f"git cwd."
        )
    return None


def is_preflight_worker_error(error: str) -> bool:
    """True for setup failures that never started real edit work."""
    e = (error or "").strip().lower()
    if not e:
        return False
    needles = (
        "not a git repo",
        "not a valid git repository",
        "no git repository",
        "refused:",
        "no workspace directory",
        "could not select a model",
        "no provider key",
        "agentic unavailable",
    )
    return any(n in e for n in needles)
