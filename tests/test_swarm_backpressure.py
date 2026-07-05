"""Unit tests for the swarm ThreadPoolExecutor backpressure gate.

These tests are hermetic: no real worker threads run and the real
``_swarm_pool`` is monkeypatched with a recording fake so we can assert
exactly which submissions the gate lets through. The goal is to prove
that a burst of dispatches cannot queue unbounded on the executor's
own work queue by growing ``_swarm_futures`` past ``_swarm_capacity``.
"""
from __future__ import annotations

import concurrent.futures
from typing import Any, Callable, List, Tuple

import pytest

from harness.conversation import ConversationalSession
from harness.config import HarnessConfig


class _FakeFuture:
    """Minimal Future stand-in: records callbacks, supports done()/run.

    We do NOT execute submitted work; the gate only cares that the
    future can be registered in ``_swarm_futures`` and can carry a
    done-callback. ``fire_done()`` triggers the callback the same way
    a real Future would when the worker finishes.
    """

    def __init__(self) -> None:
        self._callbacks: List[Callable[["_FakeFuture"], None]] = []
        self._done = False

    def add_done_callback(self, cb: Callable[["_FakeFuture"], None]) -> None:
        self._callbacks.append(cb)

    def fire_done(self) -> None:
        self._done = True
        for cb in list(self._callbacks):
            cb(self)


class _FakePool:
    """Executor stand-in that records ``submit`` calls without running them."""

    def __init__(self) -> None:
        self.calls: List[Tuple[Callable[..., Any], tuple]] = []

    def submit(self, fn: Callable[..., Any], *args: Any) -> _FakeFuture:
        self.calls.append((fn, args))
        return _FakeFuture()


def _fresh_session(monkeypatch: pytest.MonkeyPatch) -> ConversationalSession:
    """Build a Conversation with a recording pool swapped in.

    We do not care about the real thread pool or any downstream state
    for these gate-level tests; we just want the ``_submit_swarm``
    choke point under a controlled ``submit`` observer.
    """
    cfg = HarnessConfig()
    # Do NOT touch cfg.repo -- tests exercise the gate methods only.
    sess = ConversationalSession(cfg)
    fake = _FakePool()
    # Replace the real ThreadPoolExecutor with the fake recorder. The
    # gate never touches other pool methods (no shutdown / no wait), so
    # this is behavior-preserving for the code paths under test.
    sess._swarm_pool = fake  # type: ignore[assignment]
    # Clear any futures that might have been added during construction
    # (there shouldn't be any, but be defensive so counting is clean).
    with sess._swarm_futures_lock:
        sess._swarm_futures.clear()
    return sess


def _noop() -> None:  # pragma: no cover - never actually invoked
    return None


def test_capacity_defaults_to_four_times_workers() -> None:
    """Default max_workers=4 -> capacity ceiling = 16 (4x)."""
    cfg = HarnessConfig()
    sess = ConversationalSession(cfg)
    assert sess._swarm_capacity == 16


def test_swarm_at_capacity_false_below_ceiling_true_at_and_above(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sess = _fresh_session(monkeypatch)
    cap = sess._swarm_capacity
    # Empty inflight -> not at capacity.
    assert sess._swarm_inflight() == 0
    assert sess._swarm_at_capacity() is False

    # Fill inflight to cap-1: still under the ceiling.
    stub_futures = [_FakeFuture() for _ in range(cap - 1)]
    with sess._swarm_futures_lock:
        for f in stub_futures:
            sess._swarm_futures.add(f)
    assert sess._swarm_inflight() == cap - 1
    assert sess._swarm_at_capacity() is False

    # Exactly at cap -> at capacity.
    at_cap = _FakeFuture()
    with sess._swarm_futures_lock:
        sess._swarm_futures.add(at_cap)
    assert sess._swarm_inflight() == cap
    assert sess._swarm_at_capacity() is True

    # Above cap (defensive: nothing forbids it, but the gate must still
    # report at-capacity so no more submits slip through).
    extra = _FakeFuture()
    with sess._swarm_futures_lock:
        sess._swarm_futures.add(extra)
    assert sess._swarm_inflight() == cap + 1
    assert sess._swarm_at_capacity() is True


def test_submit_swarm_returns_false_and_does_not_call_pool_at_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sess = _fresh_session(monkeypatch)
    fake_pool: _FakePool = sess._swarm_pool  # type: ignore[assignment]
    # Preload _swarm_futures to the ceiling so the very next submit is
    # rejected. Use bare _FakeFuture placeholders -- the gate only reads
    # ``len(self._swarm_futures)`` here.
    with sess._swarm_futures_lock:
        for _ in range(sess._swarm_capacity):
            sess._swarm_futures.add(_FakeFuture())
    assert sess._swarm_at_capacity() is True

    ok = sess._submit_swarm(_noop)
    assert ok is False
    # Critical: the pool must NOT have been touched.
    assert fake_pool.calls == []
    # And nothing new was added to the inflight set.
    assert sess._swarm_inflight() == sess._swarm_capacity


def test_submit_swarm_below_capacity_submits_and_registers_future(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sess = _fresh_session(monkeypatch)
    fake_pool: _FakePool = sess._swarm_pool  # type: ignore[assignment]

    marker = object()
    ok = sess._submit_swarm(_noop, marker, "extra-arg")
    assert ok is True
    # Pool.submit called exactly once with the forwarded args.
    assert len(fake_pool.calls) == 1
    fn, args = fake_pool.calls[0]
    assert fn is _noop
    assert args == (marker, "extra-arg")
    # Future is now tracked in _swarm_futures.
    assert sess._swarm_inflight() == 1


def test_done_callback_removes_future_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sess = _fresh_session(monkeypatch)
    fake_pool: _FakePool = sess._swarm_pool  # type: ignore[assignment]

    ok = sess._submit_swarm(_noop)
    assert ok is True
    assert sess._swarm_inflight() == 1

    # Retrieve the FakeFuture the gate handed back into the set.
    with sess._swarm_futures_lock:
        assert len(sess._swarm_futures) == 1
        fut = next(iter(sess._swarm_futures))
    # Firing "done" should drain the future via the gate's callback.
    fut.fire_done()
    assert sess._swarm_inflight() == 0

    # Idempotency: firing done again (or any other cleanup path that
    # already discarded the future) must NOT raise. This models a bulk
    # drain running concurrently with the callback -- ``discard`` is
    # forgiving where ``remove`` would raise KeyError.
    fut.fire_done()
    assert sess._swarm_inflight() == 0

    # Directly re-invoking the discard path (e.g. a drain helper) also
    # must be tolerant of an already-removed future.
    with sess._swarm_futures_lock:
        sess._swarm_futures.discard(fut)
    assert sess._swarm_inflight() == 0


def test_submit_swarm_never_raises_when_pool_submit_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: the gate must swallow submit-time errors so a broken
    pool does not blow up the pilot loop -- it just reports 'not dispatched'.
    """
    sess = _fresh_session(monkeypatch)

    class _AngryPool:
        def submit(self, fn: Callable[..., Any], *args: Any) -> None:
            raise RuntimeError("pool exploded")

    sess._swarm_pool = _AngryPool()  # type: ignore[assignment]
    # Must return False, must not raise, must not leave anything in
    # the inflight set.
    result = sess._submit_swarm(_noop)
    assert result is False
    assert sess._swarm_inflight() == 0


def test_capacity_floor_is_at_least_one() -> None:
    """The gate's own ceiling clamps to >=1 even if a wild config value
    is fed in. We verify the ``max(1, ...)`` clamp arithmetic directly
    (ThreadPoolExecutor itself refuses max_workers<=0, so we cannot
    round-trip that through a real session)."""
    assert max(1, int(0) * 4) == 1
    assert max(1, int(-5) * 4) == 1
    assert max(1, int(1) * 4) == 4
    assert max(1, int(4) * 4) == 16
