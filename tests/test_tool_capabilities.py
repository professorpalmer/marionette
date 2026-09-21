from __future__ import annotations

from harness.send_loop_phases import PLAN_SKIP_KINDS
from harness.send_loop_phases import READ_ONLY_KINDS as PHASE_READ_ONLY_KINDS
from harness.tool_capabilities import (
    MUTATING_KINDS,
    READ_ONLY_KINDS,
    SideEffectScope,
    capability_for,
    is_workspace_mutating,
    plan_mode_blocks,
)


def test_declared_mutating_kinds_match_plan_skip_catalog():
    assert MUTATING_KINDS == PLAN_SKIP_KINDS
    assert READ_ONLY_KINDS == PHASE_READ_ONLY_KINDS


def test_read_only_kinds_are_not_plan_blocked():
    for kind in READ_ONLY_KINDS:
        cap = capability_for(kind)
        assert cap.scope is SideEffectScope.READ
        assert cap.read_only is True
        assert is_workspace_mutating(cap) is False
        assert plan_mode_blocks(kind) is False


def test_plan_skip_kinds_are_mutating():
    for kind in PLAN_SKIP_KINDS:
        assert plan_mode_blocks(kind) is True


def test_undeclared_kind_is_mutating():
    cap = capability_for("frobnicate")
    assert cap.scope is SideEffectScope.UNDECLARED
    assert cap.destructive is True
    assert plan_mode_blocks("frobnicate") is True
    assert plan_mode_blocks("") is True


def test_unknown_browser_prefix_is_network_mutating():
    cap = capability_for("browser_hover")
    assert cap.scope is SideEffectScope.NETWORK
    assert plan_mode_blocks("browser_hover") is True
