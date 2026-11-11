"""Refuse tool calls that fetch the answer key of a benchmark task.

Benchmarks such as DeepSWE and Terminal-Bench publish each task with its
reference solution, hidden tests and grader, and many mirrors copy them.
When a spec is hard, a model searches the web for the task and downloads
those files. That turns an evaluation into a lookup. On the 2026-11-11
leaderboard run this happened in 19 of 131 DeepSWE high-effort trials.

The check reads the URLs in a ``web_fetch`` target, a shell command or
Python code. It refuses a URL whose path has the shape of answer material:
a task folder's ``solution/`` or ``tests/``, a ``solution.patch``,
``test.patch``, ``model.patch``, ``grader.py`` or ``ctrf.json``, or stored
trial artifacts. A command that names a ``/tasks/<id>`` URL and also names
solution or test files (a base URL plus a file loop) is refused too.
``HARNESS_ANSWER_KEY_GUARD=0`` turns the check off.
"""

from __future__ import annotations

import os
import re
from typing import Any, Optional

_URL = re.compile(r"https?://[^\s'\"<>|;`)]+", re.IGNORECASE)
_BENCH = r"(?:deep-?swe|terminal-?bench|tbench|swe-?bench)"
_ANSWER_PATH = re.compile(
    r"/tasks/[^/\s]+/(?:solution|tests?)(?:/|$|[?#])"
    r"|/(?:solution|test|model)\.patch(?:$|[?#])"
    r"|/grader\.py(?:$|[?#])"
    r"|/ctrf\.json(?:$|[?#])"
    r"|/tests/test_outputs\.py|/solution/solve\.sh"
    r"|trial-artifacts"
    r"|[a-z0-9-]{8,}__[A-Za-z0-9]{7}(?:/|$|[?#])"
    rf"|{_BENCH}[^?#\s]*/(?:tests?|solution|results?[^/]*)(?:/|$|[?#])"
    rf"|/{_BENCH}-[a-z0-9]+(?:-[a-z0-9]+){{2,}}(?:/|$|[?#])",
    re.IGNORECASE,
)
_TASK_URL = re.compile(rf"/tasks/[^/\s]+|{_BENCH}", re.IGNORECASE)
_ANSWER_NAME = re.compile(
    r"\b(?:solution|tests?)/|\b(?:solution|test|model)\.patch\b|\bgrader\.py\b|\bsolve\.sh\b|\bsolution\.sh\b",
    re.IGNORECASE,
)

REFUSAL = (
    "Refused: {url} looks like the answer key of a benchmark task (reference "
    "solution, hidden tests, grader, or another agent's result). Do not fetch "
    "answer material. Solve the task from the repository, its documentation, "
    "and general sources."
)


def guard_enabled() -> bool:
    return os.environ.get("HARNESS_ANSWER_KEY_GUARD", "1").strip().lower() not in {"0", "false", "off", "no"}


def answer_key_url(text: str) -> Optional[str]:
    """Return the first URL in ``text`` that points at answer material, else None."""
    urls = _URL.findall(text or "")
    for url in urls:
        if _ANSWER_PATH.search(url):
            return url
    names_answer_files = _ANSWER_NAME.search(text or "") is not None
    for url in urls:
        if names_answer_files and _TASK_URL.search(url):
            return url
    return None


def _action_text(act: Any) -> str:
    kind = getattr(act, "kind", "")
    if kind == "web_fetch":
        return getattr(act, "url", "") or ""
    if kind == "run_command":
        return getattr(act, "command", "") or ""
    if kind == "run_command_batch":
        return "\n".join(str(c) for c in (getattr(act, "commands", None) or []))
    if kind == "run_ipython":
        return getattr(act, "content", "") or ""
    return ""


def answer_key_refusal(act: Any) -> Optional[str]:
    """Return the refusal text when ``act`` would fetch answer material."""
    if not guard_enabled():
        return None
    url = answer_key_url(_action_text(act))
    return REFUSAL.format(url=url) if url else None
