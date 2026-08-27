"""Session-local persistent REPL (L2) kernel bindings."""
from __future__ import annotations

from types import SimpleNamespace

from harness.pilot import PilotAction
from harness.session_repl import (
    OFFLOAD_OUTPUT_CHARS,
    bind_text,
    list_bindings,
    offload_ipython_output,
    serialize_bindings,
)
from harness.tool_dispatch import ToolDispatchMixin


def test_bind_and_show_kernel(tmp_path):
    session = SimpleNamespace(
        state_dir=str(tmp_path),
        config=SimpleNamespace(repo=str(tmp_path), state_dir=str(tmp_path)),
        harness_session_id="sess-repl",
        _ipython_kernel=None,
    )
    bind_text(session, "eval_log", "line1\nline2\n")
    rows = list_bindings(session)
    assert len(rows) == 1
    assert rows[0]["name"] == "eval_log"
    text = serialize_bindings(session, ["eval_log"])
    assert "line1" in text
    assert "eval_log" in text


def test_run_ipython_offloads_large_output(tmp_path):
    session = SimpleNamespace(
        state_dir=str(tmp_path),
        config=SimpleNamespace(repo=str(tmp_path), state_dir=str(tmp_path)),
        harness_session_id="sess-repl",
        _ipython_kernel=None,
    )
    big = "x" * (OFFLOAD_OUTPUT_CHARS + 100)
    display, bound = offload_ipython_output(session, big)
    assert bound
    assert "show_kernel" in display
    assert len(display) < len(big)


def test_show_kernel_tool_dispatch(tmp_path):
    session = SimpleNamespace(
        state_dir=str(tmp_path),
        config=SimpleNamespace(repo=str(tmp_path), state_dir=str(tmp_path)),
        harness_session_id="sess-repl",
        _ipython_kernel=None,
    )
    bind_text(session, "probe", "42")
    act = PilotAction(kind="show_kernel", path="probe")
    ok, status, val = ToolDispatchMixin._do_show_kernel(session, act)
    assert ok is True
    assert "42" in val


def test_list_kernel_tool_dispatch(tmp_path):
    session = SimpleNamespace(
        state_dir=str(tmp_path),
        config=SimpleNamespace(repo=str(tmp_path), state_dir=str(tmp_path)),
        harness_session_id="sess-repl",
        _ipython_kernel=None,
    )
    bind_text(session, "a", "1")
    ok, status, val = ToolDispatchMixin._do_list_kernel(session, PilotAction(kind="list_kernel"))
    assert ok is True
    assert "a" in val
