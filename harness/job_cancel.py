"""Cooperative dual-store job cancel helpers (PM-free seam).

Shared by ``POST /api/swarm/cancel`` and ``BusyControlMixin.interrupt`` so both
paths resolve harness + CLI durable stores the same way. Cancel is cooperative
only — never force-kills Python threads.
"""

from __future__ import annotations

from typing import Any, Iterable, Iterator, Optional


def mark_store_job_cancelled(store: Any, job_id: str) -> bool:
    """Best-effort mark ``job_id`` cancelled on a durable store.

    Tries ``cancel_job`` / ``set_job_status`` / ``update_job_status`` in that
    order. Returns True when any method succeeds.
    """
    if store is None or not job_id:
        return False
    for meth in ("cancel_job", "set_job_status", "update_job_status"):
        fn = getattr(store, meth, None)
        if fn is None:
            continue
        try:
            fn(job_id) if meth == "cancel_job" else fn(job_id, "cancelled")
            return True
        except Exception:
            continue
    return False


def iter_dual_stores(
    harness_store: Any = None,
    *,
    repo_root: str = "",
) -> Iterator[Any]:
    """Yield harness then CLI durable stores (skipping missing/None)."""
    seen: set[int] = set()
    if harness_store is not None:
        seen.add(id(harness_store))
        yield harness_store
    try:
        from .cli_job_merge import open_cli_durable_state

        cli_state = open_cli_durable_state(repo_root or "")
        cli_store = getattr(cli_state, "store", None) if cli_state is not None else None
        if cli_store is not None and id(cli_store) not in seen:
            yield cli_store
    except Exception:
        return


def store_knows_job(store: Any, job_id: str, list_jobs=None) -> bool:
    """True when ``job_id`` appears in the store's job list."""
    if store is None or not job_id:
        return False
    try:
        rows = list_jobs() if list_jobs is not None else store.list_jobs()
    except Exception:
        return False
    for j in rows or []:
        jid = j.get("id") if isinstance(j, dict) else getattr(j, "id", None)
        if jid == job_id:
            return True
    return False


def cancel_job_dual_store(
    job_id: str,
    *,
    harness_store: Any = None,
    harness_list_jobs=None,
    repo_root: str = "",
) -> Optional[dict]:
    """Cancel ``job_id`` when it is a member of harness or CLI durable store.

    Membership-gated (HTTP cancel): only the store that lists the job is
    written. Returns ``{ok, job_id, durable, marked}`` or None if unknown.
    """
    job_id = (job_id or "").strip()
    if not job_id:
        return None

    # Harness first (may use DurableState.list_jobs wrapper).
    if harness_store is not None and store_knows_job(
        harness_store, job_id, harness_list_jobs
    ):
        return {
            "ok": True,
            "job_id": job_id,
            "durable": True,
            "marked": mark_store_job_cancelled(harness_store, job_id),
        }

    # CLI durable store second (workspace-scoped).
    for store in iter_dual_stores(None, repo_root=repo_root):
        if store is harness_store:
            continue
        if not store_knows_job(store, job_id):
            continue
        return {
            "ok": True,
            "job_id": job_id,
            "durable": True,
            "marked": mark_store_job_cancelled(store, job_id),
        }

    # Cross-project live jobs (Cursor MCP under a sibling cwd) land in another
    # PM project store; resolve by job id so Cancel still works from the tracker.
    try:
        from puppetmaster.state import find_state_dir_for_job

        foreign = find_state_dir_for_job(job_id)
    except Exception:
        foreign = None
    if foreign is not None:
        try:
            from .cli_job_merge import open_cli_durable_at

            durable = open_cli_durable_at(str(foreign))
            store = getattr(durable, "store", None) if durable is not None else None
            if store is not None and store_knows_job(store, job_id):
                return {
                    "ok": True,
                    "job_id": job_id,
                    "durable": True,
                    "marked": mark_store_job_cancelled(store, job_id),
                }
        except Exception:
            pass
    return None


def drain_job_ids_dual_store(
    job_ids: Iterable[str],
    *,
    harness_store: Any = None,
    repo_root: str = "",
) -> list[str]:
    """Cooperatively cancel every id across harness + CLI stores.

    Used by session interrupt: session-tracked ids must not strand as
    actionable in either store. Membership-gated like
    ``cancel_job_dual_store`` so a sibling store that upserts on
    ``update_job_status`` cannot gain phantom cancelled rows for jobs it
    never listed. Returns ids for which at least one store accepted a
    mark. Never raises; never force-kills threads.
    """
    cancelled: list[str] = []
    for raw in job_ids:
        jid = (raw or "").strip()
        if not jid:
            continue
        hit = False
        for store in iter_dual_stores(harness_store, repo_root=repo_root):
            if not store_knows_job(store, jid):
                continue
            if mark_store_job_cancelled(store, jid):
                hit = True
        if hit:
            cancelled.append(jid)
    return cancelled
