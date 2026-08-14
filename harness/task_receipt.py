"""Compact MICRO/STANDARD task receipts (not a swarm artifact tree).

Append-only JSONL under ``{state_dir}/task_receipts.jsonl``. Hot-path helpers
never raise — a failed write must not block chat turns.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, List, Optional

JSONL_FILENAME = "task_receipts.jsonl"
PROMPT_HASH_LEN = 16
GIT_TIMEOUT_S = 5


@dataclass
class TaskReceipt:
    """One compact task receipt row.

    ``task_id`` is required; every other field is optional and omitted from
    serialized dicts when empty.
    """

    task_id: str
    profile: Optional[str] = None
    profile_source: Optional[str] = None
    escalated_from: Optional[str] = None
    model: Optional[str] = None
    adapter: Optional[str] = None
    prompt_hash: Optional[str] = None
    repo: Optional[str] = None
    branch: Optional[str] = None
    changed_files: Optional[List[str]] = field(default=None)
    patch_hash: Optional[str] = None
    verification: Optional[str] = None
    created_at: Optional[str] = None


def prompt_hash(message: str) -> str:
    """Return a short sha256 hex digest of the user message (16 hex chars)."""
    raw = message if isinstance(message, str) else ""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:PROMPT_HASH_LEN]


def git_branch(repo: str) -> str:
    """Best-effort current branch name; empty string on any failure."""
    if not repo or not isinstance(repo, str):
        return ""
    try:
        path = os.path.abspath(repo)
        if not os.path.isdir(path):
            return ""
        proc = subprocess.run(
            ["git", "-C", path, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT_S,
        )
        if proc.returncode != 0:
            return ""
        return (proc.stdout or "").strip()
    except Exception:
        return ""


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and value == "":
        return True
    if isinstance(value, (list, tuple, dict)) and len(value) == 0:
        return True
    return False


def build_receipt(**kwargs: Any) -> dict:
    """Build a receipt dict, omitting empty / missing values.

    Accepts TaskReceipt field names as kwargs. ``task_id`` should be provided
    by the caller; if absent the dict simply omits it. When ``created_at`` is
    not supplied, stamps UTC ISO-8601 now.
    """
    data = dict(kwargs)
    if "created_at" not in data or _is_empty(data.get("created_at")):
        data["created_at"] = (
            datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        )
    out: dict = {}
    for key, value in data.items():
        if _is_empty(value):
            continue
        out[key] = value
    return out


def compute_patch_hash(changed_files: Optional[List[str]]) -> str:
    """Sha256 of concatenated ``path\\0mtime`` for each existing path, else ``\"\"``."""
    if not changed_files:
        return ""
    parts: List[str] = []
    for raw in changed_files:
        if not isinstance(raw, str) or not raw:
            continue
        try:
            mtime = os.path.getmtime(raw)
        except OSError:
            continue
        parts.append("{0}\0{1}".format(raw, mtime))
    if not parts:
        return ""
    blob = "\n".join(parts).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def append_receipt(state_dir: str, receipt: Any) -> None:
    """Append one receipt as a JSONL line under ``state_dir``. Never raises."""
    try:
        if isinstance(receipt, TaskReceipt):
            payload = build_receipt(**asdict(receipt))
        elif isinstance(receipt, dict):
            payload = build_receipt(**receipt)
        else:
            return
        if not payload:
            return
        root = os.path.abspath(str(state_dir))
        os.makedirs(root, exist_ok=True)
        path = os.path.join(root, JSONL_FILENAME)
        line = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")
    except Exception:
        return


def load_receipts(state_dir: str, limit: int = 20) -> list:
    """Load up to ``limit`` most recent receipts (chronological order)."""
    try:
        if limit is None or int(limit) <= 0:
            return []
        limit = int(limit)
        path = os.path.join(os.path.abspath(str(state_dir)), JSONL_FILENAME)
        if not os.path.isfile(path):
            return []
        rows: List[dict] = []
        with open(path, encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if isinstance(rec, dict):
                    rows.append(rec)
        if len(rows) <= limit:
            return rows
        return rows[-limit:]
    except Exception:
        return []
