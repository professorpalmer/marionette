from __future__ import annotations

"""Keep the user's checklist moving while the pilot works.

In real sessions pilots marked tasks done long after finishing them, often
several at once, so the list the user watches sat still and then jumped.
After REMIND_EVERY tool calls with a task still in progress and no todo
update, the next tool result names that task. Like the repeat-tool reminder,
the nudge rides on the result being appended: a separate history row would
break native tool pairing. Never vetoes a call.
"""

from typing import Any, Optional

REMIND_EVERY = 5

# Polling a job is not progress on the task.
_NOT_WORK = frozenset({"todo", "wait"})


def _in_progress(phases: Any) -> Optional[str]:
    for phase in phases or []:
        for task in getattr(phase, "tasks", None) or []:
            if getattr(task, "status", "") == "in_progress":
                return str(getattr(task, "content", "") or "").strip() or None
    return None


def note_todo_progress_and_maybe_nudge(session: Any, act: Any, content: str) -> str:
    kind = str(getattr(act, "kind", "") or "")
    if kind == "todo":
        session._tool_calls_since_todo = 0
        return content
    if kind in _NOT_WORK:
        return content
    task = _in_progress(getattr(session, "_todo_phases", None))
    if task is None:
        return content
    count = int(getattr(session, "_tool_calls_since_todo", 0) or 0) + 1
    session._tool_calls_since_todo = count
    if count % REMIND_EVERY:
        return content
    return content + (
        f'\n\n[todo] "{task}" is still in progress on the checklist the user is '
        "watching. If it is finished, mark it done now and start the next task; "
        "update the list as each step completes, not in a batch at the end."
    )
