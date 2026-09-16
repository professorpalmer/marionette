"""Live runner replacement and send admission share one short ownership gate."""
from __future__ import annotations

import copy
import os
import threading
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from functools import wraps
from dataclasses import dataclass
from typing import Any, Callable


def replacement_gate(pilot):
    # Created under the existing metadata lock, including on older runner shells.
    with getattr(pilot, '_busy_meta', None) or nullcontext():
        gate = getattr(pilot, '_replacement_gate', None)
        if gate is None:
            gate = pilot._replacement_gate = threading.RLock()
        return gate


@dataclass
class _InputAdmission:
    pilot: Any
    lifecycle: Any
    validate: Callable[[], None]
    stop_epoch: int


_admission = ContextVar('input_admission', default=None)


def check_input_owner(pilot):
    from .input_receipts import InputReceiptError
    if (getattr(pilot, '_replacement_pending', False)
            or getattr(pilot, '_replacement_retired', False)):
        raise InputReceiptError('input_session_changed', 'The input owner changed; keep your draft.')


@contextmanager
def input_publication(pilot):
    """Validate ownership before taking any native publication or input locks."""
    admission = _admission.get()
    lifecycle = admission.lifecycle if admission is not None and admission.pilot is pilot else None
    with lifecycle or nullcontext(), replacement_gate(pilot):
        check_input_owner(pilot)
        if admission is not None and admission.pilot is pilot:
            admission.validate()
            if admission.stop_epoch != getattr(pilot, '_input_stop_epoch', 0):
                from .input_receipts import InputReceiptError
                raise InputReceiptError('input_stopped', 'Stop cancelled this input admission; keep your draft and review it before sending again.')
        yield


@contextmanager
def input_admission(pilot, lifecycle=None, validate=lambda: None):
    """Reserve a runner without retaining locks across validation or vision."""
    with lifecycle or nullcontext(), replacement_gate(pilot):
        check_input_owner(pilot)
        validate()
        pilot._input_admissions = getattr(pilot, '_input_admissions', 0) + 1
        admission = _InputAdmission(pilot, lifecycle, validate, getattr(pilot, '_input_stop_epoch', 0))
    token = _admission.set(admission)
    try:
        yield
    finally:
        _admission.reset(token)
        with lifecycle or nullcontext(), replacement_gate(pilot):
            pilot._input_admissions -= 1


def note_input_stop(pilot, preserve_input_id=None):
    # Called under the replacement gate by the Stop receipt retirement path.
    pilot._input_stop_epoch = getattr(pilot, '_input_stop_epoch', 0) + 1
    current = _admission.get()
    if preserve_input_id and current is not None and current.pilot is pilot:
        # Interrupt-and-queue intentionally preserves this request's input.
        current.stop_epoch = pilot._input_stop_epoch


def input_projection(fn):
    @wraps(fn)
    def guarded(pilot, *args, **kwargs):
        with input_publication(pilot):
            return fn(pilot, *args, **kwargs)
    return guarded


def input_conversion(fn):
    """Keep replacement from snapshotting actions while vision is in flight."""
    @wraps(fn)
    def reserved(pilot, *args, **kwargs):
        current = _admission.get()
        if current is not None and current.pilot is pilot:
            return fn(pilot, *args, **kwargs)
        with input_admission(pilot):
            return fn(pilot, *args, **kwargs)
    return reserved


class LivePilotReplacement:
    def __init__(self, pilot):
        self.pilot = pilot
        self.committed = False
        self.actions_snapshot = None

    def __enter__(self):
        p = self.pilot
        with replacement_gate(p), getattr(p, '_steer_lock', None) or nullcontext(), \
                getattr(p, '_busy_meta', None) or nullcontext():
            if getattr(p, '_replacement_retired', False):
                raise RuntimeError('active session pilot was replaced')
            if getattr(p, '_input_admissions', 0):
                raise RuntimeError('input admission in progress -- retry rebuilding after delivery finishes')
            busy = getattr(p, '_busy', None)
            if busy is not None and busy.locked():
                raise RuntimeError('pilot busy -- finish or stop the current turn before rebuilding')
            actions = getattr(p, '_session_actions', None)
            if actions is not None:
                self.actions_snapshot = copy.deepcopy(actions.snapshot())
            if busy is not None and not busy.acquire(blocking=False):
                raise RuntimeError('pilot busy -- finish or stop the current turn before rebuilding')
            if actions is not None:
                actions.clear()
                actions.close()
            p._replacement_pending = True
            # A reservation is not a turn. Reapers skip since=0; old finally and
            # compaction commits lose their generation before history is copied.
            p._busy_gen = getattr(p, '_busy_gen', 0) + 1
            p._busy_since = 0.0
        from .cache_keep_warm import stop_runner_cache
        stop_runner_cache(p, reason="Session runner replacement in progress.", close=False)
        return self

    def commit(self):
        self.committed = True

    def __exit__(self, *_):
        p = self.pilot
        with replacement_gate(p), getattr(p, '_steer_lock', None) or nullcontext(), \
                getattr(p, '_busy_meta', None) or nullcontext():
            if not self.committed and self.actions_snapshot is not None:
                p._session_actions.restore(self.actions_snapshot)
            if self.committed:
                p._replacement_retired = True
                p._stop_holds_idle = True
                p._interrupt_requested = True
                cancel = getattr(p, '_cancel', None)
                if cancel is not None:
                    cancel.set()
            p._replacement_pending = False
            busy = getattr(p, '_busy', None)
            if busy is not None:
                busy.release()


def prepare_replacement(old, new, session_id, state_root, history, *, actions_snapshot):
    """Copy live state without transferring ownership to a different session."""
    if old is new:
        raise RuntimeError('replacement must be a new pilot')
    owner = getattr(old, '_prompt_queue_owner', None)
    expected = (os.path.normcase(os.path.realpath(state_root)), session_id)
    if owner is not None and owner != expected:
        raise RuntimeError('active session does not own the previous pilot')
    for pilot in (old, new):
        bound_id = getattr(pilot, 'harness_session_id', None)
        if isinstance(bound_id, str) and bound_id and bound_id != session_id:
            raise RuntimeError('active session does not own the pilot')
    new_owner = getattr(new, '_prompt_queue_owner', None)
    if new_owner is not None and new_owner != expected:
        raise RuntimeError('active session does not own the replacement pilot')
    if session_id:
        bind = getattr(new, 'bind_prompt_queue', None)
        if callable(bind):
            bind(state_root, session_id)
        new.harness_session_id = session_id
    if history is not None:
        new._history = copy.deepcopy(history)
    for name in ('_display_transcript', '_session_job_ids', '_auto_distill',
                 '_stop_holds_idle', '_cold_input_hold', '_interrupted_swarms',
                 '_input_upload_root', '_pending_steer_drop_notice',
                 '_pending_owned_command_orphan_notice', '_steer_boundary_drop_on_acquire'):
        if hasattr(old, name):
            setattr(new, name, copy.deepcopy(getattr(old, name)))
    # Do not call load_history: its cold-attach hold semantics are intentional
    # for a restart, but this is the same live receipt-store incarnation.
    if owner is not None:
        from .input_receipts import session_input_store
        store = session_input_store(old)
        if (os.path.normcase(os.path.realpath(store.root)), store.session_id) != expected:
            raise RuntimeError('active session does not own the input store')
        new._input_receipts = store
    if actions_snapshot is not None:
        snapshot = copy.deepcopy(actions_snapshot)
        snapshot['current_turn_id'] = None
        new._session_actions.restore(snapshot)
    for name in ('_restore_pending_command_approvals_from_display',
                 'reload_session_goal', 'reload_session_todos'):
        reload = getattr(new, name, None)
        if callable(reload):
            reload()
