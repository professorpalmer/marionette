from __future__ import annotations

import threading

from harness.compaction_mixin import (
    REASON_BELOW_TRIGGER,
    REASON_CACHE_DEFERRED,
    REASON_OK,
    REASON_RESIDUAL_OFF,
    REASON_THRASH_COOLDOWN,
    SKIP_BELOW_THRESHOLD,
    SKIP_DEFERRED_PLUGIN,
    SKIP_DISABLED,
    CompactionContextMixin,
    compaction_skip_reason,
)


def test_skip_taxonomy_maps_dest_reasons():
    assert compaction_skip_reason(REASON_RESIDUAL_OFF) == SKIP_DISABLED
    assert compaction_skip_reason(REASON_BELOW_TRIGGER) == SKIP_BELOW_THRESHOLD
    assert compaction_skip_reason(REASON_CACHE_DEFERRED) == SKIP_DEFERRED_PLUGIN
    assert compaction_skip_reason(REASON_THRASH_COOLDOWN) == SKIP_DEFERRED_PLUGIN
    assert compaction_skip_reason(REASON_OK) is None


def test_attempt_records_skip_reason():
    mixin = CompactionContextMixin()
    mixin._set_compaction_attempt(REASON_RESIDUAL_OFF)
    assert mixin._last_compaction_attempt["reason"] == REASON_RESIDUAL_OFF
    assert mixin._last_compaction_attempt["skip_reason"] == SKIP_DISABLED


def test_checkpoint_lock_serializes():
    mixin = CompactionContextMixin()
    lock = mixin._compaction_checkpoint_lock()
    held = []

    def other():
        if lock.acquire(timeout=0.05):
            held.append("got")
            lock.release()
        else:
            held.append("blocked")

    lock.acquire()
    try:
        thread = threading.Thread(target=other)
        thread.start()
        thread.join(1)
    finally:
        lock.release()
    assert held == ["blocked"]
