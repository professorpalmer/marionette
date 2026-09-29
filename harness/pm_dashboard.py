"""Reuse Puppetmaster's local dashboard runtime — never a second server.

Marionette hosts the stock ``python -m puppetmaster dashboard`` listener
(runfile + ``/api/meta`` identity). This module only resolves the project
state dir, reuses a live board, or starts the same CLI the MCP verb uses.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Optional
from urllib.parse import urlencode

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
SPAWN_WAIT_S = 10.0
_JOB_ID_MAX = 128

# Marionette owns at most one board: the active store's. Reuse-check and spawn
# are one atomic step so two callers cannot both spawn. Owned boards live in
# their own session (a crash-respawned backend adopts them), so they are
# stopped here on exit and recorded in a ledger a later boot can reap.
_LOCK = threading.Lock()
_owned: dict[str, Any] = {}
_ledger_path: Optional[str] = None
_exit_registered = False


def _stop_process(pid: int, proc: Any = None) -> None:
    try:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(3)
            except subprocess.TimeoutExpired:
                proc.kill()
        elif os.name == "posix":
            import signal
            os.kill(pid, signal.SIGTERM)
        else:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    except Exception:
        pass


def _read_ledger() -> dict[str, int]:
    try:
        with open(_ledger_path or "", encoding="utf-8") as f:
            data = json.load(f)
        return {str(k): int(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _write_ledger() -> None:
    if not _ledger_path:
        return
    try:
        tmp = _ledger_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({k: p.pid for k, p in _owned.items()}, f)
        os.replace(tmp, _ledger_path)
    except OSError:
        pass


def _release_owned(state_dir: str) -> None:
    proc = _owned.pop(state_dir, None)
    if proc is not None and proc.poll() is None:
        _stop_process(proc.pid, proc)


def stop_owned_dashboards() -> None:
    with _LOCK:
        for state_dir in list(_owned):
            _release_owned(state_dir)
        _write_ledger()


def reap_orphaned_dashboards(ledger_path: str) -> int:
    """Adopt the ledger and stop boards a crashed backend left running.

    A pid is only stopped while its store's runfile still names it, so a
    recycled pid or a board the user started themselves is never touched.
    """
    global _ledger_path
    from puppetmaster.dashboard import pid_alive, read_dashboard_runfile
    with _LOCK:
        _ledger_path = ledger_path
        stopped = 0
        for state_dir, pid in _read_ledger().items():
            tracked = read_dashboard_runfile(state_dir) or {}
            if state_dir not in _owned and pid_alive(pid) and tracked.get("pid") == pid:
                _stop_process(pid)
                stopped += 1
        _write_ledger()
        return stopped


def is_dashboard_job_id(job_id: str) -> bool:
    """True for a durable Puppetmaster ``job_…`` token (no path escapes).

    Local aliases like ``local-swarm-call_…`` are Jobs-rail row ids, not store
    tokens — rejecting them keeps ``python -m puppetmaster dashboard`` from
    dying with ``dashboard_failed_to_start``.
    """
    token = (job_id or "").strip()
    if not token or len(token) > _JOB_ID_MAX:
        return False
    if not token.startswith("job_"):
        return False
    body = token[4:]
    return bool(body) and all(ch.isalnum() or ch in "_-" for ch in body)


def is_benign_non_durable_job_token(job_id: str) -> bool:
    """True for Jobs-rail aliases we may ignore (open board, no deep-link).

    Path escapes and other unsafe tokens stay rejected at the API.
    """
    token = (job_id or "").strip()
    if not token or len(token) > _JOB_ID_MAX:
        return False
    if is_dashboard_job_id(token):
        return False
    return all(ch.isalnum() or ch in "_-" for ch in token)


def build_dashboard_url(
    host: str,
    port: int,
    job_id: Optional[str] = None,
    *,
    embed: bool = True,
) -> str:
    """Deep-link the local board. ``embed=1`` is best-effort; ``?job=`` always lands."""
    params: dict[str, str] = {}
    token = (job_id or "").strip()
    if token and is_dashboard_job_id(token):
        params["job"] = token
    if embed:
        params["embed"] = "1"
    query = urlencode(params)
    return f"http://{host}:{int(port)}/" + (f"?{query}" if query else "")


def resolve_dashboard_state_dir(repo: str = "", job_id: str = "", *, job_ref=None, default_dir=None) -> Optional[str]:
    """Resolve a durable job only to its owner; empty selections use the workspace."""
    token = (job_id or "").strip()
    if job_ref is not None:
        from puppetmaster.state import resolve_job_state

        return str(resolve_job_state(job_id=token, job_ref=job_ref, cwd=repo or None, default_dir=default_dir))
    if token and is_dashboard_job_id(token):
        from puppetmaster.state import find_state_dir_for_job

        found = find_state_dir_for_job(token)
        return str(found) if found else None
    from .cli_job_merge import resolve_cli_state_dir

    return resolve_cli_state_dir(repo or "")


def _reuse_tracked_dashboard(
    state_dir: str,
    host: str,
    port: int,
    all_projects: bool,
) -> Optional[dict[str, Any]]:
    from puppetmaster.dashboard import (
        dashboard_serves,
        normalize_dashboard_host,
        pid_alive,
        read_dashboard_runfile,
    )

    tracked = read_dashboard_runfile(state_dir)
    if not tracked:
        return None
    try:
        pid = int(tracked.get("pid") or 0)
        tracked_port = int(tracked.get("port") or port)
    except (TypeError, ValueError):
        return None
    tracked_host = str(tracked.get("host") or host)
    if not pid_alive(pid):
        return None
    if normalize_dashboard_host(tracked_host) != normalize_dashboard_host(host):
        return None
    if not dashboard_serves(tracked_host, tracked_port, state_dir, all_projects=all_projects):
        return None
    return {
        "ok": True,
        "reused": True,
        "host": tracked_host,
        "port": tracked_port,
        "pid": pid,
        "state_dir": state_dir,
    }


def _spawn_dashboard_cli(
    state_dir: str,
    host: str,
    port: int,
    job_id: Optional[str],
    *,
    popen: Callable[..., Any],
) -> subprocess.Popen:
    from puppetmaster.dashboard import dashboard_runfile

    command = [
        sys.executable,
        "-m",
        "puppetmaster",
        "--state-dir",
        state_dir,
        "dashboard",
        "--port",
        str(port),
        "--no-open",
        "--write-runfile",
        "--port-search",
    ]
    if host != DEFAULT_HOST:
        command += ["--host", host, "--allow-external"]
    token = (job_id or "").strip()
    if token and is_dashboard_job_id(token):
        command.append(token)
    child_log = dashboard_runfile(state_dir).with_name("dashboard.err.log")
    try:
        err_handle: Any = open(child_log, "w", encoding="utf-8")
    except OSError:
        err_handle = subprocess.DEVNULL
    try:
        return popen(
            command,
            cwd=os.getcwd(),
            env=os.environ.copy(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=err_handle,
            start_new_session=True,
        )
    finally:
        if err_handle is not subprocess.DEVNULL:
            err_handle.close()


def ensure_local_dashboard(
    *,
    state_dir: str,
    job_id: Optional[str] = None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    all_projects: bool = False,
    popen: Callable[..., Any] = subprocess.Popen,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    wait_s: float = SPAWN_WAIT_S,
) -> dict[str, Any]:
    """Return a live dashboard URL for ``state_dir``, starting the stock CLI if needed."""
    global _exit_registered
    with _LOCK:
        for other in [k for k in _owned if k != state_dir]:
            _release_owned(other)
        reused = _reuse_tracked_dashboard(state_dir, host, port, all_projects)
        if reused:
            reused["url"] = build_dashboard_url(reused["host"], reused["port"], job_id)
            reused["embed_url"] = reused["url"]
            _write_ledger()
            return reused
        _release_owned(state_dir)
        process = _spawn_dashboard_cli(state_dir, host, port, job_id, popen=popen)
        _owned[state_dir] = process
        _write_ledger()
        if not _exit_registered:
            import atexit
            atexit.register(stop_owned_dashboards)
            _exit_registered = True
        result = _await_spawned(process, state_dir, host, port, job_id, all_projects,
                                sleep=sleep, monotonic=monotonic, wait_s=wait_s)
        if not result.get("ok"):
            _release_owned(state_dir)
            _write_ledger()
        return result


def _await_spawned(process, state_dir, host, port, job_id, all_projects, *, sleep, monotonic, wait_s):
    from puppetmaster.dashboard import (
        dashboard_runfile,
        dashboard_serves,
        read_child_stderr_tail,
        read_dashboard_runfile,
    )
    deadline = monotonic() + wait_s
    child_info = None
    while monotonic() < deadline:
        candidate = read_dashboard_runfile(state_dir)
        if candidate and candidate.get("pid") == process.pid:
            child_info = candidate
            break
        if process.poll() is not None:
            break
        sleep(0.2)

    if child_info is None:
        child_log = dashboard_runfile(state_dir).with_name("dashboard.err.log")
        body: dict[str, Any] = {
            "ok": False,
            "error": "dashboard_failed_to_start",
            "host": host,
            "port": port,
            "state_dir": state_dir,
            "returncode": process.poll(),
        }
        stderr_tail = read_child_stderr_tail(child_log)
        if stderr_tail:
            body["stderr"] = stderr_tail
        return body

    bound_host = str(child_info.get("host") or host)
    bound_port = int(child_info.get("port") or port)
    if not dashboard_serves(bound_host, bound_port, state_dir, all_projects=all_projects):
        return {
            "ok": False,
            "error": "dashboard_identity_mismatch",
            "host": bound_host,
            "port": bound_port,
            "state_dir": state_dir,
        }
    url = build_dashboard_url(bound_host, bound_port, job_id)
    return {
        "ok": True,
        "reused": False,
        "host": bound_host,
        "port": bound_port,
        "pid": process.pid,
        "state_dir": state_dir,
        "url": url,
        "embed_url": url,
    }


def try_warm_local_dashboard(state_dir: str = "") -> dict[str, Any]:
    """Start or reuse the stock board. Empty state dir is a no-op, not a spawn."""
    token = (state_dir or "").strip()
    if not token:
        return {"ok": False, "error": "state_dir_unavailable"}
    try:
        return ensure_local_dashboard(state_dir=token)
    except Exception as exc:
        return {"ok": False, "error": "dashboard_warm_failed", "detail": str(exc)}
