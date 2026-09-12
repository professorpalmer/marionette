"""Reuse Puppetmaster's local dashboard runtime — never a second server.

Marionette hosts the stock ``python -m puppetmaster dashboard`` listener
(runfile + ``/api/meta`` identity). This module only resolves the project
state dir, reuses a live board, or starts the same CLI the MCP verb uses.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Any, Callable, Optional
from urllib.parse import urlencode

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
SPAWN_WAIT_S = 10.0
_JOB_ID_MAX = 128


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


def resolve_dashboard_state_dir(repo: str = "", job_id: str = "") -> Optional[str]:
    """Prefer the store that owns ``job_id``, else the workspace CLI state dir."""
    token = (job_id or "").strip()
    if token and is_dashboard_job_id(token):
        try:
            from puppetmaster.state import find_state_dir_for_job

            found = find_state_dir_for_job(token)
            if found:
                return str(found)
        except Exception:
            pass
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
    from puppetmaster.dashboard import (
        dashboard_runfile,
        dashboard_serves,
        read_child_stderr_tail,
        read_dashboard_runfile,
    )

    reused = _reuse_tracked_dashboard(state_dir, host, port, all_projects)
    if reused:
        reused["url"] = build_dashboard_url(reused["host"], reused["port"], job_id)
        reused["embed_url"] = reused["url"]
        return reused

    process = _spawn_dashboard_cli(state_dir, host, port, job_id, popen=popen)
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
