"""Live per-hunk Agent vs External attribution anchored to CheckpointStore."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Literal, Optional

from .checkpoints import CHECKPOINT_GIT_TIMEOUT, CheckpointStore, _within
from .diffreview import parse_unified_diff

logger = logging.getLogger("pmharness.checkpoint_hunks")

SourceKind = Literal["agent", "external"]
HunkKind = Literal["added", "removed", "modified"]
DecisionKind = Literal["pending", "accepted", "reverted"]


def _parse_hunk_header(header: str) -> Optional[dict[str, int]]:
    import re

    m = re.match(r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s@@", header.strip())
    if not m:
        return None
    old_start = int(m.group(1))
    old_count = int(m.group(2) or "1")
    new_start = int(m.group(3))
    new_count = int(m.group(4) or "1")
    return {
        "old_start": old_start,
        "old_count": old_count,
        "new_start": new_start,
        "new_count": new_count,
    }


def _hunk_kind(hunk: dict[str, Any]) -> HunkKind:
    has_add = False
    has_del = False
    for line in hunk.get("lines") or []:
        if line.startswith("+"):
            has_add = True
        elif line.startswith("-"):
            has_del = True
    if has_add and not has_del:
        return "added"
    if has_del and not has_add:
        return "removed"
    return "modified"


def _hunk_new_line_numbers(hunk: dict[str, Any]) -> list[int]:
    header = _parse_hunk_header(hunk.get("header") or "")
    if not header:
        return []
    nums: list[int] = []
    cursor = header["new_start"]
    for line in hunk.get("lines") or []:
        if line.startswith("+"):
            nums.append(cursor)
            cursor += 1
        elif line.startswith(" "):
            nums.append(cursor)
            cursor += 1
        elif line.startswith("-"):
            continue
    return nums


def _classify_source(
    path: str,
    hunk: dict[str, Any],
    line_authorship: dict[str, dict[str, str]],
    write_sources: dict[str, str],
) -> SourceKind:
    path_map = line_authorship.get(path) or {}
    nums = _hunk_new_line_numbers(hunk)
    if nums:
        agent_hits = sum(1 for n in nums if path_map.get(str(n)) == "agent")
        external_hits = sum(1 for n in nums if path_map.get(str(n)) == "external")
        if agent_hits and not external_hits:
            return "agent"
        if external_hits and not agent_hits:
            return "external"
        if agent_hits >= external_hits and agent_hits:
            return "agent"
        if external_hits:
            return "external"
    kind = _hunk_kind(hunk)
    if kind == "removed":
        src = write_sources.get(path)
        if src in ("agent", "external"):
            return src  # type: ignore[return-value]
    src = write_sources.get(path)
    if src == "agent":
        return "agent"
    return "external"


def _update_line_authorship(
    old_text: str,
    new_text: str,
    source: SourceKind,
    existing: dict[str, str],
) -> dict[str, str]:
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    out = dict(existing)
    import difflib

    matcher = difflib.SequenceMatcher(None, old_lines, new_lines)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "insert"):
            for j in range(j1, j2):
                out[str(j + 1)] = source
        elif tag == "delete":
            # Drop authorship for deleted new-side lines above the deletion.
            for key in list(out.keys()):
                try:
                    ln = int(key)
                except ValueError:
                    continue
                if ln > j1:
                    out.pop(key, None)
    # Re-index after structural edits so line numbers stay aligned with file.
    if old_lines != new_lines:
        remapped: dict[str, str] = {}
        for j, line in enumerate(new_lines):
            ln = j + 1
            if str(ln) in out:
                remapped[str(ln)] = out[str(ln)]
            elif j < len(old_lines) and old_lines[j] == line and str(ln) in existing:
                remapped[str(ln)] = existing[str(ln)]
        # Preserve explicit marks from this write on changed rows.
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag in ("replace", "insert"):
                for j in range(j1, j2):
                    remapped[str(j + 1)] = source
        out = remapped
    return out


def _read_repo_file(repo: str, rel_path: str) -> str:
    abs_path = os.path.join(repo, rel_path)
    if not os.path.isfile(abs_path):
        return ""
    with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def _git_show_file(repo: str, commit_sha: str, rel_path: str) -> Optional[str]:
    try:
        res = subprocess.run(
            ["git", "show", f"{commit_sha}:{rel_path}"],
            cwd=repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=CHECKPOINT_GIT_TIMEOUT,
        )
        if res.returncode == 0:
            return res.stdout
    except Exception:
        pass
    return None


def _revert_hunk_in_text(current: str, hunk: dict[str, Any]) -> str:
    header = _parse_hunk_header(hunk.get("header") or "")
    if not header:
        return current
    lines = current.splitlines()
    old_chunk: list[str] = []
    for raw in hunk.get("lines") or []:
        if not raw:
            continue
        if raw.startswith("-") or raw.startswith(" "):
            old_chunk.append(raw[1:].rstrip("\n"))
        elif raw.startswith("+"):
            continue
    start = max(0, header["new_start"] - 1)
    end = start + header["new_count"]
    new_lines = lines[:start] + old_chunk + lines[end:]
    trailing_newline = current.endswith("\n")
    out = "\n".join(new_lines)
    if trailing_newline:
        out += "\n"
    return out


class CheckpointHunkTracker:
    """Track live unified-diff hunks vs a checkpoint baseline with authorship."""

    _lock = threading.Lock()

    def __init__(self, store: CheckpointStore, session_id: Optional[str] = None):
        self._store = store
        self._session_id = (session_id or store.session_id or "").strip() or None
        self._repo = store.repo
        self._enabled = bool(store._enabled and self._repo)
        self._meta_file: Optional[Path] = None
        if self._enabled and store._repo_hash:
            sid = self._session_id or "default"
            meta_dir = Path.home() / ".pmharness" / "checkpoints"
            self._meta_file = meta_dir / f"{store._repo_hash}_{sid}_hunks.json"

    def _default_state(self) -> dict[str, Any]:
        return {
            "baseline_id": None,
            "snapshots": {},
            "write_sources": {},
            "line_authorship": {},
            "decisions": {},
            "updated_at": int(time.time()),
        }

    def _load_state(self) -> dict[str, Any]:
        if not self._meta_file or not self._meta_file.exists():
            return self._default_state()
        try:
            with open(self._meta_file, "r", encoding="utf-8", errors="replace") as f:
                data = json.load(f)
            if isinstance(data, dict):
                base = self._default_state()
                base.update(data)
                return base
        except Exception:
            pass
        return self._default_state()

    def _save_state(self, state: dict[str, Any]) -> None:
        if not self._meta_file:
            return
        try:
            self._meta_file.parent.mkdir(parents=True, exist_ok=True)
            state["updated_at"] = int(time.time())
            temp = str(self._meta_file) + ".tmp"
            with open(temp, "w", encoding="utf-8", newline="\n") as f:
                json.dump(state, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, self._meta_file)
        except Exception as exc:
            logger.warning("failed to save hunk tracker state: %s", exc)

    def ensure_baseline(self) -> Optional[str]:
        if not self._enabled:
            return None
        with self._lock:
            state = self._load_state()
            if state.get("baseline_id"):
                return str(state["baseline_id"])
            listed = self._store.list(session_id=self._session_id)
            if listed:
                cp_id = str(listed[-1]["id"])
                state["baseline_id"] = cp_id
                self._save_state(state)
                return cp_id
            cp_id = self._store.snapshot(
                label="Live hunk baseline",
                trigger="hunk_baseline",
                session_id=self._session_id,
            )
            if cp_id:
                state["baseline_id"] = cp_id
                self._save_state(state)
            return cp_id

    def _record_write(self, rel_path: str, source: SourceKind) -> None:
        if not self._enabled or not rel_path:
            return
        rel = rel_path.replace("\\", "/").lstrip("./")
        repo = self._repo
        if not repo:
            return
        abs_path = os.path.realpath(os.path.join(repo, rel))
        if not _within(repo, abs_path):
            return
        self.ensure_baseline()
        new_text = _read_repo_file(repo, rel)
        with self._lock:
            state = self._load_state()
            snapshots: dict[str, str] = state.setdefault("snapshots", {})
            old_text = snapshots.get(rel, _git_show_file(repo, str(state.get("baseline_id")), rel) or "")
            line_auth: dict[str, dict[str, str]] = state.setdefault("line_authorship", {})
            line_auth[rel] = _update_line_authorship(
                old_text,
                new_text,
                source,
                line_auth.get(rel) or {},
            )
            snapshots[rel] = new_text
            write_sources: dict[str, str] = state.setdefault("write_sources", {})
            write_sources[rel] = source
            self._save_state(state)

    def record_agent_write(self, rel_path: str) -> None:
        """Mark a workspace write as agent-authored."""
        self._record_write(rel_path, "agent")

    def fs_notify(self, rel_path: str) -> None:
        """Mark an external filesystem change (manual editor save, etc.)."""
        self._record_write(rel_path, "external")

    def recompute(self) -> dict[str, Any]:
        if not self._enabled:
            return {"ok": False, "error": "Hunk tracker disabled: not a git worktree"}
        baseline_id = self.ensure_baseline()
        if not baseline_id:
            return {"ok": False, "error": "Failed to establish hunk baseline checkpoint"}
        repo = self._repo
        assert repo is not None
        try:
            diff_res = subprocess.run(
                ["git", "diff", baseline_id, "--", "."],
                cwd=repo,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=CHECKPOINT_GIT_TIMEOUT,
            )
            diff_text = diff_res.stdout or ""
        except Exception as exc:
            return {"ok": False, "error": f"Failed to diff baseline: {exc}"}

        state = self._load_state()
        line_auth = state.get("line_authorship") or {}
        write_sources = state.get("write_sources") or {}
        decisions: dict[str, str] = state.get("decisions") or {}

        files_out: list[dict[str, Any]] = []
        parsed = parse_unified_diff(diff_text)
        for f in parsed:
            path = f.get("path") or ""
            if not path:
                continue
            hunks_out: list[dict[str, Any]] = []
            for h in f.get("hunks") or []:
                hid = str(h.get("id") or "")
                status = decisions.get(hid, "pending")
                if status == "reverted":
                    continue
                source = _classify_source(path, h, line_auth, write_sources)
                hunks_out.append(
                    {
                        "id": hid,
                        "header": h.get("header") or "",
                        "lines": h.get("lines") or [],
                        "kind": _hunk_kind(h),
                        "source": source,
                        "status": status,
                    }
                )
            if hunks_out:
                files_out.append({"path": path, "hunks": hunks_out})

        # Whole-file adds/removes not represented as hunks still surface via diff names.
        try:
            names_res = subprocess.run(
                ["git", "diff", "--name-status", baseline_id, "--", "."],
                cwd=repo,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=CHECKPOINT_GIT_TIMEOUT,
            )
        except Exception:
            names_res = None
        listed_paths = {f["path"] for f in files_out}
        if names_res and names_res.returncode == 0:
            file_index = len(parsed)
            hunk_index = 0
            for line in names_res.stdout.splitlines():
                parts = line.split("\t", 1)
                if len(parts) != 2:
                    continue
                code, path = parts[0].strip(), parts[1].strip()
                if path in listed_paths:
                    continue
                if code.startswith("A"):
                    hid = f"{file_index}:{hunk_index}"
                    file_index += 1
                    source = write_sources.get(path, "external")
                    if (line_auth.get(path) or {}) and all(
                        v == "agent" for v in (line_auth.get(path) or {}).values()
                    ):
                        source = "agent"
                    status = decisions.get(hid, "pending")
                    if status != "reverted":
                        files_out.append(
                            {
                                "path": path,
                                "hunks": [
                                    {
                                        "id": hid,
                                        "header": "@@ file added @@",
                                        "lines": [],
                                        "kind": "added",
                                        "source": source,
                                        "status": status,
                                    }
                                ],
                            }
                        )
                elif code.startswith("D"):
                    hid = f"{file_index}:{hunk_index}"
                    file_index += 1
                    source = write_sources.get(path, "external")
                    status = decisions.get(hid, "pending")
                    if status != "reverted":
                        files_out.append(
                            {
                                "path": path,
                                "hunks": [
                                    {
                                        "id": hid,
                                        "header": "@@ file removed @@",
                                        "lines": [],
                                        "kind": "removed",
                                        "source": source,
                                        "status": status,
                                    }
                                ],
                            }
                        )

        files_out.sort(key=lambda row: row.get("path") or "")
        # Untracked adds do not appear in `git diff <commit>` — surface them explicitly.
        try:
            status_res = subprocess.run(
                ["git", "status", "--porcelain", "-u", "--", "."],
                cwd=repo,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=CHECKPOINT_GIT_TIMEOUT,
            )
        except Exception:
            status_res = None
        listed_paths = {f["path"] for f in files_out}
        if status_res and status_res.returncode == 0:
            file_index = len(parsed) + len(listed_paths)
            hunk_index = 0
            for line in status_res.stdout.splitlines():
                if not line.startswith("?? "):
                    continue
                path = line[3:].strip()
                if not path or path in listed_paths:
                    continue
                hid = f"{file_index}:{hunk_index}"
                file_index += 1
                source = write_sources.get(path, "external")
                path_lines = line_auth.get(path) or {}
                if path_lines and all(v == "agent" for v in path_lines.values()):
                    source = "agent"
                status = decisions.get(hid, "pending")
                if status == "reverted":
                    continue
                files_out.append(
                    {
                        "path": path,
                        "hunks": [
                            {
                                "id": hid,
                                "header": "@@ file added @@",
                                "lines": [],
                                "kind": "added",
                                "source": source,
                                "status": status,
                            }
                        ],
                    }
                )
                listed_paths.add(path)

        files_out.sort(key=lambda row: row.get("path") or "")
        return {
            "ok": True,
            "baseline_id": baseline_id,
            "files": files_out,
            "diff": diff_text,
        }

    def accept_hunk(self, hunk_id: str) -> dict[str, Any]:
        if not self._enabled:
            return {"ok": False, "error": "Hunk tracker disabled"}
        hid = (hunk_id or "").strip()
        if not hid:
            return {"ok": False, "error": "Missing hunk id"}
        with self._lock:
            state = self._load_state()
            decisions: dict[str, str] = state.setdefault("decisions", {})
            decisions[hid] = "accepted"
            self._save_state(state)
        return {"ok": True, "hunk_id": hid, "status": "accepted"}

    def revert_hunk(self, hunk_id: str) -> dict[str, Any]:
        if not self._enabled:
            return {"ok": False, "error": "Hunk tracker disabled"}
        hid = (hunk_id or "").strip()
        if not hid:
            return {"ok": False, "error": "Missing hunk id"}
        live = self.recompute()
        if not live.get("ok"):
            return live
        target: Optional[dict[str, Any]] = None
        target_path = ""
        for f in live.get("files") or []:
            for h in f.get("hunks") or []:
                if h.get("id") == hid:
                    target = h
                    target_path = f.get("path") or ""
                    break
            if target:
                break
        if target is None or not target_path:
            return {"ok": False, "error": f"Hunk {hid} not found"}

        repo = self._repo
        assert repo is not None
        baseline_id = str(live.get("baseline_id") or "")
        kind = target.get("kind")
        try:
            if kind == "added":
                abs_path = os.path.join(repo, target_path)
                if _within(repo, abs_path) and os.path.isfile(abs_path):
                    os.unlink(abs_path)
            elif kind == "removed":
                baseline_text = _git_show_file(repo, baseline_id, target_path)
                if baseline_text is None:
                    return {"ok": False, "error": "Baseline file missing for revert"}
                abs_path = os.path.join(repo, target_path)
                if not _within(repo, abs_path):
                    return {"ok": False, "error": "Path escapes workspace"}
                os.makedirs(os.path.dirname(abs_path), exist_ok=True)
                with open(abs_path, "w", encoding="utf-8", newline="\n") as f:
                    f.write(baseline_text)
            else:
                current = _read_repo_file(repo, target_path)
                baseline_text = _git_show_file(repo, baseline_id, target_path) or ""
                if target.get("header") == "@@ file added @@":
                    abs_path = os.path.join(repo, target_path)
                    if _within(repo, abs_path) and os.path.isfile(abs_path):
                        os.unlink(abs_path)
                else:
                    reverted = _revert_hunk_in_text(current, target)
                    abs_path = os.path.join(repo, target_path)
                    if not _within(repo, abs_path):
                        return {"ok": False, "error": "Path escapes workspace"}
                    with open(abs_path, "w", encoding="utf-8", newline="\n") as f:
                        f.write(reverted)
                # Refresh snapshot cache for subsequent attribution.
                with self._lock:
                    state = self._load_state()
                    snapshots: dict[str, str] = state.setdefault("snapshots", {})
                    snapshots[target_path] = _read_repo_file(repo, target_path)
                    self._save_state(state)
        except Exception as exc:
            return {"ok": False, "error": f"Revert failed: {exc}"}

        with self._lock:
            state = self._load_state()
            decisions: dict[str, str] = state.setdefault("decisions", {})
            decisions[hid] = "reverted"
            self._save_state(state)
        return {"ok": True, "hunk_id": hid, "status": "reverted", "path": target_path}


def hunk_tracker_for_store(
    store: CheckpointStore,
    session_id: Optional[str] = None,
) -> CheckpointHunkTracker:
    sid = session_id if session_id is not None else store.session_id
    return CheckpointHunkTracker(store, session_id=sid)


def record_agent_write(repo: Optional[str], rel_path: str, session_id: Optional[str] = None) -> None:
    if not repo or not rel_path:
        return
    try:
        store = CheckpointStore(repo, session_id=session_id)
        hunk_tracker_for_store(store, session_id=session_id).record_agent_write(rel_path)
    except Exception as exc:
        logger.debug("record_agent_write skipped: %s", exc)


def fs_notify(repo: Optional[str], rel_path: str, session_id: Optional[str] = None) -> None:
    if not repo or not rel_path:
        return
    try:
        store = CheckpointStore(repo, session_id=session_id)
        hunk_tracker_for_store(store, session_id=session_id).fs_notify(rel_path)
    except Exception as exc:
        logger.debug("fs_notify skipped: %s", exc)
