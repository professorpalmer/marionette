"""Worker lifecycle reconciliation from a complete, identity-fenced task page."""

from typing import Iterable, Optional


def terminal_task_lifecycle(statuses: Iterable[str]) -> Optional[str]:
    statuses = set(statuses)
    if not statuses or not statuses <= {"complete", "failed", "skipped", "cancelled"}:
        return None
    if "failed" in statuses:
        return "failed"
    return "complete"
