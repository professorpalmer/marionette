from __future__ import annotations

"""Keep the user's checklist moving while the pilot works.

In real sessions pilots marked tasks done long after finishing them, often
several at once, so the list the user watches sat still and then jumped.
After REMIND_EVERY tool calls with a task still in progress and no todo
update, the next tool result names that task. Like the repeat-tool reminder,
the nudge rides on the result being appended: a separate history row would
break native tool pairing. Never vetoes a call.

Pilots also did multi-step work with no list at all (stored transcripts:
0 of 21 turns with 3 to 5 tool calls had one, 2 of 21 with 6 to 14). When a
turn reaches PLAN_NUDGE_AT tool calls with no step in progress and no todo
call, one tool result asks for the plan. MICRO turns are exempt.
"""

from typing import Any, Optional

REMIND_EVERY = 5
PLAN_NUDGE_AT = 4

# Polling a job is not progress on the task.
_NOT_WORK = frozenset({"todo", "wait"})


def _in_progress(phases: Any) -> Optional[str]:
    for phase in phases or []:
        for task in getattr(phase, "tasks", None) or []:
            if getattr(task, "status", "") == "in_progress":
                return str(getattr(task, "content", "") or "").strip() or None
    return None


def reset_turn_plan_state(session: Any) -> None:
    """A new user turn starts: no plan call and no plan nudge yet."""
    session._turn_work_calls = 0
    session._turn_plan_touched = False
    session._turn_plan_nudged = False


def note_todo_progress_and_maybe_nudge(session: Any, act: Any, content: str) -> str:
    kind = str(getattr(act, "kind", "") or "")
    if kind == "todo":
        session._tool_calls_since_todo = 0
        session._turn_plan_touched = True
        return content
    if kind in _NOT_WORK:
        return content
    work = int(getattr(session, "_turn_work_calls", 0) or 0) + 1
    session._turn_work_calls = work
    task = _in_progress(getattr(session, "_todo_phases", None))
    if task is None:
        if (
            work < PLAN_NUDGE_AT
            or getattr(session, "_turn_plan_touched", False)
            or getattr(session, "_turn_plan_nudged", False)
            or str(getattr(session, "_task_profile", "") or "") == "MICRO"
        ):
            return content
        session._turn_plan_nudged = True
        return content + (
            "\n\n[plan] The user follows this work on a step list under the chat, "
            "and this work has no list. Send todo op init with 3 to 7 short steps "
            "in the same response as your next tool calls. Then send todo op "
            "advance each time a step is done."
        )
    count = int(getattr(session, "_tool_calls_since_todo", 0) or 0) + 1
    session._tool_calls_since_todo = count
    if count % REMIND_EVERY:
        return content
    return content + (
        f'\n\n[todo] "{task}" is still in progress on the checklist the user is '
        "watching. If it is finished, send todo op advance now; update the list "
        "as each step completes, not in a batch at the end."
    )
