from __future__ import annotations

"""The scheduler tick wait does not stretch when the wall clock goes back."""

import threading
from unittest.mock import MagicMock, patch

from harness.scheduler import SchedulerDaemon


def test_tick_wait_ignores_a_backward_wall_clock_jump():
    daemon = SchedulerDaemon(MagicMock())
    wall = iter([10_000.0] + [9_000.0] * 10_000)
    done = threading.Event()

    def wait():
        daemon._interruptible_wait(1)
        done.set()

    with patch("harness.scheduler.time.time", side_effect=lambda: next(wall)):
        worker = threading.Thread(target=wait, daemon=True)
        worker.start()
        finished = done.wait(5)
        daemon.stop()
    assert finished, "a 1 s tick waited for the 1000 s clock jump"
