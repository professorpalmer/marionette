"""Persistent run_ipython kernel — hermetic tests (stdlib fallback, no IPython required)."""
from __future__ import annotations

import tempfile

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.ipython_kernel import PersistentPythonKernel, get_or_create_kernel
from harness.pilot import PilotAction, build_tools_schema, from_wire
from harness.send_loop_phases import PLAN_SKIP_KINDS
from harness.tool_dispatch import ToolDispatchMixin


def test_run_ipython_in_tools_schema():
    names = {
        e["function"]["name"]
        for e in build_tools_schema()
        if e.get("type") == "function" and "function" in e
    }
    assert "run_ipython" in names
    assert "run_command" in names


def test_from_wire_run_ipython_code_alias():
    act = from_wire("run_ipython", {"code": "x = 1"})
    assert act.kind == "run_ipython"
    assert act.content == "x = 1"


def test_run_ipython_requires_code():
    try:
        PilotAction(kind="run_ipython", content="").validate()
        assert False, "expected PilotError"
    except Exception as exc:
        assert "code" in str(exc).lower() or "content" in str(exc).lower()


def test_plan_mode_skips_run_ipython():
    assert "run_ipython" in PLAN_SKIP_KINDS


def test_kernel_state_persists_across_executions(tmp_path):
    kernel = PersistentPythonKernel(str(tmp_path))
    r1 = kernel.execute("a = 41")
    assert r1.ok, r1.error
    r2 = kernel.execute("a + 1")
    assert r2.ok, r2.error
    assert "42" in r2.output
    kernel.close()


def test_kernel_print_and_error(tmp_path):
    kernel = PersistentPythonKernel(str(tmp_path))
    r = kernel.execute("print('hi')")
    assert r.ok
    assert "hi" in r.output
    bad = kernel.execute("raise ValueError('boom')")
    assert not bad.ok
    assert "boom" in (bad.error + bad.output)
    kernel.close()


def test_session_kernel_shared(tmp_path):
    cfg = HarnessConfig(
        driver="stub-oracle-v2",
        state_dir=tempfile.mkdtemp(prefix="ipy-"),
        repo=str(tmp_path),
    )
    session = ConversationalSession(cfg)
    k1 = get_or_create_kernel(session)
    k1.execute("shared = 'yes'")
    k2 = get_or_create_kernel(session)
    assert k1 is k2
    r = k2.execute("shared")
    assert r.ok and "yes" in r.output


def test_do_run_ipython_dispatch(tmp_path):
    cfg = HarnessConfig(
        driver="stub-oracle-v2",
        state_dir=tempfile.mkdtemp(prefix="ipy-"),
        repo=str(tmp_path),
    )
    session = ConversationalSession(cfg)
    # Bind mixin method explicitly (MRO already includes it on ConversationalSession).
    act = PilotAction(kind="run_ipython", content="2 + 2")
    ok, status, val = ToolDispatchMixin._do_run_ipython(session, act)
    assert ok and status == "success"
    assert isinstance(val, dict)
    assert "4" in val.get("output", "")
