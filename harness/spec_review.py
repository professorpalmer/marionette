"""A review of the turn's work from a fresh context: the request and the diff only.

The pilot's own acceptance check tests its own reading of the request, so a
misread passes it. A second model call that sees only the request text and
the workspace diff has no such anchor. It lists each requirement that the diff
does not meet or meets in a different way, and the pilot checks each point.
``HARNESS_REQUIREMENT_REVIEW=1`` turns it on for the second check round.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from typing import Any, Optional

DIFF_LIMIT = 80_000
GIT_TIMEOUT = 30

REVIEW_SYSTEM = (
    "You review a code change against the request that asked for it. You "
    "see only the request and the diff. Hidden acceptance tests were written "
    "from the request text. They call the public API exactly as the request "
    "names it, and each test checks one sentence of the request.\n"
    "Read the request sentence by sentence. For each requirement, find the "
    "code in the diff that meets it. Report each requirement that the diff "
    "does not meet, meets only in part, or meets in a different way: wrong "
    "names, arguments or keyword arguments; missing defaults; wrong values "
    "for empty, missing or unnamed cases; different strings, formats or "
    "errors; a surface that the request names but the diff does not change "
    "(CLI, config loading, constructors, serializers, exports, docs examples).\n"
    "For each point, quote the request sentence, say what the diff does, and "
    "give one concrete check (a call and its expected result). Do not report "
    "style. Do not report a point that the diff meets. If the diff meets every "
    "requirement, reply with the single line: NO GAPS."
)

REVIEW_NOTE = (
    "An independent reviewer read only the request and your diff. It reports "
    "the possible gaps below. Check each point against the request text. For "
    "each real gap, fix the code and run the check. If a point is wrong, skip "
    "it. Use throwaway scripts, and do not leave new files that the request "
    "did not ask for. Then give the final answer.\n\n"
)


def review_enabled() -> bool:
    return os.environ.get("HARNESS_REQUIREMENT_REVIEW", "").strip().lower() in {"1", "true", "on", "yes"}


def _git(repo: str, args: list, env: Optional[dict] = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env, timeout=GIT_TIMEOUT)


def turn_base(session: Any) -> str:
    """HEAD when the turn starts, so the review sees work the pilot committed. "" when the review is off."""
    repo = getattr(getattr(session, "config", None), "repo", "") or ""
    if not repo or not review_enabled():
        return ""
    try:
        res = _git(repo, ["rev-parse", "--verify", "HEAD"])
    except (OSError, subprocess.SubprocessError):
        return ""
    return res.stdout.strip() if res.returncode == 0 else ""


def workspace_diff(repo: str, base: str = "HEAD", limit: int = DIFF_LIMIT) -> str:
    """The diff from ``base`` to the working tree, new files included. The real index is not changed."""
    try:
        top = _git(repo, ["rev-parse", "--git-dir"])
    except (OSError, subprocess.SubprocessError):
        return ""
    if top.returncode != 0:
        return ""
    index = os.path.join(repo, top.stdout.strip(), f"index.review.{uuid.uuid4().hex}")
    env = dict(os.environ, GIT_INDEX_FILE=index)
    try:
        if _git(repo, ["read-tree", "HEAD"], env).returncode != 0:
            return ""
        _git(repo, ["add", "-A"], env)
        out = _git(repo, ["diff", "--cached", base, "--no-color"], env).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    finally:
        try:
            os.remove(index)
        except OSError:
            pass
    if len(out) > limit:
        out = out[:limit] + f"\n[diff cut at {limit} characters]\n"
    return out


def independent_review(pilot: Any, request: str, diff: str) -> str:
    """The reviewer's findings, or "" when the call fails or finds no gaps."""
    if pilot is None or not hasattr(pilot, "chat") or not diff.strip():
        return ""
    from pmharness.drivers.metering import attribution

    prompt = f"<request>\n{request}\n</request>\n\n<diff>\n{diff}\n</diff>"
    try:
        with attribution(purpose="requirement_review"):
            resp = pilot.chat([{"role": "user", "content": prompt}], system=REVIEW_SYSTEM)
    except Exception:
        return ""
    if resp is None or getattr(resp, "error", None):
        return ""
    text = (getattr(resp, "text", "") or "").strip()
    if not text or text.upper().startswith("NO GAPS"):
        return ""
    return text


def review_note(session: Any, request: str) -> str:
    """The note for the review round, or "" when there is nothing to send."""
    repo = getattr(getattr(session, "config", None), "repo", "") or ""
    if not repo:
        return ""
    diff = workspace_diff(repo, getattr(session, "_review_base", "") or "HEAD")
    findings = independent_review(getattr(session, "pilot", None), request, diff)
    return REVIEW_NOTE + findings if findings else ""
