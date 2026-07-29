from __future__ import annotations

"""The lsp tool contract must match its schema, handler, and dispatch coverage.

The validator, the advertised schema enum, and ``get_lsp_report`` are three
separate lists of modes. When they drift, the pilot is punished for calling a
mode the tool itself advertises.
"""

from types import SimpleNamespace

import pytest

from harness.pilot import PilotAction, PilotError, build_tools_schema


def _lsp_schema_modes():
    for item in build_tools_schema(None):
        fn = item.get("function") or {}
        if fn.get("name") == "lsp":
            return set(fn["parameters"]["properties"]["mode"]["enum"])
    raise AssertionError("lsp tool missing from schema")


def test_validate_accepts_references_with_symbol():
    act = PilotAction(
        kind="lsp", arguments={"mode": "references", "symbol": "sync_agentic_registry"},
    ).validate()
    assert act.arguments["mode"] == "references"


def test_validate_rejects_references_without_symbol():
    with pytest.raises(PilotError, match="non-empty symbol"):
        PilotAction(kind="lsp", arguments={"mode": "references"}).validate()
    with pytest.raises(PilotError, match="non-empty symbol"):
        PilotAction(
            kind="lsp", arguments={"mode": "references", "symbol": "   "},
        ).validate()


def test_validate_rejects_unknown_mode():
    with pytest.raises(PilotError, match="mode must be"):
        PilotAction(kind="lsp", arguments={"mode": "rename"}).validate()


@pytest.mark.parametrize("mode", sorted(_lsp_schema_modes()))
def test_every_advertised_mode_passes_validation(mode):
    args = {"mode": mode}
    if mode == "references":
        args["symbol"] = "PilotAction"
    PilotAction(kind="lsp", arguments=args).validate()


def test_missing_toolchain_reports_truthfully_not_as_an_error(tmp_path):
    """An absent pyright/tsc is an optional capability, not a tool failure."""
    from harness.lsp_code_intelligence import LspToolAvailability, get_lsp_report

    absent = LspToolAvailability(
        python_pyright=None,
        python_pyright_langserver=None,
        typescript_tsc=None,
        typescript_tsserver=None,
        typescript_typescript_language_server=None,
    )

    status = get_lsp_report(
        language="auto", mode="status", root=str(tmp_path), tools=absent,
    )
    assert "not found" in status.lower()

    diagnostics = get_lsp_report(
        language="python", mode="diagnostics", root=str(tmp_path), tools=absent,
    )
    assert "no tool available" in diagnostics.lower()


def test_references_mode_without_symbol_is_reported_not_raised(tmp_path):
    from harness.lsp_code_intelligence import get_lsp_report

    out = get_lsp_report(
        language="auto", mode="references", root=str(tmp_path), symbol="",
    )
    assert "non-empty symbol" in out


def test_read_only_kinds_reach_prefetch_and_result_assembly():
    """Exhaustive drift guard: no READ_ONLY_KINDS member may fall through.

    ``run_prefetch`` returning "Unknown prefetch kind" or
    ``dispatch_readonly_action`` returning "Unhandled read-only action kind"
    means the catalog grew a tool whose plumbing was never wired.
    """
    from harness.send_loop_phases import READ_ONLY_KINDS, dispatch_readonly_action, run_prefetch

    # A shaped failure result exercises every kind's error branch without
    # coupling the guard to each kind's success payload shape.
    probe = (False, "exception", "probe")

    class _Session:
        def __getattr__(self, name):
            if not name.startswith("_do_"):
                raise AttributeError(name)
            return lambda act: probe

        def _append_action_result(self, *_a, **_k):
            return None

    session = _Session()

    for kind in sorted(READ_ONLY_KINDS):
        act = PilotAction(kind=kind, arguments={})

        idx, res = run_prefetch(session, (0, act))
        assert idx == 0
        assert res == probe, f"{kind} never reached a prefetch handler"

        events = list(dispatch_readonly_action(
            session, act, 0, f"a-{kind}", {0: probe}, True,
        ))
        errors = [e.data.get("error") or "" for e in events if e.kind == "action_result"]
        assert errors, f"{kind} produced no action_result"
        assert not any("Unhandled read-only action kind" in err for err in errors), kind
