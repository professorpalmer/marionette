"""Guards the fix for the packaged-app swarm failures.

run_parallel dispatches provider workers onto a ThreadPoolExecutor; each worker
lazily first-imports harness.worker -> edit_engines -> a large slice of
puppetmaster.*. In the frozen (PyInstaller) app, concurrent first-time imports
across that pool produced the paired failures "incorrect header check" and
"cannot import name 'WorkerResult'". _prewarm_worker_imports() warms the full
worker-reachable module graph single-threaded, so worker threads only ever hit
the sys.modules cache and never perform a first-import (nothing left to race).
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


def test_prewarm_retries_when_essential_import_fails(monkeypatch):
    import importlib
    import harness.conversation as conv

    conv._WORKER_IMPORTS_WARMED = False
    real = importlib.import_module
    notes = []

    def boom(name, package=None):
        if name == "harness.worker":
            raise ImportError("simulated essential miss")
        return real(name, package)

    def capture(where, exc=None, msg=""):
        notes.append((where, repr(exc) if exc is not None else None, msg))

    monkeypatch.setattr(importlib, "import_module", boom)
    monkeypatch.setattr(conv, "_diag_note", capture)
    conv._prewarm_worker_imports()
    assert conv._WORKER_IMPORTS_WARMED is False
    assert any(where == "prewarm.essential_import" for where, _, _ in notes)
    assert any(where == "prewarm.essentials_incomplete" for where, _, _ in notes)

    monkeypatch.setattr(importlib, "import_module", real)
    conv._prewarm_worker_imports()
    assert conv._WORKER_IMPORTS_WARMED is True
    assert "harness.worker" in sys.modules


def test_prewarm_aggregates_broad_module_failures(monkeypatch):
    import importlib
    import pkgutil
    import harness.conversation as conv

    conv._WORKER_IMPORTS_WARMED = False
    real = importlib.import_module
    notes = []
    failed_names = [f"harness.fake_mod_{i}" for i in range(12)]

    def capture(where, exc=None, msg=""):
        notes.append((where, repr(exc) if exc is not None else None, msg))

    class FakePkg:
        __path__ = ["unused"]

    def fake_walk(path, prefix=""):
        for name in failed_names:
            yield type("Info", (), {"name": name})()

    def import_mix(name, package=None):
        if name in ("harness.worker", "harness.edit_engines"):
            return real(name, package)
        if name == "harness":
            return FakePkg()
        if name in ("pmharness", "puppetmaster"):
            raise ImportError("skip other roots")
        if name in failed_names or name.startswith("harness.fake_mod_"):
            raise ImportError(f"boom {name}")
        return real(name, package)

    monkeypatch.setattr(importlib, "import_module", import_mix)
    monkeypatch.setattr(pkgutil, "walk_packages", fake_walk)
    monkeypatch.setattr(conv, "_diag_note", capture)
    conv._prewarm_worker_imports()

    module_notes = [n for n in notes if n[0] == "prewarm.module_import"]
    assert len(module_notes) == 1
    msg = module_notes[0][2]
    assert "12 module(s) failed" in msg
    assert "harness.fake_mod_0" in msg
    assert "+7 more" in msg
    assert not any(n[0] == "prewarm.essential_import" for n in notes)
    assert conv._WORKER_IMPORTS_WARMED is True


def test_prewarm_package_and_walk_failures_stay_observable(monkeypatch):
    import importlib
    import harness.conversation as conv

    conv._WORKER_IMPORTS_WARMED = False
    real = importlib.import_module
    notes = []

    def capture(where, exc=None, msg=""):
        notes.append((where, repr(exc) if exc is not None else None, msg))

    def boom_pkg(name, package=None):
        if name in ("harness.worker", "harness.edit_engines"):
            return real(name, package)
        if name == "pmharness":
            raise ImportError("pkg missing")
        return real(name, package)

    monkeypatch.setattr(importlib, "import_module", boom_pkg)
    monkeypatch.setattr(conv, "_diag_note", capture)
    conv._prewarm_worker_imports()
    assert any(
        where == "prewarm.package_import" and "pmharness" in (msg or "")
        for where, _, msg in notes
    )
