"""The checklist the user watches must move step by step.

Real sessions (67 todo calls): the pilot marked tasks `done` 49 times and
`start` only 3, and 12 of 30 completion bursts closed 2-5 tasks at once after
the work was over, so the list sat still and then jumped. After several tool
calls with a task still in progress, the next tool result carries a reminder.
"""
from __future__ import annotations

from types import SimpleNamespace

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.todo import TodoItem, TodoPhase


def _session(tmp_path):
    s = ConversationalSession(HarnessConfig(driver="stub-oracle-v2", state_dir=str(tmp_path)))
    s._todo_phases = [TodoPhase(name="Fix", tasks=[
        TodoItem(content="Reproduce the flash", status="in_progress"),
        TodoItem(content="Write the fix"),
    ])]
    return s


def _run(s, kind, n=1):
    last = ""
    for i in range(n):
        act = SimpleNamespace(kind=kind, tool_call_id=f"{kind}-{i}-{len(s._history)}", arguments={"path": f"f{i}"})
        s._append_action_result(act, act.tool_call_id, f"{kind} output {i}", False)
        last = s._history[-1]["content"]
    return last


def test_reminds_after_several_tool_calls_with_a_task_in_progress(tmp_path):
    s = _session(tmp_path)
    assert "Reproduce the flash" not in _run(s, "read_file", 4)
    reminder = _run(s, "read_file")
    assert '"Reproduce the flash" is still in progress' in reminder


def test_a_todo_update_resets_the_count(tmp_path):
    s = _session(tmp_path)
    _run(s, "read_file", 4)
    _run(s, "todo")
    assert "still in progress" not in _run(s, "read_file", 4)


def test_no_reminder_without_a_task_in_progress(tmp_path):
    s = _session(tmp_path)
    for task in s._todo_phases[0].tasks:
        task.status = "completed"
    assert "still in progress" not in _run(s, "read_file", 10)
