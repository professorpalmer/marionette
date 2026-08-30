from __future__ import annotations

"""Session-owned nested todos — OMP phased tree, Marionette persistence.

Steal the oh-my-pi contract (phases + incremental ops + one in-progress +
next-actionable reminder). Do not clone the TUI. The transcript last-wins
snapshot and a session sidecar are the durable unit.
"""

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .paths import resolve_workspace_path

TODO_STATUSES = ("pending", "in_progress", "completed", "abandoned", "blocked")
TODO_OPS = ("init", "start", "done", "rm", "drop", "block", "unblock", "append", "view")
DEFAULT_INIT_PHASE = "Tasks"
TODO_FILENAME = "session_todos.json"
COLLAPSED_OPEN_CAP = 5
COLLAPSED_CLOSED_CONTEXT = 2
TODO_DESCRIPTION_MIN_OVERLAP = 6


class TodoError(ValueError):
    """Raised when a todo op cannot be applied."""


@dataclass
class TodoItem:
    content: str
    status: str = "pending"
    blocker: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"content": self.content, "status": self.status}
        if self.blocker:
            out["blocker"] = self.blocker
        return out


@dataclass
class TodoPhase:
    name: str
    tasks: List[TodoItem] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "tasks": [task.to_dict() for task in self.tasks]}

    def progress(self) -> Tuple[int, int]:
        done = sum(1 for task in self.tasks if task.status in ("completed", "abandoned"))
        return done, len(self.tasks)


def phases_to_dicts(phases: Sequence[TodoPhase]) -> List[Dict[str, Any]]:
    return [phase.to_dict() for phase in phases]


def clone_item(task: TodoItem) -> TodoItem:
    return TodoItem(content=task.content, status=task.status, blocker=task.blocker)


def clone_phases(phases: Sequence[TodoPhase]) -> List[TodoPhase]:
    return [
        TodoPhase(name=phase.name, tasks=[clone_item(task) for task in phase.tasks])
        for phase in phases
    ]


def is_todo_phase(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if not isinstance(value.get("name"), str) or not isinstance(value.get("tasks"), list):
        return False
    for task in value["tasks"]:
        if not isinstance(task, dict) or not isinstance(task.get("content"), str):
            return False
        if task.get("status") not in TODO_STATUSES:
            return False
    return True


def phases_from_raw(raw: Any) -> List[TodoPhase]:
    if not isinstance(raw, list):
        return []
    phases: List[TodoPhase] = []
    for entry in raw:
        if not is_todo_phase(entry):
            continue
        tasks = [
            TodoItem(
                content=str(task.get("content") or ""),
                status=str(task.get("status") or "pending"),
                blocker=(str(task["blocker"]).strip() or None) if task.get("blocker") else None,
            )
            for task in entry["tasks"]
        ]
        phases.append(TodoPhase(name=str(entry["name"]), tasks=tasks))
    return phases


def find_task_by_content(
    phases: Sequence[TodoPhase], content: str
) -> Optional[Tuple[TodoItem, TodoPhase]]:
    for phase in phases:
        for task in phase.tasks:
            if task.content == content:
                return task, phase
    return None


def find_phase_by_name(phases: Sequence[TodoPhase], name: str) -> Optional[TodoPhase]:
    for phase in phases:
        if phase.name == name:
            return phase
    return None


def next_actionable_task(phases: Sequence[TodoPhase]) -> Optional[TodoItem]:
    first_pending: Optional[TodoItem] = None
    for phase in phases:
        for task in phase.tasks:
            if task.status == "in_progress":
                return task
            if first_pending is None and task.status == "pending":
                first_pending = task
    return first_pending


def normalize_in_progress(phases: List[TodoPhase]) -> None:
    ordered = [task for phase in phases for task in phase.tasks]
    if not ordered:
        return
    in_progress = [task for task in ordered if task.status == "in_progress"]
    for extra in in_progress[1:]:
        extra.status = "pending"
    if in_progress:
        return
    for task in ordered:
        if task.status == "pending":
            task.status = "in_progress"
            return


def infer_todo_op(args: Dict[str, Any], has_existing: bool) -> Optional[str]:
    if isinstance(args.get("list"), list) and args["list"]:
        return "init"
    items = args.get("items")
    if isinstance(items, list) and items:
        phase = args.get("phase")
        if isinstance(phase, str) and phase.strip():
            return "append"
        if not has_existing:
            return "init"
    return None


def resolve_todo_params(raw: Any, has_existing: bool) -> Tuple[Optional[Dict[str, Any]], str]:
    if not isinstance(raw, dict):
        return None, "todo arguments must be an object"
    args = dict(raw)
    op = str(args.get("op") or "").strip().lower()
    if not op:
        inferred = infer_todo_op(args, has_existing)
        if inferred:
            op = inferred
            args["op"] = inferred
    if op not in TODO_OPS:
        return None, "todo requires op: init|start|done|rm|drop|block|unblock|append|view"
    return args, ""


def _string_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    out: List[str] = []
    for item in value:
        text = str(item or "").strip()
        if text:
            out.append(text)
    return out


def _init_list_entries(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw_list = entry.get("list")
    if isinstance(raw_list, list) and raw_list:
        parsed: List[Dict[str, Any]] = []
        for row in raw_list:
            if not isinstance(row, dict):
                continue
            phase = str(row.get("phase") or row.get("name") or "").strip()
            items = _string_list(row.get("items") or row.get("tasks"))
            if phase and items:
                parsed.append({"phase": phase, "items": items})
        return parsed
    items = _string_list(entry.get("items"))
    if items:
        phase = str(entry.get("phase") or DEFAULT_INIT_PHASE).strip() or DEFAULT_INIT_PHASE
        return [{"phase": phase, "items": items}]
    return []


def init_phases(entry: Dict[str, Any], errors: List[str]) -> List[TodoPhase]:
    listing = _init_list_entries(entry)
    if not listing:
        errors.append("Missing list for init operation")
        return []
    seen_phases: set[str] = set()
    seen_tasks: set[str] = set()
    for row in listing:
        if row["phase"] in seen_phases:
            errors.append('Duplicate phase "%s" in init list' % row["phase"])
        seen_phases.add(row["phase"])
        for content in row["items"]:
            if content in seen_tasks:
                errors.append('Duplicate task "%s" in init list' % content)
            seen_tasks.add(content)
    if errors:
        return []
    return [
        TodoPhase(
            name=row["phase"],
            tasks=[TodoItem(content=content) for content in row["items"]],
        )
        for row in listing
    ]


def _resolve_task(
    phases: Sequence[TodoPhase], content: Optional[str], errors: List[str]
) -> Optional[Tuple[TodoItem, TodoPhase]]:
    if not content:
        errors.append("Missing task content")
        return None
    hit = find_task_by_content(phases, content)
    if hit is None:
        if content.startswith("task-") and content[5:].isdigit():
            errors.append(
                'Task "%s" not found. Tasks are referenced by content, not by IDs — '
                "pass the task's full text from the previous result." % content
            )
        else:
            total = sum(len(phase.tasks) for phase in phases)
            hint = " (todo list is empty — was it replaced or not yet created?)" if total == 0 else ""
            errors.append('Task "%s" not found%s' % (content, hint))
    return hit


def _resolve_phase(
    phases: Sequence[TodoPhase], name: Optional[str], errors: List[str]
) -> Optional[TodoPhase]:
    if not name:
        errors.append("Missing phase name")
        return None
    phase = find_phase_by_name(phases, name)
    if phase is None:
        errors.append('Phase "%s" not found' % name)
    return phase


def _task_targets(
    phases: Sequence[TodoPhase], entry: Dict[str, Any], errors: List[str]
) -> List[TodoItem]:
    task = str(entry.get("task") or "").strip()
    phase_name = str(entry.get("phase") or "").strip()
    if task:
        hit = _resolve_task(phases, task, errors)
        return [hit[0]] if hit else []
    if phase_name:
        phase = _resolve_phase(phases, phase_name, errors)
        return list(phase.tasks) if phase else []
    return [task for phase in phases for task in phase.tasks]


def append_items(phases: List[TodoPhase], entry: Dict[str, Any], errors: List[str]) -> List[TodoPhase]:
    phase_name = str(entry.get("phase") or "").strip()
    items = _string_list(entry.get("items"))
    if not phase_name:
        errors.append("Missing phase name for append operation")
        return phases
    if not items:
        errors.append("Missing items for append operation")
        return phases
    seen: set[str] = set()
    duplicate = False
    for content in items:
        if content in seen or find_task_by_content(phases, content):
            errors.append('Task "%s" already exists' % content)
            duplicate = True
        seen.add(content)
    if duplicate:
        return phases
    phase = find_phase_by_name(phases, phase_name)
    if phase is None:
        phase = TodoPhase(name=phase_name, tasks=[])
        phases.append(phase)
    for content in items:
        phase.tasks.append(TodoItem(content=content))
    return phases


def remove_tasks(phases: List[TodoPhase], entry: Dict[str, Any], errors: List[str]) -> List[TodoPhase]:
    task = str(entry.get("task") or "").strip()
    phase_name = str(entry.get("phase") or "").strip()
    if task:
        hit = _resolve_task(phases, task, errors)
        if not hit:
            return phases
        item, phase = hit
        phase.tasks = [candidate for candidate in phase.tasks if candidate is not item]
        return phases
    if phase_name:
        phase = _resolve_phase(phases, phase_name, errors)
        if not phase:
            return phases
        phase.tasks = []
        return phases
    for phase in phases:
        phase.tasks = []
    return phases


def apply_entry(phases: List[TodoPhase], entry: Dict[str, Any], errors: List[str]) -> List[TodoPhase]:
    op = str(entry.get("op") or "").strip().lower()
    if op == "init":
        return init_phases(entry, errors)
    if op == "start":
        hit = _resolve_task(phases, str(entry.get("task") or "").strip(), errors)
        if not hit:
            return phases
        target, _phase = hit
        for phase in phases:
            for candidate in phase.tasks:
                if candidate.status == "in_progress" and candidate is not target:
                    candidate.status = "pending"
        target.status = "in_progress"
        return phases
    if op == "done":
        for task in _task_targets(phases, entry, errors):
            task.status = "completed"
        return phases
    if op == "drop":
        for task in _task_targets(phases, entry, errors):
            task.status = "abandoned"
        return phases
    if op == "block":
        if not str(entry.get("task") or "").strip() and not str(entry.get("phase") or "").strip():
            errors.append("block requires a task or phase target")
            return phases
        reason = str(entry.get("reason") or "").replace("\n", " ")
        reason = " ".join(reason.split()).strip() or None
        for task in _task_targets(phases, entry, errors):
            if task.status not in ("pending", "in_progress", "blocked"):
                continue
            task.status = "blocked"
            task.blocker = reason
        return phases
    if op == "unblock":
        if not str(entry.get("task") or "").strip() and not str(entry.get("phase") or "").strip():
            errors.append("unblock requires a task or phase target")
            return phases
        for task in _task_targets(phases, entry, errors):
            if task.status == "blocked":
                task.status = "pending"
                task.blocker = None
        return phases
    if op == "rm":
        return remove_tasks(phases, entry, errors)
    if op == "append":
        return append_items(phases, entry, errors)
    if op == "view":
        return phases
    errors.append("unknown todo op: %s" % op)
    return phases


def apply_todo_op(
    current: Sequence[TodoPhase],
    raw: Any,
) -> Tuple[List[TodoPhase], List[str], str]:
    """Apply one op. Returns (phases, errors, resolved_op)."""
    params, err = resolve_todo_params(raw, bool(current))
    if params is None:
        return clone_phases(current), [err], ""
    next_phases = clone_phases(current)
    errors: List[str] = []
    next_phases = apply_entry(next_phases, params, errors)
    if errors:
        return clone_phases(current), errors, str(params.get("op") or "")
    normalize_in_progress(next_phases)
    return next_phases, [], str(params.get("op") or "")


def _is_closed(status: str) -> bool:
    return status in ("completed", "abandoned")


def _collapse_tasks(tasks: Sequence[TodoItem]) -> Tuple[List[TodoItem], int]:
    closed = [task for task in tasks if _is_closed(task.status)]
    open_tasks = [task for task in tasks if not _is_closed(task.status)]
    lead = closed[-COLLAPSED_CLOSED_CONTEXT:]
    if len(open_tasks) <= COLLAPSED_OPEN_CAP:
        shown = lead + list(open_tasks)
        hidden = len(tasks) - len(shown)
        return shown, hidden
    shown = lead + list(open_tasks[:COLLAPSED_OPEN_CAP])
    hidden = len(tasks) - len(shown)
    return shown, hidden


def format_todo_tree(phases: Sequence[TodoPhase], *, collapsed: bool = True) -> str:
    if not phases:
        return "TODO (empty)"
    total_done = 0
    total = 0
    lines = []
    for index, phase in enumerate(phases, start=1):
        done, count = phase.progress()
        total_done += done
        total += count
        roman = _to_roman(index)
        lines.append("%s. %s · %d/%d" % (roman, phase.name, done, count))
        visible, hidden = _collapse_tasks(phase.tasks) if collapsed else (list(phase.tasks), 0)
        for task in visible:
            mark = {
                "completed": "[x]",
                "abandoned": "[-]",
                "in_progress": "[>]",
                "blocked": "[!]",
            }.get(task.status, "[ ]")
            extra = " — %s" % task.blocker if task.status == "blocked" and task.blocker else ""
            lines.append("  %s %s%s" % (mark, task.content, extra))
        if hidden:
            lines.append("  ... %d more todo%s" % (hidden, "s" if hidden != 1 else ""))
    header = "TODO %d/%d" % (total_done, total)
    nxt = next_actionable_task(phases)
    body = "\n".join([header] + lines)
    if nxt:
        body += "\nNext: %s" % nxt.content
    return body


def _to_roman(value: int) -> str:
    table = (
        (10, "X"),
        (9, "IX"),
        (5, "V"),
        (4, "IV"),
        (1, "I"),
    )
    remaining = max(1, int(value))
    out = []
    for arabic, glyph in table:
        while remaining >= arabic:
            out.append(glyph)
            remaining -= arabic
    return "".join(out)


def snapshot_payload(phases: Sequence[TodoPhase], op: str = "") -> Dict[str, Any]:
    nxt = next_actionable_task(phases)
    return {
        "op": op or None,
        "phases": phases_to_dicts(phases),
        "storage": "session",
        "next": nxt.content if nxt else None,
    }


class SessionTodoStore:
    """Load/save todo JSON under a harness state_dir, keyed by conversation id.

    One file is shared across sessions in the same home. Unscoped ``phases``
    (legacy) are never attached to a real session id — that leaked beyblade
    todos into marionette conversations.
    """

    def __init__(self, state_dir: str) -> None:
        self.state_dir = state_dir or ""
        self.path = os.path.join(self.state_dir, TODO_FILENAME) if self.state_dir else ""

    def _read_raw(self) -> dict:
        if not self.path or not os.path.isfile(self.path):
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _write_raw(self, payload: dict) -> None:
        if not self.path or not self.state_dir:
            return
        os.makedirs(self.state_dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="session_todos.", dir=self.state_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
                fh.write("\n")
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def load(self, session_id: str = "") -> List[TodoPhase]:
        raw = self._read_raw()
        sid = (session_id or "").strip()
        sessions = raw.get("sessions")
        if sid:
            if not isinstance(sessions, dict):
                return []
            bucket = sessions.get(sid)
            if isinstance(bucket, dict):
                return phases_from_raw(bucket.get("phases"))
            return phases_from_raw(bucket)
        if isinstance(sessions, dict) and sessions:
            return []
        return phases_from_raw(raw.get("phases"))

    def save(self, phases: Sequence[TodoPhase], session_id: str = "") -> None:
        sid = (session_id or "").strip()
        if sid:
            raw = self._read_raw()
            sessions = raw.get("sessions") if isinstance(raw.get("sessions"), dict) else {}
            sessions[sid] = {"phases": phases_to_dicts(phases)}
            self._write_raw({"sessions": sessions})
            return
        self._write_raw({"phases": phases_to_dicts(phases)})


DEFAULT_TODO_MARKDOWN = "TODO.md"

STATUS_TO_MARKER = {
    "pending": " ",
    "in_progress": "/",
    "completed": "x",
    "abandoned": "-",
    "blocked": "!",
}

MARKER_TO_STATUS = {
    " ": "pending",
    "": "pending",
    "x": "completed",
    "X": "completed",
    "/": "in_progress",
    ">": "in_progress",
    "-": "abandoned",
    "~": "abandoned",
    "!": "blocked",
}

TODO_SLASH_USAGE = (
    "Usage: /todo <verb> [args]\n"
    "  /todo                              Show current todos\n"
    "  /todo copy                         Print todos as Markdown\n"
    "  /todo export [<path>]              Write todos to file (default: TODO.md)\n"
    "  /todo import [<path>]              Replace todos from file (default: TODO.md)\n"
    "  /todo append [<phase>] <task...>   Append a task\n"
    "  /todo start  <task>                Mark task in_progress (fuzzy match)\n"
    "  /todo done   [<task|phase>]        Mark task/phase/all completed\n"
    "  /todo drop   [<task|phase>]        Mark task/phase/all abandoned\n"
    "  /todo rm     [<task|phase>]        Remove task/phase/all\n"
    "  /todo block  <task> [-- reason]    Block a task\n"
    "  /todo unblock <task>               Unblock a task"
)


def normalize_for_todo_match(value: str) -> str:
    chars: List[str] = []
    for ch in (value or "").lower():
        if ch.isalnum():
            chars.append(ch)
        elif chars and chars[-1] != " ":
            chars.append(" ")
    return "".join(chars).strip()


def todo_matches_any_description(content: str, descriptions: Sequence[str]) -> bool:
    target = normalize_for_todo_match(content)
    if not target:
        return False
    for desc in descriptions:
        candidate = normalize_for_todo_match(desc)
        if not candidate:
            continue
        if target == candidate:
            return True
        if len(target) >= TODO_DESCRIPTION_MIN_OVERLAP and target in candidate:
            return True
        if len(candidate) >= TODO_DESCRIPTION_MIN_OVERLAP and candidate in target:
            return True
    return False


def phases_to_markdown(phases: Sequence[TodoPhase]) -> str:
    if not phases:
        return "# Todos\n"
    out: List[str] = []
    for index, phase in enumerate(phases):
        if index:
            out.append("")
        out.append("# %s" % phase.name)
        for task in phase.tasks:
            marker = STATUS_TO_MARKER.get(task.status, " ")
            blocker = ""
            if task.status == "blocked" and task.blocker:
                blocker = " <!-- blocker: %s -->" % task.blocker
            out.append("- [%s] %s%s" % (marker, task.content, blocker))
    return "%s\n" % "\n".join(out)


def markdown_to_phases(md: str) -> Tuple[List[TodoPhase], List[str]]:
    errors: List[str] = []
    phases: List[TodoPhase] = []
    current: Optional[TodoPhase] = None
    heading = re.compile(r"^#{1,6}\s+(.+?)\s*$")
    task_re = re.compile(r"^[-*+]\s*\[(.?)\]\s+(.+?)\s*$")
    blocker_re = re.compile(r"^(.*?)\s*<!--\s*blocker:\s*(.*?)\s*-->$")
    for line_num, raw in enumerate((md or "").splitlines(), start=1):
        trimmed = raw.strip()
        if not trimmed:
            continue
        heading_match = heading.match(trimmed)
        if heading_match:
            current = TodoPhase(name=heading_match.group(1).strip(), tasks=[])
            phases.append(current)
            continue
        task_match = task_re.match(trimmed)
        if task_match:
            if current is None:
                current = TodoPhase(name="Todos", tasks=[])
                phases.append(current)
            marker = task_match.group(1)
            status = MARKER_TO_STATUS.get(marker)
            if status is None:
                errors.append(
                    'Line %d: unknown status marker "[%s]" (use [ ], [x], [/], [-], [!])'
                    % (line_num, marker)
                )
                continue
            raw_content = task_match.group(2).strip()
            blocker_match = blocker_re.match(raw_content)
            if status == "blocked" and blocker_match:
                current.tasks.append(
                    TodoItem(
                        content=blocker_match.group(1).strip(),
                        status=status,
                        blocker=blocker_match.group(2).strip() or None,
                    )
                )
            else:
                current.tasks.append(TodoItem(content=raw_content, status=status))
            continue
        errors.append('Line %d: unrecognized syntax "%s"' % (line_num, trimmed))
    normalize_in_progress(phases)
    return phases, errors


def resolve_todo_markdown_path(workspace_root: str, user_path: str) -> Tuple[str, str]:
    raw = (user_path or "").strip() or DEFAULT_TODO_MARKDOWN
    return resolve_workspace_path(workspace_root, raw)


def export_todo_markdown(
    phases: Sequence[TodoPhase], workspace_root: str, user_path: str = ""
) -> Tuple[str, str]:
    abs_path, rel = resolve_todo_markdown_path(workspace_root, user_path)
    parent = os.path.dirname(abs_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(abs_path, "w", encoding="utf-8") as fh:
        fh.write(phases_to_markdown(phases))
    return abs_path, rel


def import_todo_markdown(
    workspace_root: str, user_path: str = ""
) -> Tuple[List[TodoPhase], List[str], str]:
    abs_path, rel = resolve_todo_markdown_path(workspace_root, user_path)
    if not os.path.isfile(abs_path):
        return [], ['Todo markdown not found: %s' % (rel or DEFAULT_TODO_MARKDOWN)], rel
    with open(abs_path, "r", encoding="utf-8") as fh:
        text = fh.read()
    phases, errors = markdown_to_phases(text)
    return phases, errors, rel


def find_phase_fuzzy(phases: Sequence[TodoPhase], query: str) -> Optional[TodoPhase]:
    normalized = (query or "").strip().lower()
    if not normalized:
        return None
    exact = [phase for phase in phases if phase.name.lower() == normalized]
    if len(exact) == 1:
        return exact[0]
    prefixes = [phase for phase in phases if phase.name.lower().startswith(normalized)]
    if len(prefixes) == 1:
        return prefixes[0]
    substr = [phase for phase in phases if normalized in phase.name.lower()]
    if len(substr) == 1:
        return substr[0]
    return None


def find_task_fuzzy(
    phases: Sequence[TodoPhase], query: str
) -> Optional[Tuple[TodoItem, TodoPhase]]:
    normalized = (query or "").strip().lower()
    if not normalized:
        return None
    for phase in phases:
        for task in phase.tasks:
            if task.content.lower() == normalized:
                return task, phase
    matches: List[Tuple[TodoItem, TodoPhase]] = []
    for phase in phases:
        for task in phase.tasks:
            if normalized in task.content.lower():
                matches.append((task, phase))
    if len(matches) == 1:
        return matches[0]
    active = [
        hit for hit in matches if hit[0].status in ("in_progress", "pending")
    ]
    if len(active) == 1:
        return active[0]
    return None


def _split_task_reason(rest: str) -> Tuple[str, str]:
    if " -- " in rest:
        task, reason = rest.split(" -- ", 1)
        return task.strip(), reason.strip()
    return rest.strip(), ""


@dataclass
class SlashTodoResult:
    ok: bool
    mutated: bool = False
    phases: List[TodoPhase] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    tree: str = ""
    markdown: str = ""
    notice: str = ""
    path: str = ""
    usage: str = ""

    def public_dict(self) -> Dict[str, Any]:
        payload = snapshot_payload(self.phases)
        out: Dict[str, Any] = {
            "ok": self.ok,
            "mutated": self.mutated,
            "tree": self.tree or format_todo_tree(self.phases),
            "markdown": self.markdown or phases_to_markdown(self.phases),
            "notice": self.notice,
            "todos": payload,
        }
        if self.errors:
            out["error"] = "; ".join(self.errors)
        if self.path:
            out["path"] = self.path
        if self.usage:
            out["usage"] = self.usage
        return out


def _slash_result(
    phases: Sequence[TodoPhase],
    *,
    ok: bool = True,
    mutated: bool = False,
    errors: Optional[List[str]] = None,
    notice: str = "",
    path: str = "",
    usage: str = "",
    markdown: str = "",
) -> SlashTodoResult:
    cloned = clone_phases(phases)
    return SlashTodoResult(
        ok=ok,
        mutated=mutated,
        phases=cloned,
        errors=list(errors or []),
        tree=format_todo_tree(cloned),
        markdown=markdown or phases_to_markdown(cloned),
        notice=notice,
        path=path,
        usage=usage,
    )


def _apply_or_error(
    current: Sequence[TodoPhase], raw: Dict[str, Any]
) -> SlashTodoResult:
    phases, errors, _op = apply_todo_op(current, raw)
    if errors:
        return _slash_result(current, ok=False, errors=errors)
    return _slash_result(phases, mutated=True, notice=format_todo_tree(phases))


def _slash_target_op(
    current: Sequence[TodoPhase], verb: str, rest: str
) -> SlashTodoResult:
    query = (rest or "").strip()
    payload: Dict[str, Any] = {"op": verb}
    if query:
        hit = find_task_fuzzy(current, query)
        if hit:
            payload["task"] = hit[0].content
        else:
            phase = find_phase_fuzzy(current, query)
            if phase:
                payload["phase"] = phase.name
            else:
                return _slash_result(
                    current,
                    ok=False,
                    errors=['No unique task or phase matching "%s"' % query],
                )
    return _apply_or_error(current, payload)


def handle_todo_slash_command(
    raw: str,
    current: Sequence[TodoPhase],
    workspace_root: str = "",
) -> SlashTodoResult:
    text = (raw or "").strip()
    if text.lower().startswith("/todo"):
        text = text[5:].strip()
    if not text or text.lower() in ("help", "-h", "--help"):
        return _slash_result(
            current,
            notice=format_todo_tree(current) if current else TODO_SLASH_USAGE,
            usage=TODO_SLASH_USAGE,
        )
    parts = text.split(None, 1)
    verb = parts[0].lower()
    rest = parts[1].strip() if len(parts) > 1 else ""
    if verb in ("view", "show", "list"):
        return _slash_result(current, notice=format_todo_tree(current))
    if verb == "copy":
        md = phases_to_markdown(current)
        return _slash_result(current, markdown=md, notice=md.strip() or "No todos.")
    if verb == "export":
        if not (workspace_root or "").strip():
            return _slash_result(current, ok=False, errors=["No open workspace"])
        try:
            _abs_path, rel = export_todo_markdown(current, workspace_root, rest)
        except ValueError as exc:
            return _slash_result(current, ok=False, errors=[str(exc)])
        except OSError as exc:
            return _slash_result(current, ok=False, errors=[str(exc)])
        return _slash_result(
            current,
            notice="Wrote todos to %s" % (rel or DEFAULT_TODO_MARKDOWN),
            path=rel or DEFAULT_TODO_MARKDOWN,
        )
    if verb == "import":
        if not (workspace_root or "").strip():
            return _slash_result(current, ok=False, errors=["No open workspace"])
        try:
            phases, errors, rel = import_todo_markdown(workspace_root, rest)
        except ValueError as exc:
            return _slash_result(current, ok=False, errors=[str(exc)])
        except OSError as exc:
            return _slash_result(current, ok=False, errors=[str(exc)])
        if errors:
            return _slash_result(current, ok=False, errors=errors, path=rel)
        return _slash_result(
            phases,
            mutated=True,
            notice="Imported todos from %s" % (rel or DEFAULT_TODO_MARKDOWN),
            path=rel or DEFAULT_TODO_MARKDOWN,
        )
    if verb == "append":
        if not rest:
            return _slash_result(current, ok=False, errors=["Missing task for append"])
        tokens = rest.split()
        phase_name = current[-1].name if current else DEFAULT_INIT_PHASE
        task_text = rest
        if len(tokens) >= 2:
            phase = find_phase_fuzzy(current, tokens[0])
            phase_name = phase.name if phase is not None else tokens[0]
            task_text = " ".join(tokens[1:]).strip()
        return _apply_or_error(
            current, {"op": "append", "phase": phase_name, "items": [task_text]}
        )
    if verb == "start":
        if not rest:
            return _slash_result(current, ok=False, errors=["Missing task for start"])
        hit = find_task_fuzzy(current, rest)
        if hit is None:
            return _slash_result(
                current, ok=False, errors=['No unique task matching "%s"' % rest]
            )
        return _apply_or_error(current, {"op": "start", "task": hit[0].content})
    if verb in ("done", "drop", "rm"):
        return _slash_target_op(current, verb, rest)
    if verb == "block":
        task_q, reason = _split_task_reason(rest)
        if not task_q:
            return _slash_result(current, ok=False, errors=["Missing task for block"])
        hit = find_task_fuzzy(current, task_q)
        if hit is None:
            phase = find_phase_fuzzy(current, task_q)
            if phase is None:
                return _slash_result(
                    current, ok=False, errors=['No unique task or phase matching "%s"' % task_q]
                )
            return _apply_or_error(
                current, {"op": "block", "phase": phase.name, "reason": reason}
            )
        return _apply_or_error(
            current, {"op": "block", "task": hit[0].content, "reason": reason}
        )
    if verb == "unblock":
        if not rest:
            return _slash_result(current, ok=False, errors=["Missing task for unblock"])
        hit = find_task_fuzzy(current, rest)
        if hit is None:
            phase = find_phase_fuzzy(current, rest)
            if phase is None:
                return _slash_result(
                    current, ok=False, errors=['No unique task or phase matching "%s"' % rest]
                )
            return _apply_or_error(current, {"op": "unblock", "phase": phase.name})
        return _apply_or_error(current, {"op": "unblock", "task": hit[0].content})
    return _slash_result(
        current,
        ok=False,
        errors=["Unknown /todo verb: %s" % verb],
        usage=TODO_SLASH_USAGE,
    )
