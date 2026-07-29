from __future__ import annotations

"""Seed a managed edit worktree with live-tree files the goal needs.

``git worktree add`` checks out HEAD. Untracked (and dirty-but-referenced)
files in the live repo are invisible to the worker -- historically producing
empty diffs, hallucinated paths like ``C:\\dev\\null``, and "file not found"
failures. Copy any goal-referenced paths that exist in the live tree into the
worktree before the edit engine runs.

Seeding is dynamic: explicit path tokens in the goal AND dirty/untracked files
whose path components match significant goal words (so "fix the kotoba ad"
still seeds ``addons/kotoba/...`` when those files are on disk in the indexed
workspace).

Copy strategy (``HARNESS_WORKTREE_COPY_STRATEGY``):

  - ``auto`` (default): probe once per process for CoW clone support; use
    reflink/clonefile when available, otherwise ``shutil.copy2``.
  - ``copy``: always ``shutil.copy2`` (baseline behavior).
  - ``reflink``: attempt CoW clone per file; fall back to ``copy2`` on failure.

Operational counters (cloned / copied / fallback files and bytes) are emitted
via the module logger after each seed pass — not dollar savings.
"""

import errno
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from typing import Iterable, Optional

from harness.implement_guards import extract_goal_paths, resolve_repo_file
from harness.paths import path_within

logger = logging.getLogger("pmharness.worktree_seed")

# Cap dynamic copies so a vague goal cannot flood the worktree.
_MAX_DYNAMIC_SEED = 250

_COPY_STRATEGY_ENV = "HARNESS_WORKTREE_COPY_STRATEGY"
_VALID_COPY_STRATEGIES = frozenset({"auto", "copy", "reflink"})
_DEFAULT_COPY_STRATEGY = "auto"

# Linux fs.h FICLONE — architecture-specific ioctl numbers.
_FICLONE_BY_MACHINE = {
    "x86_64": 0x40049409,
    "aarch64": 0x40049409,
    "arm64": 0x40049409,
    "armv7l": 0x40049409,
    "i386": 0x40049409,
    "i686": 0x40049409,
    "ppc64le": 0x40049409,
    "s390x": 0x40049409,
}

_reflink_probe_lock = threading.Lock()
_reflink_supported: Optional[bool] = None


@dataclass
class SeedCopyStats:
    """Operational copy counters for a seed pass (not cost/savings USD)."""

    cloned_files: int = 0
    cloned_bytes: int = 0
    copied_files: int = 0
    copied_bytes: int = 0
    fallback_files: int = 0
    fallback_bytes: int = 0

    def as_log_dict(self) -> dict[str, int]:
        return {
            "cloned_files": self.cloned_files,
            "cloned_bytes": self.cloned_bytes,
            "copied_files": self.copied_files,
            "copied_bytes": self.copied_bytes,
            "fallback_files": self.fallback_files,
            "fallback_bytes": self.fallback_bytes,
        }


@dataclass
class SeedResult:
    """Paths seeded into the worktree plus operational copy counters."""

    paths: list[str] = field(default_factory=list)
    copy_stats: SeedCopyStats = field(default_factory=SeedCopyStats)


_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "to", "of", "in", "on", "at", "by", "for",
    "from", "with", "into", "onto", "over", "under", "this", "that", "these",
    "those", "it", "its", "is", "are", "be", "been", "was", "were", "do", "does",
    "did", "can", "could", "should", "would", "will", "just", "please", "need",
    "needs", "make", "made", "add", "adds", "added", "fix", "fixes", "fixed",
    "update", "updates", "updated", "change", "changes", "changed", "edit",
    "edits", "edited", "rewrite", "rewrites", "implement", "implements",
    "create", "creates", "created", "write", "writes", "written", "read",
    "file", "files", "code", "worker", "workers", "swarm", "job", "jobs",
    "run", "runs", "running", "use", "using", "via", "also", "then", "than",
    "when", "where", "what", "which", "who", "how", "why", "all", "any",
    "some", "each", "every", "both", "more", "most", "other", "only", "own",
    "same", "such", "too", "very", "not", "no", "nor", "so", "if", "as",
    "but", "about", "after", "before", "between", "during", "without",
    "through", "again", "further", "once", "here", "there", "out", "up",
    "down", "off", "new", "old", "good", "bad", "bug", "bugs", "issue",
    "issues", "task", "tasks", "goal", "goals", "help", "me", "my", "our",
    "your", "you", "we", "they", "them", "their", "his", "her", "repo",
    "project", "workspace", "directory", "folder", "path", "paths",
})

_WORD_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{1,}")


def resolve_copy_strategy(raw: Optional[str] = None) -> str:
    """Normalize ``HARNESS_WORKTREE_COPY_STRATEGY`` (default ``auto``)."""
    value = (raw if raw is not None else os.environ.get(_COPY_STRATEGY_ENV, "")).strip().lower()
    if not value:
        return _DEFAULT_COPY_STRATEGY
    if value not in _VALID_COPY_STRATEGIES:
        return _DEFAULT_COPY_STRATEGY
    return value


def reset_copy_capability_cache() -> None:
    """Clear the cached reflink probe (for hermetic tests)."""
    global _reflink_supported
    with _reflink_probe_lock:
        _reflink_supported = None


def seed_worktree_from_goal(
    repo: str,
    wt_path: str,
    goal: str,
    *,
    copy_strategy: Optional[str] = None,
) -> SeedResult:
    """Copy goal-referenced live files into ``wt_path`` when missing or different.

    Returns seeded paths (posix-style) and operational copy counters.
    Best-effort: never raises for individual copy failures.
    """
    result = SeedResult()
    if not repo or not wt_path or not goal:
        return result
    strategy = resolve_copy_strategy(copy_strategy)
    seeded: list[str] = []
    for token in extract_goal_paths(goal):
        src = resolve_repo_file(repo, token)
        if not src:
            # Directory mention: seed untracked/dirty files under it.
            dir_src = _resolve_repo_dir(repo, token)
            if dir_src:
                for rel in _iter_files_under(repo, dir_src):
                    if _copy_into_worktree(repo, wt_path, rel, strategy, result.copy_stats):
                        seeded.append(rel)
            continue
        try:
            rel = os.path.relpath(src, os.path.abspath(repo)).replace("\\", "/")
        except Exception:
            continue
        if _copy_into_worktree(repo, wt_path, rel, strategy, result.copy_stats):
            seeded.append(rel)

    # Dynamic pass: dirty/untracked on-disk files matched by goal tokens.
    for rel in _matching_live_paths(repo, goal):
        if _copy_into_worktree(repo, wt_path, rel, strategy, result.copy_stats):
            seeded.append(rel)

    # Dedup while preserving order.
    seen: set[str] = set()
    for r in seeded:
        if r in seen:
            continue
        seen.add(r)
        result.paths.append(r)
    _log_seed_copy_stats(result.copy_stats, context="goal")
    return result


def commit_seed_baseline(wt_path: str, seeded: Iterable[str]) -> int:
    """Commit seeded paths so ``finalize_worktree_patch`` only sees worker edits.

    Without this, ``git add -A`` at finalize treats seeded untracked/dirty
    copies as worker changes — analysis-mode jobs then falsely report
    "applied N files" for pre-existing diagnostics the seeder copied in.

    Returns how many paths were committed. Best-effort; never raises.
    """
    paths = []
    seen: set[str] = set()
    for rel in seeded or []:
        r = str(rel or "").replace("\\", "/").strip()
        if not r or r in seen:
            continue
        seen.add(r)
        paths.append(r)
    if not wt_path or not paths:
        return 0
    try:
        for rel in paths:
            subprocess.run(
                ["git", "-C", wt_path, "add", "--", rel],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=30,
            )
        staged = subprocess.run(
            ["git", "-C", wt_path, "diff", "--cached", "--name-only"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=15,
        )
        staged_paths = [
            ln.strip() for ln in (staged.stdout or "").splitlines() if ln.strip()
        ]
        if not staged_paths:
            return 0
        env = os.environ.copy()
        env["GIT_AUTHOR_NAME"] = "marionette-seed"
        env["GIT_AUTHOR_EMAIL"] = "seed@marionette.local"
        env["GIT_COMMITTER_NAME"] = "marionette-seed"
        env["GIT_COMMITTER_EMAIL"] = "seed@marionette.local"
        # Avoid interactive hooks / identity prompts in worker worktrees.
        env.setdefault("GIT_TERMINAL_PROMPT", "0")
        commit = subprocess.run(
            [
                "git", "-C", wt_path, "commit",
                "-m", "marionette: seed baseline (not a worker edit)",
                "--no-verify",
            ],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=30, env=env,
        )
        if commit.returncode != 0:
            return 0
        return len(staged_paths)
    except Exception:
        return 0


def seed_untracked_matching(
    repo: str,
    wt_path: str,
    prefixes: Iterable[str],
    *,
    copy_strategy: Optional[str] = None,
) -> SeedResult:
    """Copy untracked live files under any of ``prefixes`` into the worktree."""
    result = SeedResult()
    strategy = resolve_copy_strategy(copy_strategy)
    for prefix in prefixes or []:
        dir_src = _resolve_repo_dir(repo, prefix) or resolve_repo_file(repo, prefix)
        if dir_src and os.path.isdir(dir_src):
            for rel in _iter_files_under(repo, dir_src):
                dst = os.path.join(wt_path, rel.replace("/", os.sep))
                if os.path.exists(dst):
                    continue
                if _copy_into_worktree(repo, wt_path, rel, strategy, result.copy_stats):
                    result.paths.append(rel)
    _log_seed_copy_stats(result.copy_stats, context="prefix")
    return result


def goal_match_tokens(goal: str) -> set[str]:
    """Significant tokens used to match live dirty/untracked paths to a goal."""
    tokens: set[str] = set()
    for path_tok in extract_goal_paths(goal):
        norm = path_tok.replace("\\", "/").strip("/").lower()
        if not norm:
            continue
        tokens.add(norm)
        for part in norm.split("/"):
            if not part or part in (".", ".."):
                continue
            tokens.add(part)
            if "." in part:
                tokens.add(part.rsplit(".", 1)[0])
    for m in _WORD_RE.finditer(goal or ""):
        w = m.group(0).lower()
        if len(w) < 3 or w in _STOPWORDS:
            continue
        tokens.add(w)
        if "." in w:
            stem = w.rsplit(".", 1)[0]
            if len(stem) >= 3 and stem not in _STOPWORDS:
                tokens.add(stem)
    return tokens


def _log_seed_copy_stats(stats: SeedCopyStats, *, context: str) -> None:
    total = (
        stats.cloned_files + stats.copied_files + stats.fallback_files
    )
    if total <= 0:
        return
    payload = stats.as_log_dict()
    payload["context"] = context
    try:
        logger.info("worktree seed copy stats %s", payload)
    except Exception:
        pass


def _matching_live_paths(repo: str, goal: str) -> list[str]:
    tokens = goal_match_tokens(goal)
    if not tokens:
        return []
    out: list[str] = []
    for rel in _list_live_dirty_paths(repo):
        if not _path_matches_tokens(rel, tokens):
            continue
        out.append(rel)
        if len(out) >= _MAX_DYNAMIC_SEED:
            break
    return out


def _path_matches_tokens(rel: str, tokens: set[str]) -> bool:
    rel_l = rel.replace("\\", "/").lower()
    parts = [p for p in rel_l.split("/") if p]
    if not parts:
        return False
    base = parts[-1]
    stem = base.rsplit(".", 1)[0] if "." in base else base
    candidates = set(parts)
    candidates.add(base)
    candidates.add(stem)
    candidates.add(rel_l)
    return bool(candidates & tokens)


def _parse_porcelain_path_field(path_field: str) -> list[str]:
    """Normalize one porcelain path field into relative repo paths."""
    path_field = path_field.strip().strip('"').replace("\\", "/")
    if not path_field or path_field.endswith("/"):
        return []
    if " -> " in path_field:
        old_path, new_path = path_field.split(" -> ", 1)
        out: list[str] = []
        for part in (old_path.strip(), new_path.strip()):
            if part and not part.endswith("/"):
                out.append(part)
        return out
    return [path_field]


def _list_git_status_porcelain_paths(repo: str) -> list[str]:
    """Every path from git status --porcelain, including deletes and renames."""
    if not repo or not os.path.isdir(repo):
        return []
    try:
        p = subprocess.run(
            [
                "git", "-C", repo, "status", "--porcelain", "-uall",
                "--ignore-submodules=all",
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30,
        )
    except Exception:
        return []
    if p.returncode != 0:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for line in p.stdout.splitlines():
        if len(line) < 4:
            continue
        for path_part in _parse_porcelain_path_field(line[3:]):
            if path_part in seen:
                continue
            seen.add(path_part)
            out.append(path_part)
    return sorted(out)


def _list_live_dirty_paths(repo: str) -> list[str]:
    """Relative dirty/untracked paths that exist as files (worktree seeding)."""
    if not repo or not os.path.isdir(repo):
        return []
    repo_abs = os.path.abspath(repo)
    out: list[str] = []
    for path_part in _list_git_status_porcelain_paths(repo):
        src = os.path.join(repo_abs, path_part.replace("/", os.sep))
        if os.path.isfile(src):
            out.append(path_part)
    return out


def _resolve_repo_dir(repo: str, rel_or_abs: str) -> Optional[str]:
    if not repo or not rel_or_abs:
        return None
    repo_abs = os.path.abspath(repo)
    if os.path.isabs(rel_or_abs):
        path = os.path.abspath(rel_or_abs)
    else:
        path = os.path.abspath(os.path.join(repo_abs, rel_or_abs.replace("\\", os.sep)))
    try:
        common = os.path.commonpath([repo_abs, path])
        if os.path.normcase(common) != os.path.normcase(repo_abs):
            return None
    except ValueError:
        return None
    if os.path.isdir(path):
        return path
    return None


def _iter_files_under(repo: str, abs_dir: str) -> list[str]:
    repo_abs = os.path.abspath(repo)
    out: list[str] = []
    skip = {".git", "node_modules", ".venv", "__pycache__", ".codegraph", "dist", "build"}
    try:
        for root, dirs, files in os.walk(abs_dir):
            dirs[:] = [d for d in dirs if d not in skip and not d.startswith(".")]
            for name in files:
                if name.startswith("."):
                    continue
                full = os.path.join(root, name)
                try:
                    rel = os.path.relpath(full, repo_abs).replace("\\", "/")
                except Exception:
                    continue
                out.append(rel)
    except Exception:
        return []
    return out


def _import_fcntl():
    """Lazy fcntl import — unavailable on Windows."""
    try:
        import fcntl as _fcntl
    except ImportError:
        return None
    return _fcntl


def _staging_path_for(dst: str) -> str:
    """Reserve a unique sibling path that does not exist yet.

    ``mkstemp`` atomically reserves the name; we close and unlink so Linux
    ``O_EXCL`` / macOS ``clonefile`` can create the staging inode fresh.
    """
    parent = os.path.dirname(dst) or "."
    fd, path = tempfile.mkstemp(prefix=".pmseed-", dir=parent)
    os.close(fd)
    try:
        os.unlink(path)
    except OSError:
        _discard_staging(path)
        raise
    return path


def _finalize_staged_copy(staging: str, dst: str) -> None:
    os.replace(staging, dst)


def _discard_staging(staging: str) -> None:
    try:
        if os.path.lexists(staging):
            os.remove(staging)
    except OSError:
        pass


def _ficlone_ioctl() -> Optional[int]:
    if not sys.platform.startswith("linux"):
        return None
    machine = getattr(os, "uname", lambda: None)()
    if machine is None:
        return None
    return _FICLONE_BY_MACHINE.get(getattr(machine, "machine", ""))


def _probe_linux_ficlone() -> bool:
    """Best-effort probe: try FICLONE on a temp file pair."""
    fcntl = _import_fcntl()
    if fcntl is None:
        return False
    if not sys.platform.startswith("linux"):
        return False
    request = _ficlone_ioctl()
    if request is None:
        return False
    tmpdir = tempfile.mkdtemp(prefix="pmharness-reflink-probe-")
    src = os.path.join(tmpdir, "src.bin")
    dst = os.path.join(tmpdir, "dst.bin")
    try:
        with open(src, "wb") as fh:
            fh.write(b"probe")
        src_fd = os.open(src, os.O_RDONLY)
        try:
            dst_fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            try:
                fcntl.ioctl(dst_fd, request, src_fd)
                return True
            except OSError:
                return False
            finally:
                os.close(dst_fd)
        finally:
            os.close(src_fd)
    except Exception:
        return False
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _probe_macos_clonefile() -> bool:
    try:
        import ctypes
        import ctypes.util
    except Exception:
        return False
    try:
        libc_path = ctypes.util.find_library("c")
        libc = ctypes.CDLL(libc_path, use_errno=True) if libc_path else ctypes.CDLL(None, use_errno=True)
        if not hasattr(libc, "clonefile"):
            return False
    except Exception:
        return False
    tmpdir = tempfile.mkdtemp(prefix="pmharness-clonefile-probe-")
    src = os.path.join(tmpdir, "src.bin")
    dst = os.path.join(tmpdir, "dst.bin")
    try:
        with open(src, "wb") as fh:
            fh.write(b"probe")
        ret = libc.clonefile(src.encode("utf-8"), dst.encode("utf-8"), 0)
        return ret == 0
    except Exception:
        return False
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def reflink_copy_supported(force_refresh: bool = False) -> bool:
    """Return whether this process can attempt CoW clone copies."""
    global _reflink_supported
    with _reflink_probe_lock:
        if not force_refresh and _reflink_supported is not None:
            return _reflink_supported
        supported = False
        if sys.platform.startswith("linux"):
            supported = _probe_linux_ficlone()
        elif sys.platform == "darwin":
            supported = _probe_macos_clonefile()
        _reflink_supported = supported
        return supported


def _cleanup_failed_clone_dst(dst: str) -> None:
    """Remove or truncate a partial clone destination before fallback copy."""
    try:
        if os.path.lexists(dst):
            os.remove(dst)
    except Exception:
        try:
            with open(dst, "wb") as fh:
                fh.truncate(0)
            os.remove(dst)
        except Exception:
            pass


def _linux_ficlone_copy(src: str, dst: str) -> None:
    fcntl = _import_fcntl()
    if fcntl is None:
        raise OSError(errno.EOPNOTSUPP, "fcntl unavailable on this platform")
    request = _ficlone_ioctl()
    if request is None:
        raise OSError(errno.EOPNOTSUPP, "FICLONE unavailable on this machine")
    src_mode = os.stat(src).st_mode
    src_fd = os.open(src, os.O_RDONLY)
    try:
        dst_fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, src_mode & 0o777)
        try:
            fcntl.ioctl(dst_fd, request, src_fd)
        except Exception:
            _cleanup_failed_clone_dst(dst)
            raise
        finally:
            os.close(dst_fd)
    finally:
        os.close(src_fd)
    shutil.copystat(src, dst, follow_symlinks=True)


def _macos_clonefile_copy(src: str, dst: str) -> None:
    import ctypes
    import ctypes.util

    libc_path = ctypes.util.find_library("c")
    libc = ctypes.CDLL(libc_path, use_errno=True) if libc_path else ctypes.CDLL(None, use_errno=True)
    if not hasattr(libc, "clonefile"):
        raise OSError(errno.EOPNOTSUPP, "clonefile unavailable")
    ret = libc.clonefile(src.encode("utf-8"), dst.encode("utf-8"), 0)
    if ret != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), dst)


def _try_reflink_copy(src: str, dst: str) -> None:
    if sys.platform.startswith("linux"):
        _linux_ficlone_copy(src, dst)
        return
    if sys.platform == "darwin":
        _macos_clonefile_copy(src, dst)
        return
    raise OSError(errno.EOPNOTSUPP, "reflink unsupported on this platform")


def _record_copy_stat(stats: SeedCopyStats, category: str, nbytes: int) -> None:
    if category == "cloned":
        stats.cloned_files += 1
        stats.cloned_bytes += nbytes
    elif category == "copied":
        stats.copied_files += 1
        stats.copied_bytes += nbytes
    elif category == "fallback":
        stats.fallback_files += 1
        stats.fallback_bytes += nbytes


def _copy_file_with_strategy(
    src: str,
    dst: str,
    strategy: str,
    stats: SeedCopyStats,
) -> None:
    """Copy ``src`` to ``dst`` via a sibling staging file and atomic replace."""
    nbytes = os.path.getsize(src)
    staging = _staging_path_for(dst)
    try:
        if strategy == "copy":
            shutil.copy2(src, staging)
            _record_copy_stat(stats, "copied", nbytes)
        else:
            use_reflink = strategy == "reflink" or (
                strategy == "auto" and reflink_copy_supported()
            )
            if not use_reflink:
                shutil.copy2(src, staging)
                _record_copy_stat(stats, "copied", nbytes)
            else:
                try:
                    _try_reflink_copy(src, staging)
                    _record_copy_stat(stats, "cloned", nbytes)
                except OSError:
                    _cleanup_failed_clone_dst(staging)
                    shutil.copy2(src, staging)
                    _record_copy_stat(stats, "fallback", nbytes)
        _finalize_staged_copy(staging, dst)
    except Exception:
        _cleanup_failed_clone_dst(staging)
        raise


def _copy_into_worktree(
    repo: str,
    wt_path: str,
    rel: str,
    strategy: str,
    stats: SeedCopyStats,
) -> bool:
    repo_abs = os.path.abspath(repo)
    wt_abs = os.path.abspath(wt_path)
    src = os.path.join(repo_abs, rel.replace("/", os.sep))
    dst = os.path.join(wt_abs, rel.replace("/", os.sep))
    # Regular files only — do not follow/copy symlinks as file bodies.
    if not os.path.isfile(src):
        return False
    if not path_within(src, repo_abs, allow_equal=True):
        return False
    if not path_within(dst, wt_abs, allow_equal=True):
        return False
    try:
        if os.path.isfile(dst):
            # Skip identical content (compare bytes when sizes match).
            try:
                if os.path.getsize(src) == os.path.getsize(dst):
                    with open(src, "rb") as a, open(dst, "rb") as b:
                        if a.read() == b.read():
                            return False
            except Exception:
                pass
        os.makedirs(os.path.dirname(dst) or wt_abs, exist_ok=True)
        _copy_file_with_strategy(src, dst, strategy, stats)
        return True
    except Exception:
        # Staging failures are cleaned in _copy_file_with_strategy; never
        # touch an existing destination until os.replace succeeds.
        return False
