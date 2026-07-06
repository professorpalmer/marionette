from __future__ import annotations

import json
import subprocess

import pytest

import harness.lsp_code_intelligence as lsp


def test_missing_tools_graceful_output_python_diagnostics(monkeypatch, tmp_path):
    monkeypatch.setattr(lsp.shutil, "which", lambda _: None)

    report = lsp.get_lsp_report(
        language="python",
        mode="diagnostics",
        root=str(tmp_path),
        timeout_ms=1000,
    )

    assert "Python diagnostics" in report
    assert "no tool available" in report.lower()
    assert "pyright" in report.lower()


def test_missing_tools_graceful_output_typescript_diagnostics(monkeypatch, tmp_path):
    monkeypatch.setattr(lsp.shutil, "which", lambda _: None)

    report = lsp.get_lsp_report(
        language="typescript",
        mode="diagnostics",
        root=str(tmp_path),
        timeout_ms=1000,
    )

    assert "TypeScript diagnostics" in report
    # Should explain that `tsc`-like tools were not found.
    assert "tsc" in report.lower()


def test_parse_tsc_diagnostics():
    output = (
        "src/app.ts(1,2): error TS1005: ';' expected\n"
        "src/util.ts(3,4): warning TS6133: 'x' is declared but its value is never read.\n"
        "error TS9999: Something went wrong\n"
    )
    diags = lsp.parse_tsc_diagnostics(output)
    assert len(diags) == 3
    assert diags[0].file == "src/app.ts"
    assert diags[0].line == 1
    assert diags[0].column == 2
    assert diags[0].severity == "error"
    assert diags[0].code == "1005"
    assert "';'" in diags[0].message
    assert diags[1].severity == "warning"
    assert diags[1].code == "6133"
    assert diags[2].file is None
    assert diags[2].code == "9999"


def test_parse_pyright_diagnostics():
    payload = {
        "generalDiagnostics": [
            {
                "file": "main.py",
                "severity": "error",
                "message": "Type mismatch",
                "range": {"start": {"line": 0, "character": 1}},
            }
        ]
    }
    diags = lsp.parse_pyright_diagnostics(json.dumps(payload))
    assert len(diags) == 1
    d = diags[0]
    assert d.file == "main.py"
    assert d.line == 1  # 0-based -> 1-based
    assert d.column == 2  # 0-based -> 1-based
    assert d.severity == "error"
    assert "Type mismatch" in d.message


def test_diagnostics_timeout(monkeypatch, tmp_path):
    # Ensure TypeScript path uses tsc, then force timeout.
    monkeypatch.setattr(lsp.shutil, "which", lambda name: "/usr/bin/tsc" if name == "tsc" else None)

    def fake_run(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd=_args[0] if _args else "tsc", timeout=1)

    monkeypatch.setattr(lsp.subprocess, "run", fake_run)

    report = lsp.get_lsp_report(
        language="typescript",
        mode="diagnostics",
        root=str(tmp_path),
        timeout_ms=50,
    )

    assert "TypeScript diagnostics" in report
    assert "Timed out" in report


def test_windows_safe_command_handling_uses_args_list(monkeypatch, tmp_path):
    monkeypatch.setattr(lsp.shutil, "which", lambda name: "/usr/bin/tsc" if name == "tsc" else None)

    called = {}

    def fake_run(cmd, *, cwd, stdout, stderr, text, timeout, shell, check):
        called["cmd_type"] = type(cmd)
        called["cmd"] = cmd
        called["shell"] = shell

        class P:
            returncode = 0
            stdout = ""

        return P()

    monkeypatch.setattr(lsp.subprocess, "run", fake_run)

    _ = lsp.get_lsp_report(
        language="typescript",
        mode="diagnostics",
        root=str(tmp_path),
        timeout_ms=500,
    )

    assert called["cmd_type"] is list
    assert called["shell"] is False
    assert called["cmd"][0].endswith("tsc")


def test_references_text_scan_finds_symbol(tmp_path):
    src = tmp_path / "module.py"
    src.write_text("def unique_ref_target():\n    return unique_ref_target()\n", encoding="utf-8")

    report = lsp.get_symbol_references("unique_ref_target", str(tmp_path))

    assert "References for `unique_ref_target`" in report
    assert "module.py" in report
    assert "Text scan:" in report


def test_references_graceful_when_no_matches(monkeypatch, tmp_path):
    monkeypatch.setattr(lsp, "_codegraph_references", lambda *a, **k: (False, "CodeGraph unavailable"))

    report = lsp.get_symbol_references("no_such_symbol_xyz", str(tmp_path))

    assert "References for `no_such_symbol_xyz`" in report
    assert "Text scan: no matches found." in report


def test_lsp_schema_includes_references_and_symbol():
    from harness.pilot import build_tools_schema

    schema = build_tools_schema()
    lsp_entry = next(t for t in schema if t["function"]["name"] == "lsp")
    props = lsp_entry["function"]["parameters"]["properties"]
    assert "references" in props["mode"]["enum"]
    assert "symbol" in props

