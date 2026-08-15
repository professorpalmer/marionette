"""Concurrency-seam tests for mid-turn steer enqueue / drain / drop.

Wiki rule Concurrency Seam Testing (v0.9.105): contend the lock with a
barrier, then assert the post-join partition. No wall-clock latency checks.

Production seam (harness/steer_mixin.py):
- drop_queued_steers:50 takes ``_steer_lock`` and returns stripped items
- _abandoned_turn_blocks_steer_enqueue:174 is True only when
  ``_stop_holds_idle`` coincides with ``_busy.locked()``
- enqueue_steer:194 strips blanks, refuses abandon, then appends under lock
- drain_steer:211 pops the whole queue under the same lock
"""
from __future__ import annotations

import collections
import threading

from harness.steer_mixin import SteerMixin


class _SteerHost(SteerMixin):
    """Minimal host: mixin methods plus the fields they read and write."""

    def __init__(self) -> None:
        self._steer_lock = threading.Lock()
        self._steer_queue = collections.deque()
        self._steer_pending = False
        self._stop_holds_idle = False
        self._busy = threading.Lock()
        self._pending_steer_drop_notice = None
        self._display_transcript = []


def _join_threads(threads):
    for thread in threads:
        thread.join(timeout=5)
    still_running = [thread.name for thread in threads if thread.is_alive()]
    assert still_running == [], still_running


def test_concurrent_enqueue_and_drain_preserves_every_text_exactly_once():
    """N producers barrier-sync then enqueue; drainers run in the same window."""
    host = _SteerHost()
    producer_count = 16
    drainer_count = 3
    texts = ["steer-%02d" % i for i in range(producer_count)]
    barrier = threading.Barrier(producer_count + drainer_count)
    producers_done = threading.Event()
    drained = []
    drain_lock = threading.Lock()
    errors = []

    def produce(text):
        try:
            barrier.wait(timeout=5)
            host.enqueue_steer(text)
        except Exception as exc:  # pragma: no cover - failure surfaces below
            errors.append(exc)

    def drain():
        try:
            barrier.wait(timeout=5)
            while True:
                items = host.drain_steer()
                if items:
                    with drain_lock:
                        drained.extend(items)
                if producers_done.is_set():
                    leftover = host.drain_steer()
                    if leftover:
                        with drain_lock:
                            drained.extend(leftover)
                    break
        except Exception as exc:  # pragma: no cover - failure surfaces below
            errors.append(exc)

    threads = [threading.Thread(target=produce, args=(text,)) for text in texts]
    threads.extend(threading.Thread(target=drain) for _ in range(drainer_count))
    for thread in threads:
        thread.start()
    _join_threads(threads[:producer_count])
    producers_done.set()
    _join_threads(threads[producer_count:])

    leftover = host.drain_steer()
    drained.extend(leftover)

    assert errors == []
    assert leftover == []
    assert "" not in drained
    assert all(item.strip() for item in drained)
    assert sorted(drained) == texts
    assert len(drained) == len(set(drained)) == producer_count
    assert list(host._steer_queue) == []


def test_concurrent_drop_and_enqueue_partition_without_loss_or_double_count():
    """Queue remainder plus dropped items equal the enqueued set."""
    host = _SteerHost()
    enqueue_count = 16
    dropper_count = 4
    texts = ["drop-steer-%02d" % i for i in range(enqueue_count)]
    barrier = threading.Barrier(enqueue_count + dropper_count)
    producers_done = threading.Event()
    dropped = []
    drop_lock = threading.Lock()
    errors = []

    def produce(text):
        try:
            barrier.wait(timeout=5)
            host.enqueue_steer(text)
        except Exception as exc:  # pragma: no cover - failure surfaces below
            errors.append(exc)

    def drop():
        try:
            barrier.wait(timeout=5)
            while True:
                items = host.drop_queued_steers()
                if items:
                    with drop_lock:
                        dropped.extend(items)
                if producers_done.is_set():
                    leftover = host.drop_queued_steers()
                    if leftover:
                        with drop_lock:
                            dropped.extend(leftover)
                    break
        except Exception as exc:  # pragma: no cover - failure surfaces below
            errors.append(exc)

    threads = [threading.Thread(target=produce, args=(text,)) for text in texts]
    threads.extend(threading.Thread(target=drop) for _ in range(dropper_count))
    for thread in threads:
        thread.start()
    _join_threads(threads[:enqueue_count])
    producers_done.set()
    _join_threads(threads[enqueue_count:])

    remaining = host.drain_steer()

    assert errors == []
    combined = remaining + dropped
    assert "" not in combined
    assert len(combined) == enqueue_count
    assert sorted(combined) == texts
    assert set(remaining) | set(dropped) == set(texts)
    assert set(remaining) & set(dropped) == set()
    assert len(remaining) == len(set(remaining))
    assert len(dropped) == len(set(dropped))


def test_abandoned_turn_blocks_enqueue_only_while_busy_is_held():
    """Stop hold + locked busy refuses enqueue and records a drop notice.

    Idle sessions still accept steers when ``_stop_holds_idle`` is sticky.
    """
    host = _SteerHost()
    host._stop_holds_idle = True
    assert host._busy.acquire(blocking=False)
    try:
        assert host._abandoned_turn_blocks_steer_enqueue() is True
        host.enqueue_steer("late steer after stop")
        assert list(host._steer_queue) == []
        assert host.drain_steer() == []
        notice = host._pending_steer_drop_notice
        assert notice is not None
        assert notice["reason"] == "steer_dropped"
        assert notice["count"] == 1
        assert "Dropped 1 queued steer" in notice["message"]
        assert host._display_transcript
        assert host._display_transcript[0]["role"] == "assistant"
        assert host._display_transcript[0]["text"] == notice["message"]
    finally:
        host._busy.release()

    # Sticky idle hold on a ready session must not refuse enqueue.
    host._pending_steer_drop_notice = None
    host._display_transcript = []
    assert host._busy.locked() is False
    assert host._abandoned_turn_blocks_steer_enqueue() is False
    host.enqueue_steer("valid after idle hold")
    assert host.drain_steer() == ["valid after idle hold"]
    assert host._pending_steer_drop_notice is None

    # Busy without the Stop hold is a live turn: enqueue stays open.
    assert host._busy.acquire(blocking=False)
    try:
        host._stop_holds_idle = False
        assert host._abandoned_turn_blocks_steer_enqueue() is False
        host.enqueue_steer("live-turn steer")
        assert host.drain_steer() == ["live-turn steer"]
    finally:
        host._busy.release()


def test_enqueue_steer_ignores_blank_and_whitespace():
    """Blank composer text never enters the queue (existing contract)."""
    host = _SteerHost()
    host.enqueue_steer("")
    host.enqueue_steer("   ")
    host.enqueue_steer("\n\t ")
    host.enqueue_steer(None)  # type: ignore[arg-type]
    assert host.drain_steer() == []
    assert list(host._steer_queue) == []
    assert host._pending_steer_drop_notice is None

    host.enqueue_steer("  keep me  ")
    assert host.drain_steer() == ["keep me"]
