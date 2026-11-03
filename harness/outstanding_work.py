"""Outstanding work section for the compaction residual.

After a compact the pilot lost sight of background commands, workers and
flows it started earlier, then started them again or stopped waiting. This
section names the work of this session that was still live at compact time.
It reads in-memory session state only and never starts or stops anything.
"""
from __future__ import annotations

import time
from typing import Any, List, Tuple

OUTSTANDING_HEADING = (
    "### Outstanding work (still running when this history was compacted)"
)
_NOTE_LINE = (
    "Do not start these again. Wait for their results or check their status."
)
_MAX_ITEMS = 8
_MAX_CHARS = 1500
_LABEL_CHARS = 100
# An allowlist: a status this module does not know is not shown as live.
_LIVE_JOB_STATUSES = frozenset({"registered", "queued", "pending", "running", "starting"})
_LIVE_FLOW_STATUSES = frozenset({"running", "waiting", "interrupted"})
_KIND_NAMES = {
    "run_command": "background command",
    "command_batch": "command batch",
    "parallel_wave": "parallel wave",
}


def _started(ts: Any) -> str:
    try:
        value = float(ts)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(value))


def _label(text: Any) -> str:
    from .command_jobs import secret_free_command_preview

    # Goals and commands can carry keys. The residual goes to the provider.
    return secret_free_command_preview(str(text or ""), max_chars=_LABEL_CHARS)


def _job_items(session: Any, sid: str) -> List[Tuple[float, str]]:
    lock = getattr(session, "_local_jobs_lock", None)
    jobs = getattr(session, "_local_jobs", None)
    if lock is None or not isinstance(jobs, dict):
        return []
    with lock:
        rows = [dict(row) for row in jobs.values() if isinstance(row, dict)]
    items: List[Tuple[float, str]] = []
    for row in rows:
        if str(row.get("session_id") or "") != sid:
            continue
        status = str(row.get("status") or "")
        if status not in _LIVE_JOB_STATUSES:
            continue
        # The parent row names a batch or wave once; its children would
        # crowd out other work.
        if row.get("batch_id") or row.get("parent_wave_id"):
            continue
        kind_key = str(row.get("job_kind") or "")
        kind = _KIND_NAMES.get(kind_key) or str(row.get("role") or "job") + " job"
        text = row.get("command_preview") if kind_key == "run_command" else row.get("goal")
        ts = row.get("started_at") or row.get("created_at")
        started = _started(ts)
        line = "- %s %s (%s): %s" % (kind, row.get("id") or "?", status, _label(text))
        if started:
            line += " [started %s]" % started
        try:
            order = float(ts or 0)
        except (TypeError, ValueError):
            order = 0.0
        items.append((order, line))
    return items


def _flow_items(session: Any) -> List[Tuple[float, str]]:
    runs = getattr(session, "_flow_runs", None)
    if not isinstance(runs, dict):
        return []
    items: List[Tuple[float, str]] = []
    for run_id, record in list(runs.items()):
        if not isinstance(record, dict):
            continue
        status = str(record.get("status") or "")
        if status not in _LIVE_FLOW_STATUSES:
            continue
        line = "- flow run %s (%s): %s" % (run_id, status, _label(record.get("objective")))
        # Flow records keep no start time; sort them after timed jobs.
        items.append((0.0, line))
    return items


def outstanding_work_block(session: Any) -> str:
    """The section text, or "" when nothing is live or a read fails."""
    try:
        sid = str(getattr(session, "harness_session_id", "") or "")
        items = _job_items(session, sid) + _flow_items(session)
        if not items:
            return ""
        items.sort(key=lambda item: item[0], reverse=True)
        lines = [OUTSTANDING_HEADING, _NOTE_LINE]
        size = len(lines[0]) + len(lines[1]) + 1
        shown = 0
        for _, line in items[:_MAX_ITEMS]:
            # Keep room for the "and N more" line inside the cap.
            if size + 1 + len(line) > _MAX_CHARS - 24:
                break
            lines.append(line)
            size += 1 + len(line)
            shown += 1
        if not shown:
            return ""
        rest = len(items) - shown
        if rest > 0:
            lines.append("- and %d more" % rest)
        return "\n".join(lines)
    except Exception:
        # The compaction path must never raise for this optional section.
        return ""
