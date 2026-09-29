"""The approval sweeper must not keep a dropped runner alive."""
import gc
import threading
import time
import weakref

import harness.conversation as conv


def _session(pending):
    s = conv.ConversationalSession.__new__(conv.ConversationalSession)
    s._approval_sweeper = None
    s._approval_sweep_stop = threading.Event()
    s._command_approval_lock = threading.Lock()
    s._pending_command_approvals = pending
    return s


def test_dropped_runner_is_collected_and_sweeper_exits(monkeypatch):
    monkeypatch.setattr(conv, 'APPROVAL_SWEEP_SECONDS', 0.01)
    s = _session({'h': {'expires_at': time.time() + 3600}})
    s._ensure_approval_sweeper()
    thread = s._approval_sweeper
    ref = weakref.ref(s)
    del s
    gc.collect()
    assert ref() is None
    thread.join(2)
    assert not thread.is_alive()


def test_sweeper_stops_once_nothing_is_pending(monkeypatch):
    monkeypatch.setattr(conv, 'APPROVAL_SWEEP_SECONDS', 0.01)
    s = _session({})
    s._ensure_approval_sweeper()
    thread = s._approval_sweeper
    thread.join(2)
    assert not thread.is_alive()
    assert s._approval_sweeper is None
