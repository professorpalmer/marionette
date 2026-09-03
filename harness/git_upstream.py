from __future__ import annotations

"""Git upstream lag snapshot for analysis workers.

When the live checkout trails ``origin/<branch>``, file reads reflect stale
HEAD and workers historically concluded "fix missing" even though the commit
was already fetched at ``origin/*``. Surface the lag in the analysis brief so
workers verify against the upstream tip (via read-only ``git show``) instead of
treating HEAD as the whole repository.
"""

import os
import subprocess
from typing import Optional

from .git_spawn import git_extra_args, git_spawn_env


def _git(repo: str, *args: str, timeout: float = 8.0) -> tuple[int, str]:
    try:
        env = git_spawn_env()
        env["GIT_PAGER"] = "cat"
        env["PAGER"] = "cat"
        env["GIT_TERMINAL_PROMPT"] = "0"
        proc = subprocess.run(
            ["git", "-C", repo, *git_extra_args(), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
    except Exception:
        return 1, ""
    return int(proc.returncode or 0), (proc.stdout or "").strip()


def git_upstream_brief(repo: str) -> str:
    """Return a short markdown block describing HEAD vs upstream, or ``\"\"``.

    Best-effort and never raises. Empty when ``repo`` is not a usable git checkout.
    """
    root = (repo or "").strip()
    if not root or not os.path.isdir(root):
        return ""
    code, inside = _git(root, "rev-parse", "--is-inside-work-tree")
    if code != 0 or inside.lower() != "true":
        return ""

    _, head_sha = _git(root, "rev-parse", "--short", "HEAD")
    _, head_subj = _git(root, "log", "-1", "--format=%s", "HEAD")
    _, branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if not head_sha:
        return ""

    upstream = ""
    code, up = _git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    if code == 0 and up and up != "@{upstream}":
        upstream = up
    elif branch and branch != "HEAD":
        # Common fallback when upstream isn't configured but origin/<branch> exists.
        code, tip = _git(root, "rev-parse", "--verify", f"origin/{branch}")
        if code == 0 and tip:
            upstream = f"origin/{branch}"

    lines = [
        "Git workspace state (authoritative for commit verification):",
        f"- HEAD: {head_sha}" + (f" ({head_subj})" if head_subj else ""),
    ]
    if branch:
        lines.append(f"- branch: {branch}")

    if not upstream:
        lines.append(
            "- upstream: (none configured) — file reads reflect local HEAD only; "
            "do not invent remote tip claims without evidence."
        )
        return "\n".join(lines)

    _, up_sha = _git(root, "rev-parse", "--short", upstream)
    _, up_subj = _git(root, "log", "-1", "--format=%s", upstream)
    behind = ahead = 0
    code, counts = _git(root, "rev-list", "--left-right", "--count", f"HEAD...{upstream}")
    if code == 0 and counts:
        parts = counts.split()
        if len(parts) >= 2:
            try:
                ahead = int(parts[0])
                behind = int(parts[1])
            except ValueError:
                ahead = behind = 0

    lines.append(
        f"- upstream: {upstream} @ {up_sha}"
        + (f" ({up_subj})" if up_subj else "")
    )
    if behind > 0 or ahead > 0:
        lines.append(f"- vs upstream: ahead {ahead}, behind {behind}")
    else:
        lines.append("- vs upstream: in sync (for last-fetched tip)")

    if behind > 0:
        lines.append(
            f"- IMPORTANT: local HEAD trails {upstream} by {behind} commit(s). "
            "File reads and CodeGraph reflect HEAD, NOT the remote tip. "
            "Before concluding a fix is missing, inspect the upstream tip with "
            f"read-only git (e.g. `git show {upstream}` / "
            f"`git log -1 --oneline {upstream}` / "
            f"`git show {upstream}:path/to/file`). "
            "Do not fail a verification solely because HEAD lacks a commit "
            "that exists on the upstream tip."
        )
    return "\n".join(lines)


def maybe_git_upstream_brief(repo: Optional[str]) -> str:
    """Safe wrapper used by call sites that must never raise."""
    try:
        return git_upstream_brief(repo or "")
    except Exception:
        return ""
