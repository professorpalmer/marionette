from __future__ import annotations

import json
import os
import subprocess

import pytest

import harness.lsp_code_intelligence as lsp


def _touch_tool(path: os.PathLike[str] | str) -> str:
    path = os.fspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("#!/usr/bin/env true\n")
    try:
        os.chmod(path, 0o755)
    except OSError:
        pass
    return path


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
    assert "PATH" in report
    assert "node_modules/.bin" in report
    assert ".venv" in report


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
    assert "PATH" in report
    assert "node_modules/.bin" in report


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

    def fake_run(cmd, *, cwd, stdout, stderr, text, timeout, shell, check, **kwargs):
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


def test_discover_nested_frontend_node_modules_tsc(monkeypatch, tmp_path):
    """Monorepo layout: tsc lives under webapp/node_modules/.bin, not PATH."""
    monkeypatch.setattr(lsp.shutil, "which", lambda _: None)
    frontend = tmp_path / "webapp"
    frontend.mkdir()
    (frontend / "package.json").write_text("{}", encoding="utf-8")
    tsc = _touch_tool(frontend / "node_modules" / ".bin" / "tsc")

    tools = lsp.discover_lsp_tools(root=str(tmp_path))
    assert tools.typescript_tsc == tsc
    assert tools.typescript_available


def test_discover_root_node_modules_tsc(monkeypatch, tmp_path):
    monkeypatch.setattr(lsp.shutil, "which", lambda _: None)
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    tsc = _touch_tool(tmp_path / "node_modules" / ".bin" / "tsc")

    tools = lsp.discover_lsp_tools(root=str(tmp_path))
    assert tools.typescript_tsc == tsc


def test_discover_windows_cmd_shim_hermetic(monkeypatch, tmp_path):
    """Cross-platform: npm's Windows .cmd shim is accepted even on POSIX."""
    monkeypatch.setattr(lsp.shutil, "which", lambda _: None)
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    tsc_cmd = _touch_tool(tmp_path / "node_modules" / ".bin" / "tsc.cmd")

    tools = lsp.discover_lsp_tools(root=str(tmp_path))
    assert tools.typescript_tsc == tsc_cmd


def test_discover_prefers_posix_shim_over_cmd(monkeypatch, tmp_path):
    monkeypatch.setattr(lsp.shutil, "which", lambda _: None)
    bin_dir = tmp_path / "node_modules" / ".bin"
    posix = _touch_tool(bin_dir / "tsc")
    _touch_tool(bin_dir / "tsc.cmd")

    tools = lsp.discover_lsp_tools(root=str(tmp_path))
    assert tools.typescript_tsc == posix


def test_discover_path_takes_precedence_over_workspace(monkeypatch, tmp_path):
    path_tsc = "/usr/bin/tsc-from-path"
    monkeypatch.setattr(
        lsp.shutil, "which", lambda name: path_tsc if name == "tsc" else None,
    )
    _touch_tool(tmp_path / "node_modules" / ".bin" / "tsc")

    tools = lsp.discover_lsp_tools(root=str(tmp_path))
    assert tools.typescript_tsc == path_tsc


def test_discover_scans_workspace_once(monkeypatch, tmp_path):
    """PATH misses for every tool must still walk the workspace only once."""
    monkeypatch.setattr(lsp.shutil, "which", lambda _: None)
    webapp = tmp_path / "webapp"
    webapp.mkdir()
    (webapp / "package.json").write_text("{}", encoding="utf-8")
    _touch_tool(webapp / "node_modules" / ".bin" / "tsc")

    real_walk = os.walk
    walk_calls: list[str] = []

    def counting_walk(top, *args, **kwargs):
        walk_calls.append(os.path.abspath(top))
        return real_walk(top, *args, **kwargs)

    monkeypatch.setattr(lsp.os, "walk", counting_walk)

    tools = lsp.discover_lsp_tools(root=str(tmp_path))
    assert tools.typescript_tsc is not None
    assert len(walk_calls) == 1
    assert walk_calls[0] == os.path.abspath(tmp_path)


def test_discover_nested_before_deeper_sibling(monkeypatch, tmp_path):
    """Shallower package roots are preferred; walk order is deterministic."""
    monkeypatch.setattr(lsp.shutil, "which", lambda _: None)
    webapp = tmp_path / "webapp"
    nested = tmp_path / "packages" / "ui"
    webapp.mkdir()
    nested.mkdir(parents=True)
    (webapp / "package.json").write_text("{}", encoding="utf-8")
    (nested / "package.json").write_text("{}", encoding="utf-8")
    shallow = _touch_tool(webapp / "node_modules" / ".bin" / "tsc")
    _touch_tool(nested / "node_modules" / ".bin" / "tsc")

    tools = lsp.discover_lsp_tools(root=str(tmp_path))
    assert tools.typescript_tsc == shallow


def test_discover_does_not_crawl_into_node_modules(monkeypatch, tmp_path):
    monkeypatch.setattr(lsp.shutil, "which", lambda _: None)
    # A decoy tsc buried inside a dependency tree must be ignored.
    decoy = (
        tmp_path
        / "node_modules"
        / "some-pkg"
        / "node_modules"
        / ".bin"
        / "tsc"
    )
    _touch_tool(decoy)

    tools = lsp.discover_lsp_tools(root=str(tmp_path))
    assert tools.typescript_tsc is None


def test_discover_pyright_from_venv_bin(monkeypatch, tmp_path):
    monkeypatch.setattr(lsp.shutil, "which", lambda _: None)
    pyright = _touch_tool(tmp_path / ".venv" / "bin" / "pyright")

    tools = lsp.discover_lsp_tools(root=str(tmp_path))
    assert tools.python_pyright == pyright
    assert tools.python_available


def test_discover_pyright_from_venv_scripts_windows_layout(monkeypatch, tmp_path):
    monkeypatch.setattr(lsp.shutil, "which", lambda _: None)
    pyright = _touch_tool(tmp_path / ".venv" / "Scripts" / "pyright.exe")

    tools = lsp.discover_lsp_tools(root=str(tmp_path))
    assert tools.python_pyright == pyright


def test_discover_pyright_from_node_modules(monkeypatch, tmp_path):
    monkeypatch.setattr(lsp.shutil, "which", lambda _: None)
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    pyright = _touch_tool(tmp_path / "node_modules" / ".bin" / "pyright")

    tools = lsp.discover_lsp_tools(root=str(tmp_path))
    assert tools.python_pyright == pyright


def test_discover_pyright_venv_before_node_modules(monkeypatch, tmp_path):
    monkeypatch.setattr(lsp.shutil, "which", lambda _: None)
    venv_tool = _touch_tool(tmp_path / ".venv" / "bin" / "pyright")
    _touch_tool(tmp_path / "node_modules" / ".bin" / "pyright")

    tools = lsp.discover_lsp_tools(root=str(tmp_path))
    assert tools.python_pyright == venv_tool


def test_status_mentions_checked_locations_when_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(lsp.shutil, "which", lambda _: None)
    status = lsp.get_lsp_status(language="auto", root=str(tmp_path))
    assert "PATH" in status
    assert "node_modules/.bin" in status
    assert ".venv" in status


def test_tool_argv_uses_cmd_exe_for_windows_shims(monkeypatch):
    monkeypatch.setattr(lsp.os, "name", "nt")
    argv = lsp._tool_argv(r"C:\proj\node_modules\.bin\tsc.cmd", "--noEmit")
    assert argv[:3] == ["cmd.exe", "/c", r"C:\proj\node_modules\.bin\tsc.cmd"]
    assert argv[3:] == ["--noEmit"]


def test_tool_argv_passthrough_on_posix(monkeypatch):
    monkeypatch.setattr(lsp.os, "name", "posix")
    argv = lsp._tool_argv("/proj/node_modules/.bin/tsc", "--noEmit")
    assert argv == ["/proj/node_modules/.bin/tsc", "--noEmit"]


def test_nested_tsc_uses_package_tsconfig_and_cwd(monkeypatch, tmp_path):
    """Repo root has no tsconfig; nested webapp tsc must run against webapp/."""
    monkeypatch.setattr(lsp.shutil, "which", lambda _: None)
    webapp = tmp_path / "webapp"
    webapp.mkdir()
    (webapp / "package.json").write_text("{}", encoding="utf-8")
    (webapp / "tsconfig.json").write_text("{}", encoding="utf-8")
    tsc = _touch_tool(webapp / "node_modules" / ".bin" / "tsc")

    called: dict = {}

    def fake_capture(cmd, *, cwd, timeout_s):
        called["cmd"] = list(cmd)
        called["cwd"] = cwd
        return 0, ""

    monkeypatch.setattr(lsp, "_run_command_capture", fake_capture)

    report = lsp.get_lsp_report(
        language="typescript",
        mode="diagnostics",
        root=str(tmp_path),
        timeout_ms=500,
    )

    assert "TypeScript diagnostics" in report
    assert called["cwd"] == str(webapp)
    assert called["cmd"][0] == tsc
    assert "--noEmit" in called["cmd"]
    p_idx = called["cmd"].index("-p")
    assert os.path.normpath(called["cmd"][p_idx + 1]) == os.path.normpath(
        webapp / "tsconfig.json"
    )


def test_path_tsc_uses_nested_webapp_tsconfig_and_cwd(monkeypatch, tmp_path):
    """PATH tsc still uses shallowest package tsconfig when root has none."""
    path_tsc = "/usr/bin/tsc-from-path"
    monkeypatch.setattr(
        lsp.shutil, "which", lambda name: path_tsc if name == "tsc" else None,
    )
    webapp = tmp_path / "webapp"
    webapp.mkdir()
    (webapp / "package.json").write_text("{}", encoding="utf-8")
    (webapp / "tsconfig.json").write_text("{}", encoding="utf-8")

    called: dict = {}

    def fake_capture(cmd, *, cwd, timeout_s):
        called["cmd"] = list(cmd)
        called["cwd"] = cwd
        return 0, ""

    monkeypatch.setattr(lsp, "_run_command_capture", fake_capture)

    report = lsp.get_lsp_report(
        language="typescript",
        mode="diagnostics",
        root=str(tmp_path),
        timeout_ms=500,
    )

    assert "TypeScript diagnostics" in report
    assert called["cwd"] == str(webapp)
    assert called["cmd"][0] == path_tsc
    assert "--noEmit" in called["cmd"]
    p_idx = called["cmd"].index("-p")
    assert os.path.normpath(called["cmd"][p_idx + 1]) == os.path.normpath(
        webapp / "tsconfig.json"
    )


def test_root_tsconfig_takes_precedence_over_nested_package(monkeypatch, tmp_path):
    """Explicit root tsconfig wins even when nested webapp also has one."""
    monkeypatch.setattr(lsp.shutil, "which", lambda _: None)
    (tmp_path / "tsconfig.json").write_text("{}", encoding="utf-8")
    webapp = tmp_path / "webapp"
    webapp.mkdir()
    (webapp / "package.json").write_text("{}", encoding="utf-8")
    (webapp / "tsconfig.json").write_text("{}", encoding="utf-8")
    tsc = _touch_tool(webapp / "node_modules" / ".bin" / "tsc")

    called: dict = {}

    def fake_capture(cmd, *, cwd, timeout_s):
        called["cmd"] = list(cmd)
        called["cwd"] = cwd
        return 0, ""

    monkeypatch.setattr(lsp, "_run_command_capture", fake_capture)

    _ = lsp.get_lsp_report(
        language="typescript",
        mode="diagnostics",
        root=str(tmp_path),
        timeout_ms=500,
    )

    assert called["cwd"] == str(tmp_path)
    p_idx = called["cmd"].index("-p")
    assert os.path.normpath(called["cmd"][p_idx + 1]) == os.path.normpath(
        tmp_path / "tsconfig.json"
    )
    assert called["cmd"][0] == tsc


def test_nested_windows_cmd_tsc_project_argv_safe(monkeypatch, tmp_path):
    """Windows .cmd shim keeps cmd.exe /c argv while using nested project."""
    monkeypatch.setattr(lsp.shutil, "which", lambda _: None)
    monkeypatch.setattr(lsp.os, "name", "nt")
    webapp = tmp_path / "webapp"
    webapp.mkdir()
    (webapp / "package.json").write_text("{}", encoding="utf-8")
    (webapp / "tsconfig.json").write_text("{}", encoding="utf-8")
    tsc_cmd = _touch_tool(webapp / "node_modules" / ".bin" / "tsc.cmd")

    called: dict = {}

    def fake_capture(cmd, *, cwd, timeout_s):
        called["cmd"] = list(cmd)
        called["cwd"] = cwd
        return 0, ""

    monkeypatch.setattr(lsp, "_run_command_capture", fake_capture)

    _ = lsp.get_lsp_report(
        language="typescript",
        mode="diagnostics",
        root=str(tmp_path),
        timeout_ms=500,
    )

    assert called["cwd"] == str(webapp)
    assert called["cmd"][:3] == ["cmd.exe", "/c", tsc_cmd]
    assert "--noEmit" in called["cmd"]
    p_idx = called["cmd"].index("-p")
    assert os.path.normpath(called["cmd"][p_idx + 1]) == os.path.normpath(
        webapp / "tsconfig.json"
    )

