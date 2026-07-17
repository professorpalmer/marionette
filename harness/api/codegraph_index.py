"""CodeGraph indexer runtime (peeled from ``harness.server``).

Owns background index/reindex, status resolution, staleness refresh, and the
single-indexer Popen guard. ``server.py`` re-exports historical ``_`` names
for tests and callers; inject Puppetmaster/diag/cfg via :func:`bind_deps`.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional


# ---------------------------------------------------------------------------
# In-process state (re-exported from server for tests / services wiring)
# ---------------------------------------------------------------------------

codegraph_status = "none"
codegraph_status_reason = None
codegraph_preflight = None  # dict | None
codegraph_suggested_action = None  # dict | None

# Short-TTL cache for the /api/codegraph status payload, keyed by repo path.
codegraph_status_cache: dict = {}  # repo -> (monotonic_expiry, payload_dict)
CODEGRAPH_STATUS_TTL = 30.0  # seconds
# After an indexer failure, suppress GET auto-reindex for this many seconds.
codegraph_fail_until: dict = {}  # repo -> monotonic timestamp

# Handle to the in-flight CodeGraph indexer: (repo_path, Popen) | None.
codegraph_index_proc = None  # tuple[str, Any] | None
codegraph_index_lock = threading.Lock()

# Debounce: never re-check staleness more than once per this interval per repo.
codegraph_stale_check_at: dict = {}  # repo -> monotonic timestamp of last check
CODEGRAPH_STALE_DEBOUNCE = 20.0  # seconds

startup_index_fired = False


@dataclass
class CodegraphIndexDeps:
    """Server-side deps the indexer must not import from ``harness.server``."""

    puppetmaster_available: Callable[[], bool]
    puppetmaster_cmd: Callable[..., list]
    diag: Callable[..., Any]
    get_state_dir: Callable[[], str]
    get_repo: Callable[[], Optional[str]]


_deps: Optional[CodegraphIndexDeps] = None


def bind_deps(deps: CodegraphIndexDeps) -> None:
    """Wire server callables once during server module import."""
    global _deps
    _deps = deps


def _require_deps() -> CodegraphIndexDeps:
    if _deps is None:
        raise RuntimeError("codegraph_index.bind_deps() was not called")
    return _deps


def codegraph_indexed(repo_path: str) -> bool:
    """True only when a built CodeGraph DB exists for the repo.

    The `.codegraph/` directory alone is NOT proof of an index: `codegraph init`
    writes `config.json` there before any indexing, and `codegraph status` hangs
    on a config-only checkout (no DB) until it times out -- which surfaced as
    "unsupported" on fresh installs. Gate on the actual DB file so an init'd-but-
    unindexed checkout is treated as needing an index, not as ready.
    """
    try:
        return os.path.isfile(os.path.join(repo_path, ".codegraph", "codegraph.db"))
    except Exception:
        return False


def codegraph_index_alive() -> bool:
    """True only while the tracked indexer subprocess is actually running."""
    p = codegraph_index_proc
    if p is None:
        return False
    try:
        return p[1].poll() is None
    except Exception:
        return False


def codegraph_index_log_path() -> str:
    state = _require_deps().get_state_dir() or os.path.expanduser("~/.pmharness/state")
    try:
        os.makedirs(state, exist_ok=True)
    except Exception:
        pass
    return os.path.join(state, "codegraph-index.log")


def codegraph_api_payload(repo, status=None):
    """Shared /api/codegraph fields (reason, preflight, suggested_action)."""
    st = status if status is not None else (get_codegraph_status(repo) if repo else "none")
    return {
        "indexed": bool(repo and codegraph_indexed(repo)),
        "status": st,
        "reason": codegraph_status_reason,
        "preflight": codegraph_preflight,
        "suggested_action": codegraph_suggested_action,
        "repo": repo or "",
    }


def codegraph_tail_log(max_chars: int = 800) -> str:
    path = codegraph_index_log_path()
    try:
        if not os.path.isfile(path):
            return ""
        with open(path, encoding="utf-8", errors="replace") as f:
            data = f.read()
        if len(data) <= max_chars:
            return data.strip()
        return data[-max_chars:].strip()
    except Exception:
        return ""


def prepare_codegraph_scope(repo_path: str) -> dict:
    """Run preflight; auto-apply asset excludes when scope is recommended.

    Returns the preflight dict and updates globals for API/UI. Does not start
    the indexer. Verdict ``unlikely`` means callers should NOT start a full
    index; ``scope_recommended`` / ``ok`` may proceed (after excludes merge).
    """
    global codegraph_status, codegraph_status_reason
    global codegraph_preflight, codegraph_suggested_action
    from ..codegraph_preflight import (
        child_exclude_globs,
        ensure_lua_includes,
        merge_codegraph_excludes,
        preflight_workspace,
    )

    deps = _require_deps()
    pre = preflight_workspace(repo_path)
    codegraph_preflight = pre
    codegraph_suggested_action = None
    verdict = pre.get("verdict") or "ok"

    try:
        ensure_lua_includes(repo_path)
    except Exception as e:
        deps.diag("server.codegraph_lua_include", e)

    if verdict == "unlikely":
        codegraph_status = "needs_scope"
        codegraph_status_reason = pre.get("reason") or (
            "Workspace has almost no indexable source under a huge tree."
        )
        roots = pre.get("suggested_roots") or []
        excludes = pre.get("suggested_excludes") or []
        if roots:
            codegraph_suggested_action = {
                "kind": "open_subdir",
                "path": os.path.join(repo_path, roots[0]),
                "excludes": excludes,
            }
        elif excludes:
            codegraph_suggested_action = {
                "kind": "write_excludes",
                "excludes": excludes,
            }
        return pre

    if verdict == "scope_recommended":
        extra = child_exclude_globs(pre.get("suggested_excludes") or [])
        try:
            merge_codegraph_excludes(repo_path, extra_excludes=extra or None)
        except Exception as e:
            deps.diag("server.codegraph_merge_excludes", e)
        codegraph_status_reason = pre.get("reason") or (
            "Large install detected; asset excludes applied before indexing."
        )
        roots = pre.get("suggested_roots") or []
        if roots:
            codegraph_suggested_action = {
                "kind": "open_subdir",
                "path": os.path.join(repo_path, roots[0]),
                "excludes": pre.get("suggested_excludes") or [],
            }
        else:
            codegraph_suggested_action = {
                "kind": "write_excludes",
                "excludes": pre.get("suggested_excludes") or [],
            }
        # Still index after excludes — do not leave the user on needs_scope
        # when we can recover automatically.
        return pre

    # ok
    try:
        # Ensure lua is graphable even on normal repos that already have config.
        ensure_lua_includes(repo_path)
    except Exception:
        pass
    return pre


def index_codegraph_bg(repo_path: str):
    global codegraph_status, codegraph_status_reason, codegraph_status_cache
    global codegraph_preflight, codegraph_suggested_action
    global codegraph_index_proc
    deps = _require_deps()
    if not deps.puppetmaster_available():
        codegraph_status = "unsupported"
        codegraph_status_reason = "puppetmaster not found -- codegraph/swarm unavailable"
        return

    # Missing/moved workspace: fail once with a clear reason. Spawning with a
    # bad cwd on Windows surfaces as WinError 2/267 and the GET self-heal loop
    # used to re-kick forever, stuffing codegraph-index.log into the panel.
    if not repo_path or not os.path.isdir(repo_path):
        codegraph_status = "unsupported"
        codegraph_status_reason = (
            f"Workspace path is missing or not a directory: {repo_path or '(empty)'}. "
            "Open Folder on the real path (e.g. C:\\Ashita or C:\\Ashita\\addons) "
            "and Remove the phantom from Projects."
        )
        codegraph_suggested_action = None
        if repo_path:
            codegraph_status_cache.pop(repo_path, None)
        return

    # Claim INDEXING immediately -- before preflight -- so /api/workspace/open
    # and status polls never flash UNSUPPORTED while scope prep runs. Preflight
    # may still override to needs_scope / unsupported on real failures.
    codegraph_status = "indexing"
    codegraph_status_reason = None
    codegraph_status_cache.pop(repo_path, None)

    # Preflight before spawning: avoid a doomed 10-minute walk of game assets.
    try:
        pre = prepare_codegraph_scope(repo_path)
    except Exception as e:
        deps.diag("server.codegraph_preflight", e)
        pre = {"verdict": "ok"}
    if (pre.get("verdict") or "") == "unlikely":
        # Do not start indexer; status already needs_scope with reason.
        codegraph_status_cache.pop(repo_path, None)
        return

    # Guard against a second indexer while one is already running -- concurrent
    # codegraph indexers collide on the same SQLite (lock-busy) and wedge the panel.
    with codegraph_index_lock:
        if codegraph_index_alive():
            codegraph_status = "indexing"
            return
        codegraph_status = "indexing"
        if not codegraph_status_reason:
            codegraph_status_reason = None
        # Invalidate any cached status for this repo so the panel does not show
        # stale "ready" stats while a fresh (re)index is running.
        codegraph_status_cache.pop(repo_path, None)
        codegraph_fail_until.pop(repo_path, None)
        log_path = codegraph_index_log_path()
        try:
            import subprocess
            log_f = open(log_path, "a", encoding="utf-8", errors="replace")
            log_f.write(f"\n--- codegraph init --index @ {repo_path} ---\n")
            log_f.flush()
            proc = subprocess.Popen(
                deps.puppetmaster_cmd("codegraph", "init", "--index"),
                cwd=repo_path,
                stdout=log_f,
                stderr=subprocess.STDOUT,
            )
            codegraph_index_proc = (repo_path, proc)
        except Exception as e:
            codegraph_status = "unsupported"
            codegraph_status_reason = f"failed to start indexer: {e}"
            return

    # After scope/excludes, allow a longer run; still a backstop so a wedged
    # process cannot pin the panel forever.
    index_timeout = 1800

    def wait_and_update():
        global codegraph_status, codegraph_status_reason, codegraph_index_proc
        global codegraph_suggested_action
        timed_out = False
        try:
            proc.wait(timeout=index_timeout)
            if proc.returncode == 0 and codegraph_indexed(repo_path):
                codegraph_status = "ready"
                codegraph_status_reason = None
            elif proc.returncode == 0:
                codegraph_status = "unsupported"
                codegraph_status_reason = (
                    "Indexer exited 0 but no codegraph.db was written. "
                    + (codegraph_tail_log(max_chars=400) or "See codegraph-index.log.")
                )
            else:
                codegraph_status = "unsupported"
                # One clean failure line — do not dump the whole repeated log.
                tail = codegraph_tail_log(max_chars=400)
                # Prefer the last non-empty line of the tail.
                last_line = ""
                if tail:
                    for line in reversed(tail.splitlines()):
                        if line.strip():
                            last_line = line.strip()
                            break
                codegraph_status_reason = (
                    f"Indexer failed (exit {proc.returncode}). "
                    + (last_line or "See ~/.pmharness/state/codegraph-index.log.")
                )
                # Back off auto-reindex for this path so GET polling cannot
                # restart a doomed indexer every few seconds.
                codegraph_fail_until[repo_path] = time.monotonic() + 120.0
        except Exception:
            timed_out = True
            codegraph_status = "unsupported"
            codegraph_status_reason = (
                f"Indexing timed out after {index_timeout // 60} minutes. "
                "The tree is likely still too large — open a code subdirectory "
                "or apply asset excludes, then re-index."
            )
            codegraph_fail_until[repo_path] = time.monotonic() + 120.0
            try:
                proc.kill()
            except Exception:
                pass
        finally:
            try:
                if proc.stdout:
                    proc.stdout.close()
            except Exception:
                pass
            with codegraph_index_lock:
                if codegraph_index_proc and codegraph_index_proc[1] is proc:
                    codegraph_index_proc = None
            codegraph_status_cache.pop(repo_path, None)
            if timed_out:
                codegraph_suggested_action = {
                    "kind": "write_excludes",
                    "excludes": (pre.get("suggested_excludes") if isinstance(pre, dict) else None) or [],
                }

    threading.Thread(target=wait_and_update, daemon=True).start()


def reindex_codegraph_bg(repo_path: str):
    global codegraph_status, codegraph_status_reason
    deps = _require_deps()
    if not deps.puppetmaster_available():
        codegraph_status = "unsupported"
        codegraph_status_reason = "puppetmaster not found -- codegraph/swarm unavailable"
        return
    # Force a fresh preflight + index (same path as init).
    index_codegraph_bg(repo_path)


def get_codegraph_status(repo_path: str) -> str:
    """Resolve CodeGraph badge status for ``repo_path``.

    ``unsupported`` is reserved for confirmed failures (PM missing, path
    missing, indexer failed with a reason). The transient empty-index case
    (PM available, path exists, no DB yet, no failure reason) returns
    ``indexing`` so the LeftRail never flashes UNSUPPORTED before the
    indexer starts.
    """
    global codegraph_status, codegraph_status_reason
    deps = _require_deps()
    if not repo_path:
        return "none"
    if not deps.puppetmaster_available():
        codegraph_status = "unsupported"
        return "unsupported"
    if not os.path.isdir(repo_path):
        codegraph_status = "unsupported"
        return "unsupported"
    # Self-heal: trust "indexing" while the indexer is alive OR while we are
    # still in preflight/spawn (status=indexing but proc not assigned yet).
    # Demoting that window to unsupported caused the LeftRail UNSUPPORTED flash.
    if codegraph_status == "indexing":
        if codegraph_index_alive():
            return "indexing"
        if codegraph_indexed(repo_path):
            codegraph_status = "ready"
            return "ready"
        # Proc handle present but dead => indexer exited without a DB.
        if codegraph_index_proc is not None:
            codegraph_status = "unsupported"
            if not codegraph_status_reason:
                codegraph_status_reason = "Indexer stopped before writing codegraph.db"
            return "unsupported"
        # No proc yet (preflight / about to spawn) -- stay indexing.
        return "indexing"
    if codegraph_status == "needs_scope":
        return "needs_scope"
    if codegraph_indexed(repo_path):
        codegraph_status = "ready"
        return "ready"
    # Confirmed failure with a reason sticks; bare default / empty-index does not.
    if codegraph_status == "unsupported" and codegraph_status_reason:
        return "unsupported"
    # Transient: PM ok, path ok, not indexed yet — never flash unsupported.
    if codegraph_status not in ("indexing", "pending"):
        codegraph_status = "indexing"
    return "indexing"


def codegraph_is_stale(repo_path: str) -> bool:
    """True if the working tree has changed since the .codegraph index was built.

    Detects edits AND deletions: we compare the index mtime against the newest
    mtime of (a) every source FILE and (b) every DIRECTORY. Directory mtimes are
    the key to catching deletions/renames -- removing a file bumps its parent
    dir's mtime even though no surviving file looks newer (the original bug:
    deleted files left the index referencing ghosts while this returned False).
    """
    try:
        codegraph_path = os.path.join(repo_path, ".codegraph")
        if not os.path.exists(codegraph_path):
            return False
        cg_mtime = os.path.getmtime(codegraph_path)
        skip_dirs = {".git", "node_modules", ".venv", ".codegraph", "dist", "build", "__pycache__"}
        extensions = {".py", ".ts", ".tsx", ".js", ".jsx", ".java", ".swift", ".go", ".rs"}
        for root, dirs, files in os.walk(repo_path):
            dirs[:] = [d for d in dirs if d not in skip_dirs]
            # (b) directory mtime -- catches deletions/renames/additions in this dir
            try:
                if os.path.getmtime(root) > cg_mtime:
                    return True
            except Exception:
                pass
            # (a) source file mtime -- catches in-place edits
            for file in files:
                _, ext = os.path.splitext(file)
                if ext.lower() in extensions:
                    try:
                        if os.path.getmtime(os.path.join(root, file)) > cg_mtime:
                            return True
                    except Exception:
                        pass
    except Exception:
        pass
    return False


def maybe_refresh_codegraph(repo_path: str, *, force: bool = False) -> None:
    """Debounced, background staleness-driven reindex. Safe to call on every turn
    and on session switch -- the debounce + the indexing-guard ensure it never
    thrashes. force=True bypasses the debounce (e.g. an explicit user action)."""
    if not repo_path:
        return
    import time as _time
    if not force:
        last = codegraph_stale_check_at.get(repo_path, 0.0)
        if (_time.monotonic() - last) < CODEGRAPH_STALE_DEBOUNCE:
            return
    codegraph_stale_check_at[repo_path] = _time.monotonic()

    def worker():
        global codegraph_status, codegraph_status_reason
        if codegraph_status == "indexing":
            return
        if codegraph_is_stale(repo_path):
            codegraph_status_reason = "files changed -- refreshing index"
            reindex_codegraph_bg(repo_path)
    try:
        threading.Thread(target=worker, daemon=True).start()
    except Exception as e:
        _require_deps().diag("server.codegraph_stale_check_thread", e)


def maybe_auto_index_codegraph():
    global startup_index_fired, codegraph_status, codegraph_status_reason
    if startup_index_fired:
        return
    startup_index_fired = True

    deps = _require_deps()
    repo = deps.get_repo()
    if repo and os.path.isdir(repo):
        if not deps.puppetmaster_available():
            codegraph_status = "unsupported"
            codegraph_status_reason = "puppetmaster not found -- codegraph/swarm unavailable"
            return

        if codegraph_indexed(repo):
            codegraph_status = "ready"
        else:
            # No built DB yet (fresh checkout, or init'd-but-never-indexed):
            # build it in the background so the panel comes up ready without a
            # manual re-index. `index_codegraph_bg` runs `codegraph init --index`.
            def target():
                index_codegraph_bg(repo)
            t = threading.Thread(target=target, daemon=True)
            t.start()


_REASON_UNSET = object()


def set_codegraph_status(status: str, reason: Any = _REASON_UNSET) -> None:
    """Mutate status globals (injected into SessionServices / WorkspaceServices).

    Passing only ``status`` leaves ``codegraph_status_reason`` untouched.
    Passing an explicit ``reason`` (including ``None``) updates both.
    """
    global codegraph_status, codegraph_status_reason
    codegraph_status = status
    if reason is not _REASON_UNSET:
        codegraph_status_reason = reason


def clear_active_codegraph() -> None:
    global codegraph_status, codegraph_status_reason
    codegraph_status = "none"
    codegraph_status_reason = None
