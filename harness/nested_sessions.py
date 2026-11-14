from __future__ import annotations

"""Nested sessions: a pilot starts child sessions and gets their reports.

Design: ``docs/design/nested-sessions.md``. A child is a normal session row
with a ``nest`` link and its own runner. The backend drives a child turn with
no browser attached (``harness.headless_turn``). When a child turn ends,
``on_turn_end`` records its status and report and puts a note in the parent
inbox. ``deliver`` moves notes into an idle parent and can wake the parent
with one resume turn.

Pure helpers (names, report text, note text, live status, wake rule) are at
the top so tests can check them without a server.
"""

import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from .display_rows import now_ms

NEST_STATUSES = ("working", "done", "failed", "stopped")
REPORT_CHARS = 400
# Automatic parent turns since the last user message. Stops report loops.
MAX_AUTO_WAKES = 8
# Delivery retry while the parent is busy: every half second for one minute.
RETRY_SECONDS = 0.5
RETRY_TRIES = 120
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,38}[a-z0-9]$")

_rev_lock = threading.Lock()
_rev = 0


def bump_rev() -> int:
    """Count each nest change. The UI refreshes the rail when it moves."""
    global _rev
    with _rev_lock:
        _rev += 1
        return _rev


def current_rev() -> int:
    return _rev


# -- pure helpers ----------------------------------------------------------


def normalize_name(raw: str) -> str:
    """A short slug for the chip: lowercase words joined by hyphens."""
    text = re.sub(r"[^a-z0-9]+", "-", (raw or "").strip().lower()).strip("-")
    return text[:40].strip("-")


def valid_name(name: str) -> bool:
    return bool(_NAME_RE.match(name or ""))


def unique_name(name: str, taken: List[str]) -> str:
    if name not in taken:
        return name
    n = 2
    while f"{name}-{n}" in taken:
        n += 1
    return f"{name}-{n}"


def branch_name(name: str, sid: str) -> str:
    return f"nest/{name}-{sid[:6]}"


def message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text") or ""))
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(parts)
    return ""


def _is_user_text(message: dict) -> bool:
    """A user message the user or parent wrote, not a tool result in user role."""
    if message.get("role") != "user":
        return False
    content = message.get("content")
    if isinstance(content, list):
        return not any(isinstance(p, dict) and p.get("type") == "tool_result" for p in content)
    return True


def report_from_history(history: List[dict], limit: int = REPORT_CHARS) -> str:
    """The first ``limit`` characters of this turn's last assistant reply.

    Only replies after the last user message count. A turn stopped before it
    replied has no report, not the report of the turn before it.
    """
    for message in reversed(history or []):
        if not isinstance(message, dict):
            continue
        if _is_user_text(message):
            return ""
        if message.get("role") != "assistant":
            continue
        text = " ".join(message_text(message.get("content")).split())
        if text:
            return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."
    return ""


def live_status(stored: str, runner_status: str, in_flight: bool = False) -> str:
    """The status to show. Only a turn that runs now is ``working``.

    A running runner, or a turn the supervisor started that has not ended, is
    ``working``. A stored ``working`` with neither lost its turn (a crash, a
    restart before the boot pass), so it shows ``stopped``, never ``working``.
    """
    if in_flight or runner_status in ("running", "attaching"):
        return "working"
    if stored == "working":
        return "stopped"
    return stored if stored in NEST_STATUSES else "done"


def note_id(note: dict) -> str:
    """The key that makes one report arrive once."""
    return str(note.get("id") or f"{note.get('child_id', '')}:{note.get('at', '')}")


def note_text(note: dict) -> str:
    """The parent history text for one child report."""
    name = note.get("name") or note.get("child_id") or "child"
    lines = [
        "<nested-session-event>",
        f'Child session "{name}" (id {note.get("child_id", "")}) ended a turn. Status: {note.get("status", "done")}.',
    ]
    if note.get("driver") == "user":
        lines.append("The user drove this turn in the child session.")
    report = (note.get("report") or "").strip()
    lines.append(f"Report: {report}" if report else "Report: (no text in the final reply)")
    lines.append(
        'This is the child\'s report, not proof. Check its branch or files before you '
        'say the work is done. Use nested_session action "message" to reply to it.'
    )
    lines.append("</nested-session-event>")
    return "\n".join(lines)


def note_display_row(note: dict) -> dict:
    """The parent transcript row for one report.

    ``ts`` is the delivery time in milliseconds, as for every display row. A
    wake turn starts at delivery, so a reload times that turn from this row.
    """
    return {
        "type": "session_note",
        "note_id": note_id(note),
        "child_id": note.get("child_id", ""),
        "name": note.get("name", ""),
        "status": note.get("status", "done"),
        "report": note.get("report", ""),
        "driver": note.get("driver", "parent"),
        "ts": now_ms(),
    }


def should_wake(*, pending_wake: bool, siblings_working: bool, parent_idle: bool,
                auto_wakes: int, stop_held: bool) -> bool:
    return (pending_wake and not siblings_working and parent_idle
            and not stop_held and auto_wakes < MAX_AUTO_WAKES)


def child_preamble(parent_title: str, name: str, branch: str, worktree: str) -> str:
    where = f"Your git worktree is {worktree} on branch {branch}. " if branch else ""
    return (
        f'You are the nested session "{name}", started by the session "{parent_title}". '
        f"{where}Do only the task below. The first {REPORT_CHARS} characters of your final "
        "reply go back to the parent session, so put the result first: what you did, "
        "the commit or files, and what is not done or not verified. Do not push.\n\nTask:\n"
    )


def child_payload(row: dict, runner_status: str, in_flight: bool = False) -> dict:
    nest = row.get("nest") or {}
    return {
        "id": row.get("id", ""),
        "title": row.get("title", ""),
        "name": nest.get("name", ""),
        "status": live_status(nest.get("status", ""), runner_status, in_flight),
        "report": nest.get("report", ""),
        "todo": nest.get("todo", ""),
        "branch": nest.get("branch", ""),
        "worktree": nest.get("worktree", ""),
        "updated": nest.get("updated") or row.get("created") or 0,
    }


def run_repo(row: Optional[dict]) -> str:
    """The directory a child's runner works in, or "" for a normal session."""
    nest = (row or {}).get("nest") or {}
    path = (nest.get("worktree") or "").strip()
    return path if path and os.path.isdir(path) else ""


# -- supervisor ------------------------------------------------------------


@dataclass
class NestServices:
    sessions: Any
    get_runner: Callable[[str], Any]
    runner_status: Callable[[str], str]
    # (session_id, message, *, resume, driver, token) -> None. Runs on a
    # thread. When the thread ends it calls ``turn_exited(sid, token, outcome)``.
    run_turn: Callable[..., None]
    persist: Callable[[str, Any], None]
    diag: Callable[..., None]
    add_worktree: Optional[Callable[[str, str], dict]] = None
    # Build a runner for a session with no view change (a wake needs one).
    ensure_runner: Optional[Callable[[str], Any]] = None


class NestedSupervisor:
    def __init__(self, svc: NestServices) -> None:
        self.svc = svc
        self._lock = threading.RLock()
        # Sessions whose current turn the parent (or a wake) started.
        self._parent_driven: Dict[str, bool] = {}
        # session id -> token of the turn ``_begin`` started that has not ended.
        self._in_flight: Dict[str, int] = {}
        self._tokens = 0
        # Parents with a delivery retry thread.
        self._retrying: set = set()

    # -- tool actions --------------------------------------------------

    def start(self, parent: Any, *, name: str, brief: str, title: str = "",
              todo: str = "") -> dict:
        sessions = self.svc.sessions
        parent_id = getattr(parent, "harness_session_id", "") or ""
        if not parent_id:
            return {"ok": False, "error": "This session has no id. Open a project first."}
        if (sessions.nest_of(parent_id) or {}).get("parent_id"):
            return {"ok": False, "error": "A nested session cannot start its own nested sessions."}
        slug = normalize_name(name)
        if not valid_name(slug):
            return {"ok": False, "error": "name must be 2-40 characters: lowercase letters, digits, and hyphens."}
        if not (brief or "").strip():
            return {"ok": False, "error": "brief is required: the full task for the child."}
        taken = [(row.get("nest") or {}).get("name", "") for row in sessions.children_of(parent_id)]
        slug = unique_name(slug, taken)
        parent_row = next((r for r in sessions.rows() if r.get("id") == parent_id), {})
        root = (parent_row.get("workspace_root") or parent_row.get("repo")
                or getattr(getattr(parent, "config", None), "repo", "") or "").strip()
        if not root or self.svc.add_worktree is None:
            return {"ok": False, "error": "Nested sessions need a project folder that is a git repository."}
        try:
            row = sessions.create_child(
                parent_id, title=(title or "").strip() or slug,
                nest={"name": slug, "todo": (todo or "").strip(), "status": "working",
                      "report": "", "updated": time.time(), "worktree": "", "branch": ""},
                workspace_root=root,
            )
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        sid = row["id"]
        # Each child writes only in its own worktree, so two children never
        # write the same files. No worktree means no child.
        try:
            tree = self.svc.add_worktree(root, branch_name(slug, sid))
            if not tree.get("path"):
                raise ValueError("no worktree path")
        except Exception as exc:
            sessions.delete(sid)
            reason = " ".join((str(exc).strip() or type(exc).__name__).split())[:300]
            return {"ok": False, "error": f"Could not make a git worktree for the child: {reason}"}
        sessions.update_nest(sid, worktree=tree["path"], branch=tree.get("branch", ""))
        linked = self._link_todo(parent, todo, sid, slug)
        message = child_preamble(parent_row.get("title") or "parent", slug,
                                 tree.get("branch", ""), tree.get("path", "")) + brief.strip()
        self._begin(sid, message, driver="parent")
        bump_rev()
        result = {
            "ok": True, "session_id": sid, "name": slug,
            "branch": tree.get("branch", ""), "worktree": tree["path"],
        }
        if linked is not None:
            result["todos"] = linked
        return result

    def message(self, parent: Any, child: str, text: str) -> dict:
        row = self._child_row(parent, child)
        if row is None:
            return {"ok": False, "error": f"No child session {child!r} of this session."}
        if not (text or "").strip():
            return {"ok": False, "error": "message is required."}
        sid = row["id"]
        runner = self.svc.get_runner(sid)
        busy = self.svc.runner_status(sid) in ("running", "attaching")
        with self._lock:
            starting = sid in self._in_flight and not busy
        if starting:
            return {"ok": False, "error": "The child is starting a turn. Send the message again in a few seconds."}
        if busy and runner is not None and hasattr(runner, "enqueue_steer"):
            try:
                runner.enqueue_steer(text.strip())
                return {"ok": True, "session_id": sid, "delivered": "steer"}
            except Exception as exc:
                return {"ok": False, "error": f"steer failed: {exc}"}
        self._begin(sid, text.strip(), driver="parent")
        bump_rev()
        return {"ok": True, "session_id": sid, "delivered": "turn"}

    def stop(self, parent: Any, child: str) -> dict:
        row = self._child_row(parent, child)
        if row is None:
            return {"ok": False, "error": f"No child session {child!r} of this session."}
        runner = self.svc.get_runner(row["id"])
        interrupt = getattr(runner, "interrupt", None)
        if callable(interrupt) and self.svc.runner_status(row["id"]) in ("running", "attaching"):
            try:
                interrupt()
            except Exception as exc:
                self.svc.diag("nested.stop", exc)
        self.svc.sessions.update_nest(row["id"], status="stopped", updated=time.time())
        bump_rev()
        return {"ok": True, "session_id": row["id"], "status": "stopped"}

    def children(self, parent_id: str) -> List[dict]:
        rows = self.svc.sessions.children_of(parent_id)
        rows.sort(key=lambda r: r.get("created") or 0)
        with self._lock:
            flying = set(self._in_flight)
        return [child_payload(r, self.svc.runner_status(r.get("id", "")), r.get("id") in flying)
                for r in rows]

    def in_flight(self, sid: str) -> bool:
        with self._lock:
            return sid in self._in_flight

    # -- turn hooks ----------------------------------------------------

    def _begin(self, sid: str, message: str, *, driver: str, resume: bool = False) -> None:
        with self._lock:
            self._tokens += 1
            token = self._tokens
            self._parent_driven[sid] = True
            self._in_flight[sid] = token
        if self.svc.sessions.nest_of(sid) is not None:
            self.svc.sessions.update_nest(sid, status="working", updated=time.time())
        self.svc.run_turn(sid, message, resume=resume, driver=driver, token=token)

    def _settle(self, sid: str) -> bool:
        """End the bookkeeping of the current turn. Returns parent_driven."""
        with self._lock:
            self._in_flight.pop(sid, None)
            return self._parent_driven.pop(sid, False)

    def turn_exited(self, sid: str, token: int, outcome: str) -> None:
        """The thread of a turn that ``_begin`` started ended.

        Normally ``finalize_turn`` already called ``on_turn_end``. When it did
        not (the turn thread ended some other way), end the turn here so the
        status never stays ``working`` and the parent still gets a report.
        """
        with self._lock:
            if self._in_flight.get(sid) != token:
                return
        self.on_turn_end(sid, self.svc.get_runner(sid), outcome=outcome or "failed")

    def on_turn_end(self, sid: str, runner: Any, *, outcome: str = "") -> None:
        """Called after every turn (``finalize_turn`` and the headless driver)."""
        if not sid:
            return
        parent_driven = self._settle(sid)
        nest = self.svc.sessions.nest_of(sid)
        if nest is not None and nest.get("parent_id"):
            self._record_child_turn(sid, nest, runner, outcome=outcome, parent_driven=parent_driven)
        elif not parent_driven and self.svc.sessions.nest_parent_state(sid):
            # A turn the user drove saw every delivered note, and it resets the
            # automatic-turn budget.
            self.svc.sessions.nest_parent_state(sid, auto_wakes=0, pending_wake=False)
        if self._has_inbox(sid):
            self.deliver(sid)

    def refused(self, sid: str, error: str) -> None:
        """A turn could not start (no free session slot, busy runner, build error)."""
        parent_driven = self._settle(sid)
        nest = self.svc.sessions.nest_of(sid)
        if nest is None or not nest.get("parent_id"):
            return
        text = " ".join(str(error or "The turn did not start.").split())[:REPORT_CHARS]
        now = time.time()
        self.svc.sessions.update_nest(sid, status="failed", report=text, updated=now)
        self.svc.sessions.push_nest_note(nest["parent_id"], {
            "id": uuid.uuid4().hex, "child_id": sid, "name": nest.get("name", ""), "status": "failed",
            "report": text, "driver": "parent" if parent_driven else "user", "at": now,
            "wake": parent_driven,
        })
        bump_rev()
        self.deliver(nest["parent_id"])

    def _record_child_turn(self, sid: str, nest: dict, runner: Any, *,
                           outcome: str, parent_driven: bool) -> None:
        stopped = bool(getattr(runner, "_stop_holds_idle", False)) or outcome == "stopped"
        status = "stopped" if stopped else ("failed" if outcome == "failed" else "done")
        history = list(getattr(runner, "_history", None) or []) if runner is not None else []
        report = report_from_history(history)
        now = time.time()
        self.svc.sessions.update_nest(sid, status=status, report=report, updated=now)
        driver = "parent" if parent_driven else "user"
        self.svc.sessions.push_nest_note(nest["parent_id"], {
            "id": uuid.uuid4().hex, "child_id": sid, "name": nest.get("name", ""), "status": status,
            "report": report, "driver": driver, "at": now,
            "wake": parent_driven and status in ("done", "failed"),
        })
        bump_rev()
        self.deliver(nest["parent_id"])

    def deliver(self, parent_id: str) -> bool:
        """Move inbox notes into an idle parent. Wake it when the rule allows.

        Each note arrives once. Notes leave the inbox only after the parent
        transcript that holds them is saved, and a note whose row is already
        in the transcript is not added again. A busy parent gets a retry.
        Returns True when a wake turn started.
        """
        sessions = self.svc.sessions
        runner = self.svc.get_runner(parent_id)
        if runner is None and self.svc.ensure_runner is not None and self._wants_wake(parent_id):
            try:
                runner = self.svc.ensure_runner(parent_id)
            except Exception as exc:
                self.svc.diag("nested.wake_runner", exc)
                return False
        if runner is None or not hasattr(runner, "_display_transcript"):
            return False
        if self.svc.runner_status(parent_id) in ("running", "attaching"):
            self._retry_later(parent_id)
            return False
        busy = getattr(runner, "_busy", None)
        if busy is None or not busy.acquire(blocking=False):
            self._retry_later(parent_id)
            return False
        appended = False
        notes: List[dict] = []
        try:
            notes = sessions.peek_nest_notes(parent_id)
            seen = {row.get("note_id") for row in runner._display_transcript
                    if isinstance(row, dict) and row.get("type") == "session_note"}
            fresh = [n for n in notes if note_id(n) not in seen]
            if fresh:
                history = runner._history
                text = "\n\n".join(note_text(n) for n in fresh)
                if history and history[-1].get("role") == "user" and isinstance(history[-1].get("content"), str):
                    history[-1]["content"] = history[-1]["content"].rstrip() + "\n\n" + text
                else:
                    history.append({"role": "user", "content": text})
                pending_events = getattr(runner, "_nest_note_events", None)
                if pending_events is None:
                    pending_events = []
                    runner._nest_note_events = pending_events
                for note in fresh:
                    row = note_display_row(note)
                    runner._display_transcript.append(row)
                    # The next turn streams the rows to a client that shows this session.
                    pending_events.append(dict(row))
                appended = True
                if any(n.get("wake") for n in fresh):
                    sessions.nest_parent_state(parent_id, pending_wake=True)
        finally:
            busy.release()
        if appended:
            try:
                self.svc.persist(parent_id, runner)
            except Exception as exc:
                # The notes stay in the inbox. The rows in memory stop a second copy.
                self.svc.diag("nested.persist_parent", exc)
                return False
            bump_rev()
        if notes:
            sessions.drop_nest_notes(parent_id, [note_id(n) for n in notes])
        state = sessions.nest_parent_state(parent_id)
        siblings_working = any(
            c["status"] == "working" for c in self.children(parent_id)
        )
        last_is_user = bool(runner._history) and runner._history[-1].get("role") == "user"
        if not should_wake(
            pending_wake=bool(state.get("pending_wake")) and last_is_user,
            siblings_working=siblings_working,
            parent_idle=True,
            auto_wakes=int(state.get("auto_wakes") or 0),
            stop_held=bool(getattr(runner, "_stop_holds_idle", False)),
        ):
            return False
        sessions.nest_parent_state(
            parent_id, pending_wake=False, auto_wakes=int(state.get("auto_wakes") or 0) + 1,
        )
        self._begin(parent_id, "", driver="wake", resume=True)
        return True

    def _retry_later(self, parent_id: str) -> None:
        """Try ``deliver`` again while the parent is busy (one thread per parent).

        The parent's own turn end delivers too. This covers a report that
        lands while that turn is closing.
        """
        if not self._has_inbox(parent_id):
            return
        with self._lock:
            if parent_id in self._retrying:
                return
            self._retrying.add(parent_id)

        def _run() -> None:
            try:
                for _ in range(RETRY_TRIES):
                    time.sleep(RETRY_SECONDS)
                    if not self._has_inbox(parent_id):
                        return
                    if self.svc.runner_status(parent_id) in ("running", "attaching"):
                        continue
                    runner = self.svc.get_runner(parent_id)
                    busy = getattr(runner, "_busy", None)
                    if busy is not None and busy.locked():
                        continue
                    with self._lock:
                        self._retrying.discard(parent_id)
                    self.deliver(parent_id)
                    return
            except Exception as exc:
                self.svc.diag("nested.deliver_retry", exc)
            finally:
                with self._lock:
                    self._retrying.discard(parent_id)

        threading.Thread(target=_run, name=f"nested-retry-{parent_id}", daemon=True).start()

    def reconcile_on_boot(self) -> List[str]:
        """A child still marked ``working`` lost its turn to a restart.

        Mark it ``stopped`` and send it one turn to continue. Returns the ids.
        """
        resumed = []
        for row in self.svc.sessions.nested_rows():
            nest = row.get("nest") or {}
            if not nest.get("parent_id") or nest.get("status") != "working":
                continue
            sid = row["id"]
            self.svc.sessions.update_nest(sid, status="stopped", updated=time.time())
            if os.environ.get("HARNESS_NESTED_RESUME_ON_BOOT", "1") != "0":
                self._begin(sid, "The app restarted during your last turn. Continue the task "
                                 "from where you stopped.", driver="parent")
                resumed.append(sid)
        if resumed:
            bump_rev()
        return resumed

    # -- helpers -------------------------------------------------------

    def _has_inbox(self, sid: str) -> bool:
        return any(r.get("id") == sid and r.get("nest_inbox") for r in self.svc.sessions.nested_rows())

    def _wants_wake(self, parent_id: str) -> bool:
        row = next((r for r in self.svc.sessions.rows() if r.get("id") == parent_id), None)
        if row is None:
            return False
        pending = any(n.get("wake") for n in row.get("nest_inbox") or [])
        if not pending:
            return False
        return not any(c["status"] == "working" for c in self.children(parent_id))

    def _child_row(self, parent: Any, child: str) -> Optional[dict]:
        parent_id = getattr(parent, "harness_session_id", "") or ""
        key = (child or "").strip()
        for row in self.svc.sessions.children_of(parent_id):
            if row.get("id") == key or (row.get("nest") or {}).get("name") == key:
                return row
        return None

    def _link_todo(self, parent: Any, todo: str, sid: str, name: str) -> Optional[dict]:
        link = getattr(parent, "link_todo_session", None)
        if not callable(link):
            return None
        try:
            return link(todo, sid, name)
        except Exception as exc:
            self.svc.diag("nested.link_todo", exc)
            return None


def with_live_status(rows: Any) -> Any:
    """Put the live child status into session list rows (``nest.status``).

    The stored status can say ``working`` after its turn died. The rail reads
    this list, so it must get the same status as the plan chips.
    """
    sup = _supervisor
    if sup is None or not isinstance(rows, list):
        return rows
    for row in rows:
        nest = row.get("nest") if isinstance(row, dict) else None
        if not isinstance(nest, dict) or not nest.get("parent_id"):
            continue
        sid = row.get("id", "")
        try:
            status = live_status(nest.get("status", ""), sup.svc.runner_status(sid), sup.in_flight(sid))
        except Exception:
            continue
        row["nest"] = {**nest, "status": status}
    return rows


def deliver_when_ready(session_id: str, get_runner: Callable[[str], Any]) -> None:
    """After a view switch: deliver waiting notes once the runner is built."""
    sup = _supervisor
    if sup is None:
        return
    rows = [r for r in sup.svc.sessions.nested_rows() if r.get("id") == session_id]
    if not rows or not rows[0].get("nest_inbox"):
        return

    def _run() -> None:
        runner = get_runner(session_id)
        wait = getattr(runner, "ensure_ready", None)
        if callable(wait) and not hasattr(runner, "_display_transcript"):
            try:
                wait(timeout=120.0)
            except Exception:
                return
        try:
            sup.deliver(session_id)
        except Exception as exc:
            sup.svc.diag("nested.deliver_after_switch", exc)

    threading.Thread(target=_run, name=f"nested-deliver-{session_id}", daemon=True).start()


def lock_worktree(path: str, reason: str) -> None:
    """Keep worktree cleanup away from a child's tree."""
    try:
        subprocess.run(["git", "-C", path, "worktree", "lock", "--reason", reason, path],
                       capture_output=True, text=True, timeout=10)
    except Exception:
        pass


_CODEGRAPH_SEED = ("codegraph.db", "config.json", ".gitignore")


def seed_codegraph(project: str, worktree: str) -> bool:
    """Copy the project's CodeGraph index into a new child worktree.

    Without it, the first CodeGraph read in the child (a switch to it, or its
    first turn) runs a full ``codegraph init --index`` of the worktree. The
    index keys files by path and content hash, so the copy needs only an
    incremental pass for the files the child changes. The directory time is set
    to 0 so the first staleness check runs that pass once. Clone copies on
    APFS cost no disk space. Returns False when the project has no index.
    """
    src = os.path.join(project, ".codegraph")
    if not os.path.isfile(os.path.join(src, "codegraph.db")):
        return False
    dst = os.path.join(worktree, ".codegraph")
    try:
        os.makedirs(dst, exist_ok=True)
        for name in _CODEGRAPH_SEED:
            path = os.path.join(src, name)
            if not os.path.isfile(path):
                continue
            target = os.path.join(dst, name)
            cloned = sys.platform == "darwin" and subprocess.run(
                ["cp", "-c", path, target], capture_output=True, timeout=30).returncode == 0
            if not cloned:
                shutil.copy2(path, target)
        os.utime(dst, (0, 0))
        return True
    except Exception:
        return False


_supervisor: Optional[NestedSupervisor] = None


def set_supervisor(supervisor: Optional[NestedSupervisor]) -> None:
    global _supervisor
    _supervisor = supervisor


def supervisor() -> Optional[NestedSupervisor]:
    return _supervisor
