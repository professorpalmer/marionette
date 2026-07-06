"""Read-only L0-L3 memory layer snapshot helpers (Wave 7, v1).

Classifies and counts durable vs hot context without blocking the send path.
Char-based L0 proxy; per-layer failures return empty counts for that layer only.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

LAYER_IDS = ("L0", "L1", "L2", "L3")

JOURNAL_FILENAME = "memory_layers.jsonl"

_L1_DB_NAMES = frozenset(
    {
        "spill_index.sqlite",
        "eval_history.sqlite",
        "tool_output_savings.sqlite",
        "history_compaction.sqlite",
    }
)


def _empty_layer() -> dict[str, Any]:
    return {"bytes": 0, "entries": 0, "components": {}}


def _safe_file_size(path: str) -> int:
    try:
        if path and os.path.isfile(path):
            return int(os.path.getsize(path))
    except Exception:
        pass
    return 0


def _dir_file_bytes(directory: str) -> tuple[int, int]:
    """Return (total_bytes, file_count) for regular files under directory."""
    if not directory or not os.path.isdir(directory):
        return 0, 0
    total = 0
    count = 0
    try:
        for name in os.listdir(directory):
            path = os.path.join(directory, name)
            if os.path.isfile(path):
                total += _safe_file_size(path)
                count += 1
    except Exception:
        return 0, 0
    return total, count


def _message_char_len(message: dict) -> int:
    chars = len(message.get("content") or "")
    role = message.get("role") or ""
    if message.get("tool_calls"):
        for tc in message["tool_calls"]:
            func = tc.get("function") or {}
            chars += len(func.get("name") or "") + len(func.get("arguments") or "") + 30
    elif role == "tool":
        chars += len(message.get("tool_call_id") or "") + 30
    return chars


def estimate_l0_hot_chars(conversation) -> int:
    """Sum char lengths of messages in the active history window for next send()."""
    try:
        history = getattr(conversation, "_history", None) or []
    except Exception:
        return 0
    total = 0
    try:
        for message in history:
            if isinstance(message, dict):
                total += _message_char_len(message)
    except Exception:
        return 0
    return total


def _count_turn_context_lines(state_dir: str, session_id: str) -> tuple[int, int]:
    """Return (line_count, byte_count) for session rows in turn_context.jsonl."""
    from .turn_context import JOURNAL_FILENAME as TURN_JOURNAL

    path = os.path.join(os.path.abspath(state_dir), TURN_JOURNAL)
    if not os.path.isfile(path):
        return 0, 0
    sid = session_id or "default"
    lines = 0
    nbytes = 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except Exception:
                    continue
                if not isinstance(record, dict) or record.get("session_id") != sid:
                    continue
                lines += 1
                nbytes += len(raw.encode("utf-8", errors="replace"))
    except Exception:
        return 0, 0
    return lines, nbytes


def _spill_session_stats(state_dir: str, session_id: str) -> tuple[int, int, int]:
    """Return (entry_count, indexed_chars, spill_file_bytes) for one session."""
    from .spill_registry import list_spills

    try:
        rows = list_spills(state_dir, session_id=session_id or None)
    except Exception:
        return 0, 0, 0
    chars = 0
    file_bytes = 0
    for row in rows:
        chars += int(row.get("chars") or 0)
        file_bytes += _safe_file_size(str(row.get("path") or ""))
    return len(rows), chars, file_bytes


def _savings_entry_count(state_dir: str, session_id: str) -> int:
    try:
        from .tool_output_savings import get_ledger

        summary = get_ledger(state_dir).summarize(session_id=session_id or None)
        return int(summary.record_count or 0)
    except Exception:
        return 0


def measure_l1_session(state_dir: str, session_id: str) -> dict[str, Any]:
    """Counts from session-scoped state-dir artifacts."""
    if not state_dir:
        return _empty_layer()
    try:
        sid = session_id or "default"
        turn_lines, turn_bytes = _count_turn_context_lines(state_dir, sid)
        spill_entries, spill_chars, spill_file_bytes = _spill_session_stats(state_dir, sid)
        spill_index_bytes = _safe_file_size(
            os.path.join(os.path.abspath(state_dir), "spill_index.sqlite")
        )
        eval_bytes = _safe_file_size(
            os.path.join(os.path.abspath(state_dir), "eval_history.sqlite")
        )
        savings_entries = _savings_entry_count(state_dir, sid)
        savings_bytes = _safe_file_size(
            os.path.join(os.path.abspath(state_dir), "tool_output_savings.sqlite")
        )
        results_bytes, results_files = _dir_file_bytes(
            os.path.join(os.path.abspath(state_dir), "pmharness-results")
        )

        from .eval_history import summarize_eval_history

        eval_entries, _failed = summarize_eval_history(state_dir, sid)

        components = {
            "turn_context_lines": turn_lines,
            "turn_context_bytes": turn_bytes,
            "spill_entries": spill_entries,
            "spill_chars": spill_chars,
            "spill_file_bytes": spill_file_bytes,
            "spill_index_bytes": spill_index_bytes,
            "eval_entries": eval_entries,
            "eval_db_bytes": eval_bytes,
            "savings_entries": savings_entries,
            "savings_db_bytes": savings_bytes,
            "pmharness_results_bytes": results_bytes,
            "pmharness_results_files": results_files,
        }
        entries = (
            turn_lines
            + spill_entries
            + eval_entries
            + savings_entries
            + results_files
        )
        nbytes = (
            turn_bytes
            + spill_file_bytes
            + spill_index_bytes
            + eval_bytes
            + savings_bytes
            + results_bytes
        )
        return {"bytes": nbytes, "entries": entries, "components": components}
    except Exception:
        return _empty_layer()


def measure_l2_workspace(state_dir: str, repo: str = "") -> dict[str, Any]:
    """Best-effort workspace-durable counts; zeros when stores are absent."""
    if not state_dir and not repo:
        return _empty_layer()
    try:
        components: dict[str, int] = {}
        nbytes = 0
        entries = 0

        memory_paths = []
        if state_dir:
            memory_paths.append(os.path.join(os.path.abspath(state_dir), "memory.json"))
        try:
            from .memory_store import MEMORY_PATH

            memory_paths.append(str(MEMORY_PATH))
        except Exception:
            pass
        seen = set()
        for path in memory_paths:
            if path in seen:
                continue
            seen.add(path)
            size = _safe_file_size(path)
            if size > 0:
                components["memory_json_bytes"] = components.get("memory_json_bytes", 0) + size
                entries += 1
                nbytes += size

        if state_dir:
            for name in ("swarm_local_jobs.json", "prompt_queue.json", "harness_sessions.json"):
                path = os.path.join(os.path.abspath(state_dir), name)
                size = _safe_file_size(path)
                if size > 0:
                    key = name.replace(".", "_") + "_bytes"
                    components[key] = size
                    nbytes += size
                    entries += 1

            try:
                for name in os.listdir(os.path.abspath(state_dir)):
                    if not name.endswith(".sqlite") or name in _L1_DB_NAMES:
                        continue
                    path = os.path.join(os.path.abspath(state_dir), name)
                    size = _safe_file_size(path)
                    if size > 0:
                        components["job_store_bytes"] = components.get("job_store_bytes", 0) + size
                        nbytes += size
                        entries += 1
            except Exception:
                pass

        if repo:
            codegraph_dir = os.path.join(os.path.abspath(repo), ".codegraph")
            cg_bytes, cg_files = _dir_file_bytes(codegraph_dir)
            if cg_bytes > 0:
                components["codegraph_bytes"] = cg_bytes
                components["codegraph_files"] = cg_files
                nbytes += cg_bytes
                entries += cg_files

        return {"bytes": nbytes, "entries": entries, "components": components}
    except Exception:
        return _empty_layer()


def _spill_retention_days() -> int:
    raw = os.environ.get("HARNESS_SPILL_RETENTION_DAYS", "").strip().lower()
    if not raw or raw in ("0", "off", "none", "forever"):
        return 0
    try:
        days = int(raw)
    except ValueError:
        return 0
    return max(0, days)


def measure_l3_cold(state_dir: str, session_id: str) -> dict[str, Any]:
    """Compaction journal plus expired-sweep-eligible spill rows (count only)."""
    if not state_dir:
        return _empty_layer()
    try:
        sid = session_id or "default"
        from .history_compaction_journal import summarize_history_compactions

        summary = summarize_history_compactions(state_dir, sid)
        compaction_db_bytes = _safe_file_size(
            os.path.join(os.path.abspath(state_dir), "history_compaction.sqlite")
        )
        compaction_bytes = max(0, summary.chars_before - summary.chars_after)
        expired_spills = 0
        retention_days = _spill_retention_days()
        if retention_days > 0:
            from .spill_registry import list_spills

            cutoff = time.time() - (retention_days * 86400)
            for row in list_spills(state_dir, session_id=sid):
                try:
                    if float(row.get("ts") or 0) < cutoff:
                        expired_spills += 1
                except Exception:
                    continue

        components = {
            "compaction_records": summary.record_count,
            "compaction_chars_before": summary.chars_before,
            "compaction_chars_after": summary.chars_after,
            "compaction_db_bytes": compaction_db_bytes,
            "expired_spill_rows": expired_spills,
            "spill_retention_days": retention_days,
        }
        entries = summary.record_count + expired_spills
        nbytes = compaction_bytes + compaction_db_bytes
        return {"bytes": nbytes, "entries": entries, "components": components}
    except Exception:
        return _empty_layer()


def snapshot_memory_layers(
    conversation,
    state_dir: str,
    session_id: str,
    repo: str = "",
) -> dict[str, Any]:
    """Assemble a read-only L0-L3 snapshot dict."""
    snapshot_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    l0_chars = estimate_l0_hot_chars(conversation)
    l0_entries = 0
    try:
        history = getattr(conversation, "_history", None) or []
        l0_entries = max(0, len(history) - 1) if history else 0
    except Exception:
        l0_entries = 0
    return {
        "L0": {"bytes": l0_chars, "entries": l0_entries},
        "L1": measure_l1_session(state_dir, session_id),
        "L2": measure_l2_workspace(state_dir, repo),
        "L3": measure_l3_cold(state_dir, session_id),
        "snapshot_at": snapshot_at,
    }
