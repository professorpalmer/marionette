from __future__ import annotations

import threading
from types import SimpleNamespace

from harness.tool_timeout import (
    TOOL_TIMEOUT,
    declared_timeout_ms,
    invoke_do,
    run_with_tool_deadline,
)


def _session():
    return SimpleNamespace(_cancel=threading.Event(), _interrupt_requested=False)


def test_inner_timeout_kinds_declare_nothing():
    for kind in (
        "run_command",
        "run_ipython",
        "run_swarm",
        "run_implement",
        "run_parallel",
        "route_task",
    ):
        assert declared_timeout_ms(kind) is None


def test_network_tools_declare_a_budget():
    for kind in (
        "web_fetch",
        "web_search",
        "read_pdf",
        "search_codegraph",
        "search_files",
        "call_mcp",
        "query_wiki",
    ):
        budget = declared_timeout_ms(kind)
        assert budget is not None
        assert budget > 0


def test_invoke_do_maps_only_this_wrapper_expiry(monkeypatch):
    monkeypatch.setenv("HARNESS_TOOL_TIMEOUT_WEB_SEARCH_MS", "40")
    session = _session()
    act = SimpleNamespace(kind="web_search")

    def _slow():
        session._cancel.wait(2.0)
        return (True, "ok", "late")

    triple = invoke_do(session, act, _slow)
    assert triple[0] is False
    assert triple[1] == TOOL_TIMEOUT
    assert "40ms" in triple[2]
    assert not session._cancel.is_set()


def test_user_stop_is_not_remapped_to_tool_timeout():
    session = _session()
    session._interrupt_requested = True
    session._cancel.set()

    def _already_cancelled():
        return (False, "cancelled", "user stop")

    result, timed_out = run_with_tool_deadline(
        session, "web_search", _already_cancelled, timeout_ms=20,
    )
    assert timed_out is None
    assert result[1] == "cancelled"


def test_inner_command_timeout_status_stays_honest():
    session = _session()

    def _command():
        return (False, "timeout", "command timed out")

    result, timed_out = run_with_tool_deadline(session, "run_command", _command)
    assert timed_out is None
    assert result == (False, "timeout", "command timed out")


def test_no_budget_does_not_arm_a_timer():
    session = _session()
    called = []

    def _fn():
        called.append(True)
        return (True, "ok", "x")

    result, timed_out = run_with_tool_deadline(session, "read_file", _fn)
    assert timed_out is None
    assert result[0] is True
    assert called == [True]
