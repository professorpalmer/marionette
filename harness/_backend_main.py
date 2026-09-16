import sys
import threading


class ManagedScheduler:
    """One app-owned daemon, sharing durable claims with external daemons."""

    def __init__(self, store_factory=None, daemon_factory=None):
        from harness.schedule_store import ScheduleStore
        from harness.scheduler import SchedulerDaemon
        self.store_factory = store_factory or ScheduleStore
        self.daemon_factory = daemon_factory or SchedulerDaemon
        self._lock = threading.Lock()
        self._thread = None
        self._daemon = None

    def start(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._daemon = self.daemon_factory(self.store_factory())
            self._thread = threading.Thread(target=self._daemon.serve,
                                            kwargs={"tick_seconds": 1},
                                            name="app-scheduler", daemon=True)
            self._thread.start()
            return True

    def stop(self):
        with self._lock:
            if self._daemon is None:
                return True
            self._daemon.stop()
            thread = self._thread
        thread.join(timeout=2)
        # A blocked execution keeps its claim and store until its own finally.
        return not thread.is_alive()

if __name__ == "__main__":
    from harness.cli import main
    sys.exit(main())
