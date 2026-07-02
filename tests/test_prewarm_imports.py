"""Guards the fix for the packaged-app swarm failures.

run_parallel dispatches provider workers onto a thread pool; each worker lazily
first-imports harness.worker -> edit_engines -> puppetmaster.*. In the frozen
(PyInstaller) app those come from one shared zlib PYZ archive, and concurrent
first-time imports raced its reader, producing "incorrect header check" and
"cannot import name 'WorkerResult'". _prewarm_worker_imports() warms them
single-threaded so worker threads only ever hit the sys.modules cache.
"""
import sys
import threading

from harness.conversation import _prewarm_worker_imports


def test_prewarm_populates_module_cache():
    _prewarm_worker_imports()
    assert "harness.worker" in sys.modules
    assert "harness.edit_engines" in sys.modules
    # the symbol the racing worker thread failed to import must be present
    assert hasattr(sys.modules["harness.worker"], "WorkerResult")


def test_prewarm_is_idempotent():
    # Multiple calls (per session / per dispatch) must be safe and cheap.
    _prewarm_worker_imports()
    _prewarm_worker_imports()
    assert "harness.worker" in sys.modules


def test_concurrent_worker_imports_after_prewarm_never_fail():
    """After warming, N threads importing the worker symbols concurrently (as
    run_parallel does) must all succeed -- they only touch the cache."""
    _prewarm_worker_imports()
    errors = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        try:
            from harness.edit_engines import run_edit_worker  # noqa: F401
            from harness.worker import WorkerResult  # noqa: F401
        except Exception as e:  # pragma: no cover - the failure we are guarding against
            errors.append(repr(e))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
