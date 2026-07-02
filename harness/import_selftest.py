"""Diagnostic: reproduce (or prove absent) the concurrent first-import race that
broke run_parallel in the packaged app.

In the frozen (PyInstaller) build, harness.worker / harness.edit_engines /
puppetmaster.* are served from one shared, zlib-compressed PYZ archive. When
run_parallel fans provider workers onto a thread pool, each thread lazily
first-imports that chain; concurrent reads of the shared archive raced, surfacing
as "Error -3 while decompressing data: incorrect header check" and
"cannot import name 'WorkerResult' from 'harness.worker'".

This runs that exact pattern deterministically: evict the modules, then import
them from N threads released simultaneously, for several iterations. With
--prewarm it first warms them single-threaded (the fix) and must come back clean.

Exit code 0 = no import failures, 1 = at least one failure observed.
"""
from __future__ import annotations

import sys
import threading

_CHAIN = [
    "harness.worker",
    "harness.edit_engines",
    "puppetmaster.orchestrator",
    "puppetmaster.store_factory",
    "puppetmaster.workers",
]


def _evict(deep: bool = False) -> None:
    for name in _CHAIN:
        sys.modules.pop(name, None)
    if deep:
        # Evict the ENTIRE puppetmaster subtree + harness leaf modules so the
        # concurrent re-import recreates a cold first-import storm (what a fresh
        # run_parallel process actually does), not just a 5-leaf reimport whose
        # dependencies are still cached.
        for name in list(sys.modules):
            if name == "puppetmaster" or name.startswith("puppetmaster."):
                sys.modules.pop(name, None)
            elif name in ("harness.worker", "harness.edit_engines"):
                sys.modules.pop(name, None)


def _import_all(errors: list) -> None:
    try:
        import harness.worker as w
        import harness.edit_engines  # noqa: F401
        # touch the exact symbol that failed in the field
        _ = w.WorkerResult
        for mod in ("puppetmaster.orchestrator", "puppetmaster.store_factory",
                    "puppetmaster.workers"):
            __import__(mod)
    except BaseException as exc:  # noqa: BLE001 - we want to capture zlib/ImportError alike
        errors.append(f"{type(exc).__name__}: {exc}")


def run_import_selftest(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    prewarm = "--prewarm" in argv
    deep = "--deep" in argv
    threads = 8
    iterations = 25
    for i, a in enumerate(argv):
        if a == "--threads" and i + 1 < len(argv):
            threads = int(argv[i + 1])
        if a == "--iterations" and i + 1 < len(argv):
            iterations = int(argv[i + 1])

    frozen = bool(getattr(sys, "frozen", False))
    print(f"import-selftest: frozen={frozen} threads={threads} "
          f"iterations={iterations} prewarm={prewarm} deep={deep}")

    all_errors: list = []
    for it in range(iterations):
        _evict(deep=deep)
        if prewarm:
            # prewarm carries a process-global "warmed" flag; clear it BEFORE
            # warming so each iteration genuinely re-imports (single-threaded)
            # from the archive after the eviction above.
            import harness.conversation as conv
            conv._WORKER_IMPORTS_WARMED = False
            conv._prewarm_worker_imports()

        errors: list = []
        barrier = threading.Barrier(threads)

        def worker():
            barrier.wait()  # release all threads at once -> maximize collision
            _import_all(errors)

        ts = [threading.Thread(target=worker) for _ in range(threads)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        if errors:
            all_errors.extend(errors)

    if all_errors:
        print(f"FAIL: {len(all_errors)} import failure(s) across {iterations} iteration(s):")
        for e in sorted(set(all_errors)):
            print(f"  - {e}")
        return 1
    print(f"PASS: no import failures across {iterations} iteration(s) x {threads} threads.")
    return 0
