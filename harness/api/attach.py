"""Attach / rebuild helpers peeled from ``harness.server``.

View attach, deferred cold-build readiness, and active-runner rebuild take an
:class:`AttachServices` so this module never imports ``harness.server`` at
top level. ``server.py`` builds services from its module globals and keeps
thin ``_attach_view`` / ``_ensure_active_pilot_ready`` / … wrappers so tests
and callers keep importing historical names.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from ..deferred_attach import (
    DeferredPilotPlaceholder,
    defer_cold_attach_enabled,
    is_deferred_placeholder,
    normalize_transcript_payload,
    schedule_deferred_build,
)
from ..session import Session
from ..conversation import ConversationalSession
from ..sessions import load_transcript


@dataclass
class AttachServices:
    """Explicit deps for attach/rebuild helpers (injected by ``server.py``)."""

    get_pilot: Callable[[], Any]
    set_pilot: Callable[[Any], None]
    get_session: Callable[[], Any]
    set_session: Callable[[Any], None]
    cfg: Any
    runners: Any
    sessions: Any
    pilot_swap_lock: Any
    bind_pilot_services: Callable[[Any], None]
    build_conversational_pilot: Callable[..., Any]
    sync_pilot_session_id: Callable[[], None]
    sessions_state_dir: Callable[[], str]
    diag: Callable[..., None]
    apply_model_context_window: Callable[[], None]
    freeze_pilot_meters_into_boot_carry: Callable[[Any], None]
    runner_config_snapshot: Callable[[], Any]


def attach_view(
    session_id: str,
    svc: AttachServices,
    *,
    factory=None,
    load_transcript_on_create: bool = True,
    defer_cold_build: Optional[bool] = None,
) -> Any:
    """Point the UI at ``session_id`` via the runner registry.

    Warm path (runner already exists): set_active_view + assign global ``_pilot``
    without calling the factory.

    Cold path: get_or_create under the lease. When ``defer_cold_build=True``
    and ``HARNESS_DEFER_COLD_ATTACH`` is not off, register an idle
    ``DeferredPilotPlaceholder`` with the disk transcript and schedule
    ``ConversationalSession`` construction off the response path (Hermes
    ``_schedule_agent_build``). Opt-in only -- default callers stay synchronous.
    Turn-start paths must call ``ensure_active_pilot_ready`` so chats never
    race a half-built pilot.

    Raises ``LeaseExhaustedError`` when a new runner is required but every
    lease slot holds a busy runner.
    """
    if not session_id:
        raise ValueError("session_id required to attach view")

    # --- Warm fast path: never rebuild; never interrupt other runners. ---
    existing = svc.runners.get(session_id)
    if existing is not None:
        # Failed deferred cold build sticks warm forever unless we drop it —
        # mark_failed clears defer_building but leaves the shell in the registry.
        if (
            is_deferred_placeholder(existing)
            and getattr(existing, "build_error", None) is not None
        ):
            svc.runners.drop(session_id, notify=False)
            existing = None
        else:
            svc.runners.set_active_view(session_id)
            with svc.pilot_swap_lock:
                svc.set_pilot(existing)
                try:
                    svc.get_session().state_dir = svc.get_pilot().state_dir
                except Exception:
                    pass
                svc.bind_pilot_services(svc.get_pilot())
                svc.sync_pilot_session_id()
                # S3: warm-reuse after relocate/workspace-open may leave ACP on
                # the previous root — retarget closes only when cwd differs.
                try:
                    release = getattr(svc.get_pilot(), "release_warm_acp", None)
                    if callable(release):
                        release(
                            reason="workspace",
                            cwd=getattr(svc.cfg, "repo", None),
                        )
                except Exception as e:
                    svc.diag("server.attach_warm_acp_workspace", e)
            return svc.get_pilot()

    created = True
    # Opt-in only: callers that need Hermes-style cold attach pass
    # defer_cold_build=True (sessions/switch, create, workspace/open). Other
    # paths stay synchronous so rebuild/meter tests are not raced.
    want_defer = (
        factory is None
        and defer_cold_build is True
        and defer_cold_attach_enabled()
    )

    history: Any = []
    if load_transcript_on_create:
        history = load_transcript(svc.sessions_state_dir(), session_id)
    transcript_payload = normalize_transcript_payload(history)

    if want_defer:
        placeholder = DeferredPilotPlaceholder(
            session_id=session_id,
            state_dir=svc.sessions_state_dir(),
            transcript=transcript_payload,
        )
        placeholder._pending_history = history

        def _factory():
            return placeholder

        runner = svc.runners.get_or_create(session_id, _factory)
        svc.runners.set_active_view(session_id)
        with svc.pilot_swap_lock:
            svc.set_pilot(runner)
            try:
                svc.get_session().state_dir = svc.get_pilot().state_dir
            except Exception:
                pass
            svc.bind_pilot_services(svc.get_pilot())
            svc.sync_pilot_session_id()

        def _build():
            return svc.build_conversational_pilot()

        def _on_done(real: Any) -> None:
            try:
                svc.bind_pilot_services(real)
                # Session ownership must be set before hydrate so pending
                # command-approval restore can refuse foreign display rows.
                real.harness_session_id = session_id
                # Prefer the placeholder's live transcript: callers may
                # load_history() after cold attach (tests + resume paths)
                # while this build was still in flight. The attach-time
                # ``history`` closure would otherwise wipe those turns.
                live = placeholder.export_transcript_data()
                hydrate = (
                    live
                    if (
                        live.get("history")
                        or live.get("display")
                        or live.get("job_ids")
                    )
                    else history
                )
                if load_transcript_on_create and hydrate:
                    real.load_history(hydrate)
            except Exception as e:
                svc.diag("server.deferred_pilot_hydrate", e)
                placeholder.mark_failed(e)
                return
            with svc.pilot_swap_lock:
                current = svc.runners.get(session_id)
                if current is not placeholder:
                    # View dropped or replaced while building — abandon swap.
                    placeholder.mark_ready(real)
                    return
                try:
                    svc.runners.replace(session_id, real, notify=False)
                except Exception as e:
                    svc.diag("server.deferred_pilot_replace", e)
                    placeholder.mark_failed(e)
                    return
                if svc.runners.active_view_id == session_id:
                    svc.set_pilot(real)
                    try:
                        svc.get_session().state_dir = real.state_dir
                    except Exception:
                        pass
                    svc.bind_pilot_services(real)
                    svc.sync_pilot_session_id()
            placeholder.mark_ready(real)

        def _on_error(exc: BaseException) -> None:
            svc.diag("server.deferred_pilot_build", exc, msg=f"sid={session_id}")
            placeholder.mark_failed(exc)

        schedule_deferred_build(_build, on_done=_on_done, on_error=_on_error)
        return runner

    def _factory():
        if factory is not None:
            return factory()
        # New runners start at zero meters -- boot pill sums carry + live.
        return svc.build_conversational_pilot()

    runner = svc.runners.get_or_create(session_id, _factory)
    svc.runners.set_active_view(session_id)
    with svc.pilot_swap_lock:
        svc.set_pilot(runner)
        # Keep tracker/jobs pointed at the store this runner writes to.
        try:
            svc.get_session().state_dir = svc.get_pilot().state_dir
        except Exception:
            pass
        svc.bind_pilot_services(svc.get_pilot())
        # Session ownership before hydrate so pending DANGER approval restore
        # validates display rows against the owning session.
        svc.sync_pilot_session_id()
        # Existing runners already hold live history (including in-flight turns).
        # Only hydrate from disk when the runner was just created.
        if created and load_transcript_on_create:
            svc.get_pilot().load_history(history)
    return svc.get_pilot()


def ensure_active_pilot_ready(svc: AttachServices, *, timeout: float = 120.0) -> Any:
    """Block until the active view's deferred cold build finishes (if any).

    No-op for warm / sync runners. Raises on timeout or build failure so turn
    starts never execute against a half-built placeholder.
    """
    pilot = svc.get_pilot()
    if not is_deferred_placeholder(pilot):
        return pilot
    real = pilot.ensure_ready(timeout=timeout)
    with svc.pilot_swap_lock:
        # Background swap usually already updated _pilot; repair if not.
        if is_deferred_placeholder(svc.get_pilot()):
            svc.set_pilot(real)
            try:
                svc.get_session().state_dir = real.state_dir
            except Exception:
                pass
            svc.bind_pilot_services(real)
            svc.sync_pilot_session_id()
        elif svc.runners.active_view_id == getattr(real, "harness_session_id", None):
            # Prefer registry live runner when active view matches.
            live = svc.runners.get(svc.runners.active_view_id or "")
            if live is not None and not is_deferred_placeholder(live):
                svc.set_pilot(live)
    return svc.get_pilot()


def gate_active_pilot_ready(svc: AttachServices, *, timeout: float = 120.0) -> Optional[dict]:
    """Ensure the active pilot is a real ConversationalSession.

    Returns a JSON error body for a 409 when the deferred build is still
    running / failed; ``None`` when the pilot is ready to mutate.
    """
    try:
        ensure_active_pilot_ready(svc, timeout=timeout)
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "code": "pilot_not_ready",
        }
    return None


def attach_view_transcript_payload(
    runner: Any,
    session_id: str,
    svc: AttachServices,
) -> dict[str, list]:
    """Transcript for attach/switch responses (live runner, else disk)."""
    try:
        if runner is not None and hasattr(runner, "export_transcript_data"):
            return normalize_transcript_payload(runner.export_transcript_data())
    except Exception as e:
        svc.diag("server.attach_transcript_export", e)
    return normalize_transcript_payload(
        load_transcript(svc.sessions_state_dir(), session_id)
    )


def rebuild_pilot_and_session(svc: AttachServices) -> None:
    """Rebuild the ACTIVE view's runner for the current driver, preserving history.

    Only replaces the active view's entry in the registry -- never wipes other
    busy runners. If the active runner is mid-turn, refuse (callers that need
    a hard swap should 409 first).

    Defensive: if the configured driver cannot be built (e.g. a stale saved
    spec the catalog no longer knows), do NOT let the exception escape and
    crash the POST handler -- that left the whole app dead on workspace-open /
    session-switch. We roll back to the previous working driver and surface the
    error to the caller to show, instead of taking down the process.
    """
    # Finish any deferred cold build before touching _history / meters.
    ensure_active_pilot_ready(svc)
    active_id = svc.sessions.active or svc.runners.active_view_id
    if active_id:
        existing = svc.runners.get(active_id)
        if existing is not None:
            busy = getattr(existing, "_busy", None)
            locked = getattr(busy, "locked", None) if busy is not None else None
            if callable(locked) and locked():
                raise RuntimeError("pilot busy -- finish or stop the current turn before rebuilding")

    prev_driver = svc.cfg.driver
    svc.apply_model_context_window()
    try:
        # Tracker Session may share the view config; the runner gets a frozen copy.
        new_session = Session(svc.cfg)
        new_pilot = ConversationalSession(svc.runner_config_snapshot())
    except Exception as e:
        # Roll back to the last driver that built successfully.
        svc.cfg.driver = prev_driver
        svc.apply_model_context_window()
        raise RuntimeError(
            f"could not load model {prev_driver!r}: {e}. Reverted to the "
            f"previous pilot."
        ) from e
    # Keep the tracker/jobs reads pointed at the store the pilot writes to (see
    # the pin at initial construction) across workspace/driver switches too.
    new_session.state_dir = new_pilot.state_dir
    with svc.pilot_swap_lock:
        old_history = svc.get_pilot()._history
        old_auto_distill = getattr(svc.get_pilot(), "_auto_distill", False)
        old_pilot = svc.get_pilot()
        # Freeze at the OLD runner's bound rates (``_cfg.driver`` may already
        # point at the new model). Replacement starts with zero cost meters.
        try:
            svc.freeze_pilot_meters_into_boot_carry(old_pilot)
        except Exception:
            pass
        # S3: rebuild owns the outgoing warm ACP — close before pointer swap
        # so Windows cannot orphan agent acp children (drop also releases).
        try:
            release = getattr(old_pilot, "release_warm_acp", None)
            if callable(release):
                release(reason="session_switch")
        except Exception as e:
            svc.diag("server.rebuild_warm_acp_close", e)
        svc.set_session(new_session)
        svc.set_pilot(new_pilot)
        svc.get_pilot()._history = old_history
        svc.get_pilot()._auto_distill = old_auto_distill
        svc.bind_pilot_services(svc.get_pilot())
        svc.sync_pilot_session_id()
        if active_id:
            # Replace only this view's registry entry; leave other runners alone.
            # notify=False: meters already frozen above; drop must not re-fold.
            svc.runners.drop(active_id, notify=False)
            svc.runners.get_or_create(active_id, lambda: svc.get_pilot())
            svc.runners.set_active_view(active_id)
