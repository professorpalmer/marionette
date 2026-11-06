from __future__ import annotations

"""The run_ipython kernel runs in a child process.

A cell used to run in a backend thread. It changed the cwd and stdout of the
whole backend, and a cell past its time limit held the kernel for ever.
"""

import os
import sys

import pytest

from harness.ipython_kernel import PersistentPythonKernel

posix_only = pytest.mark.skipif(os.name == "nt", reason="Windows has no SIGINT to a child")


@pytest.fixture
def kernel(tmp_path):
    k = PersistentPythonKernel(str(tmp_path))
    yield k
    k.close()


@posix_only
def test_a_runaway_cell_is_interrupted_and_variables_stay(kernel):
    assert kernel.execute("keep = 41").ok
    stopped = kernel.execute("while True: pass", timeout=0.5)
    assert stopped.timed_out and "Variables are kept" in stopped.error
    after = kernel.execute("keep + 1", timeout=10)
    assert after.ok and after.output == "42"


def test_a_cell_does_not_change_the_backend_cwd_or_stdout(kernel, tmp_path):
    cwd_before, stdout_before = os.getcwd(), sys.stdout
    kernel.execute("while True: pass", timeout=0.5)
    assert os.getcwd() == cwd_before
    assert sys.stdout is stdout_before
    here = kernel.execute("__import__('os').getcwd()", timeout=10)
    assert here.ok and os.path.realpath(eval(here.output)) == os.path.realpath(str(tmp_path))


def test_a_cell_that_ignores_the_interrupt_restarts_the_kernel(kernel):
    kernel.execute("keep = 1")
    code = "import time\ntry:\n    time.sleep(30)\nexcept KeyboardInterrupt:\n    while True: pass"
    stopped = kernel.execute(code, timeout=0.5)
    assert stopped.timed_out and "restarted" in stopped.error
    fresh = kernel.execute("'keep' in globals()", timeout=10)
    assert fresh.ok and fresh.output == "False"


def test_the_kernel_does_not_see_provider_keys(kernel, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-not-for-kernel")
    seen = kernel.execute("__import__('os').environ.get('OPENROUTER_API_KEY')", timeout=10)
    assert seen.ok and "sk-or-test" not in seen.output


def test_input_and_print_do_not_touch_the_kernel_channel(kernel):
    asked = kernel.execute("print('before'); input()", timeout=10)
    assert not asked.ok and "EOFError" in asked.error
    assert kernel.execute("print('still here')", timeout=10).output == "still here"


def test_close_ends_the_child(tmp_path):
    k = PersistentPythonKernel(str(tmp_path))
    assert k.execute("1").ok
    proc = k._child.proc
    k.close()
    assert proc.poll() is not None


def test_a_dropped_kernel_ends_its_child_without_close(tmp_path):
    # A session that drops its kernel must not leave the child alive. The
    # reader thread once held the child object, so its finalizer never ran.
    import gc

    k = PersistentPythonKernel(str(tmp_path))
    assert k.execute("1", timeout=10).ok
    proc = k._child.proc
    del k
    gc.collect()
    proc.wait(timeout=10)
    assert proc.poll() is not None
