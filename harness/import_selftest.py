"""Diagnostic: reproduce (or prove absent) the concurrent first-import failure
that broke run_parallel in the packaged app.

Root cause (confirmed by inspection, after refuting the shared-archive theory):
harness.worker imported harness.conversation at module top level, while
conversation (via the worker prewarm / lazy dispatch) imports back into worker --
a circular dependency. In the unfrozen repo this is masked because everything is
already imported by the time run_parallel runs. In run_parallel's thread pool,
two threads first-import the two ends of the cycle at once (worker on one,
conversation on another). Their per-module import locks cross:
thread A holds worker + wants conversation, thread B holds conversation + wants
worker. To avoid deadlock, CPython hands one thread the OTHER, half-initialized
module instead of blocking -- so a thread sees a worker module whose WorkerResult
(defined after the offending import) does not exist yet:
    cannot import name 'WorkerResult' from 'harness.worker'
and the sibling's cascading import failure surfaced as the paired
    Error -3 while decompressing data: incorrect header check.

The fix removes worker's top-level import of conversation (ConvEvent -> TYPE_CHECKING,
ConversationalSession -> lazy at its call site), so the cycle -- and the deadlock-
avoidance partial-module hand-off -- cannot occur.

This runs that pattern deterministically: evict the modules, then from N threads
released simultaneously, race the TWO ENDS of the cycle (half import worker-first,
half import conversation-first) so their locks actually cross. Different modules
on different threads is the trigger; importing the same module on every thread
would just serialize on one lock and hide the bug. With --prewarm it first warms
single-threaded and must come back clean.

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
    # Both ends of the cycle must be cold for the lock cross to happen.
    for name in ("harness.worker", "harness.edit_engines", "harness.conversation"):
        sys.modules.pop(name, None)
    if deep:
        # Evict the ENTIRE puppetmaster subtree + harness leaf modules so the
        # concurrent re-import recreates a cold first-import storm (what a fresh
        # run_parallel process actually does), not just a leaf reimport whose
        # dependencies are still cached.
        for name in list(sys.modules):
            if name == "puppetmaster" or name.startswith("puppetmaster."):
                sys.modules.pop(name, None)
            elif name in ("harness.worker", "harness.edit_engines",
                          "harness.conversation"):
                sys.modules.pop(name, None)


def _import_worker_first(errors: list) -> None:
    try:
        from harness.worker import WorkerResult  # the exact symbol that failed
        _ = WorkerResult
        import harness.edit_engines  # noqa: F401
    except BaseException as exc:  # noqa: BLE001 - capture zlib/ImportError alike
        errors.append(f"{type(exc).__name__}: {exc}")


def _import_conversation_first(errors: list) -> None:
    try:
        import harness.conversation  # noqa: F401 - the other end of the cycle
        from harness.worker import WorkerResult
        _ = WorkerResult
    except BaseException as exc:  # noqa: BLE001
        errors.append(f"{type(exc).__name__}: {exc}")


def run_import_audit() -> int:
    """Single-threaded: import EVERY module in the worker-reachable graph and
    report each one that fails, with its exception. This isolates a PACKAGING
    fault (e.g. a module whose PYZ entry fails to zlib-decompress -> "incorrect
    header check") from a concurrency fault: a packaging fault fails here too,
    deterministically and by name; a pure race does not.

    Exit 0 = every module imported cleanly, 1 = at least one failed.
    """
    import importlib
    import pkgutil

    frozen = bool(getattr(sys, "frozen", False))
    print(f"import-audit: frozen={frozen} -- importing full worker graph single-threaded")
    failures: list = []
    seen = 0
    for pkg_name in ("harness", "pmharness", "puppetmaster"):
        try:
            pkg = importlib.import_module(pkg_name)
        except BaseException as exc:  # noqa: BLE001
            failures.append((pkg_name, f"{type(exc).__name__}: {exc}"))
            continue
        pkg_path = getattr(pkg, "__path__", None)
        if not pkg_path:
            continue
        for info in pkgutil.walk_packages(pkg_path, prefix=pkg_name + "."):
            name = info.name
            if name.endswith("__main__") or ".tests" in name or ".test_" in name:
                continue
            seen += 1
            try:
                importlib.import_module(name)
            except BaseException as exc:  # noqa: BLE001 - capture zlib errors too
                failures.append((name, f"{type(exc).__name__}: {exc}"))

    if failures:
        print(f"FAIL: {len(failures)} module(s) failed to import ({seen} attempted):")
        for name, err in sorted(failures):
            print(f"  - {name}: {err}")
        return 1
    print(f"PASS: all {seen} modules imported cleanly.")
    return 0


def run_import_selftest(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--audit" in argv:
        return run_import_audit()
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

        def worker(idx):
            barrier.wait()  # release all threads at once -> maximize collision
            # Race the TWO ENDS of the cycle so the per-module import locks cross;
            # importing the same module on every thread would just serialize.
            if idx % 2 == 0:
                _import_worker_first(errors)
            else:
                _import_conversation_first(errors)

        ts = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
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
