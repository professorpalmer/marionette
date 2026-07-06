from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Iterable, Optional


@dataclass(frozen=True)
class LspDiagnostic:
    file: Optional[str]
    line: Optional[int]
    column: Optional[int]
    severity: str
    code: Optional[str]
    message: str


def _which(command: str, *, which_fn=None) -> Optional[str]:
    if which_fn is None:
        which_fn = shutil.which
    try:
        return which_fn(command)
    except Exception:
        return None


@dataclass(frozen=True)
class LspToolAvailability:
    python_pyright: Optional[str]
    python_pyright_langserver: Optional[str]
    typescript_tsc: Optional[str]
    typescript_tsserver: Optional[str]
    typescript_typescript_language_server: Optional[str]

    @property
    def python_available(self) -> bool:
        return bool(self.python_pyright or self.python_pyright_langserver)

    @property
    def typescript_available(self) -> bool:
        return bool(self.typescript_tsc or self.typescript_tsserver or self.typescript_typescript_language_server)


def discover_lsp_tools(*, which_fn=None) -> LspToolAvailability:
    # Keep tool discovery conservative: first-pass diagnostics should use the
    # simplest, best-supported CLI surface for each language.
    if which_fn is None:
        which_fn = shutil.which
    return LspToolAvailability(
        python_pyright=_which("pyright", which_fn=which_fn),
        python_pyright_langserver=_which("pyright-langserver", which_fn=which_fn),
        typescript_tsc=_which("tsc", which_fn=which_fn),
        # `tsserver` is not commonly on PATH, but the request explicitly calls
        # it out, so surface it if available.
        typescript_tsserver=_which("tsserver", which_fn=which_fn),
        # Common alternative when `tsc` is missing.
        typescript_typescript_language_server=_which("typescript-language-server", which_fn=which_fn),
    )


def _run_command_capture(
    cmd: list[str],
    *,
    cwd: str,
    timeout_s: float,
) -> tuple[int, str]:
    # Windows-safe: use args list (no shell=True) and rely on harness' global
    # win_console hidden-console defaults.
    p = subprocess.run(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
        timeout=timeout_s,
        shell=False,
        check=False,
    )
    return p.returncode, p.stdout or ""


def _iter_diag_items(obj: Any) -> Iterable[dict[str, Any]]:
    # Best-effort traversal: pyright output shape can evolve.
    if isinstance(obj, dict):
        if all(k in obj for k in ("severity", "message")):
            yield obj  # might or might not have range/file
        for v in obj.values():
            yield from _iter_diag_items(v)
    elif isinstance(obj, list):
        for it in obj:
            yield from _iter_diag_items(it)


def parse_pyright_diagnostics(output_json: str) -> list[LspDiagnostic]:
    try:
        payload = json.loads(output_json)
    except Exception:
        return []

    diags: list[LspDiagnostic] = []
    for item in _iter_diag_items(payload):
        try:
            severity = str(item.get("severity") or "unknown").lower()
            message = str(item.get("message") or "").strip()
            code = item.get("rule") or item.get("code")
            code_str = str(code) if code else None
            file = item.get("file")
            line = None
            col = None
            r = item.get("range") or {}
            start = r.get("start") or {}
            if isinstance(start, dict):
                line_val = start.get("line")
                char_val = start.get("character")
                if isinstance(line_val, int):
                    # pyright uses 0-based line/character.
                    line = line_val + 1
                if isinstance(char_val, int):
                    col = char_val + 1
            diags.append(
                LspDiagnostic(
                    file=str(file) if file else None,
                    line=line,
                    column=col,
                    severity=severity,
                    code=code_str,
                    message=message,
                )
            )
        except Exception:
            continue
    # De-dup identical items (best-effort, not stability-critical).
    seen: set[tuple[Any, ...]] = set()
    out: list[LspDiagnostic] = []
    for d in diags:
        key = (d.file, d.line, d.column, d.severity, d.code, d.message)
        if key in seen:
            continue
        seen.add(key)
        out.append(d)
    return out


_TSC_LINE_RE = re.compile(
    r"^(?P<file>.+?)\((?P<line>\d+),(?P<col>\d+)\):\s*"
    r"(?:(?P<sev>error|warning)\s+)?"
    r"TS(?P<code>\d+):\s*(?P<msg>.+?)\s*$",
    re.IGNORECASE,
)

_TSC_NOLOC_RE = re.compile(
    r"^error\s+TS(?P<code>\d+):\s*(?P<msg>.+?)\s*$",
    re.IGNORECASE,
)


def parse_tsc_diagnostics(output: str) -> list[LspDiagnostic]:
    diags: list[LspDiagnostic] = []
    for raw_line in (output or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m = _TSC_LINE_RE.match(line)
        if m:
            sev = (m.group("sev") or "error").lower()
            file = m.group("file").strip()
            diags.append(
                LspDiagnostic(
                    file=file,
                    line=int(m.group("line")),
                    column=int(m.group("col")),
                    severity=sev,
                    code=m.group("code"),
                    message=m.group("msg").strip(),
                )
            )
            continue
        m2 = _TSC_NOLOC_RE.match(line)
        if m2:
            diags.append(
                LspDiagnostic(
                    file=None,
                    line=None,
                    column=None,
                    severity="error",
                    code=m2.group("code"),
                    message=m2.group("msg").strip(),
                )
            )
    return diags


def _format_diagnostics(diags: list[LspDiagnostic], *, max_items: int = 80) -> str:
    if not diags:
        return "No diagnostics found."
    items = diags[:max_items]
    lines: list[str] = []
    for d in items:
        loc = ""
        if d.file:
            loc = d.file
            if d.line is not None:
                loc += f":{d.line}"
                if d.column is not None:
                    loc += f":{d.column}"
            loc += ": "
        lines.append(
            f"{loc}{d.severity}"
            + (f" TS{d.code}" if d.code else "")
            + f": {d.message}"
        )
    if len(diags) > max_items:
        lines.append(f"... ({len(diags) - max_items} more diagnostics truncated) ...")
    return "\n".join(lines)


def _word_boundary_pattern(symbol: str) -> re.Pattern:
    escaped = re.escape(symbol.strip())
    return re.compile(r"\b" + escaped + r"\b")


def _text_scan_references(
    symbol: str,
    root: str,
    *,
    max_results: int = 50,
) -> list[str]:
    """Fallback reference search: word-boundary scan over text files."""
    if not symbol.strip():
        return []
    pattern = _word_boundary_pattern(symbol)
    skip_dirs = {".git", "node_modules", "results", "build", "dist", "__pycache__", ".venv"}
    matches: list[str] = []
    for dir_root, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for filename in files:
            file_path = os.path.join(dir_root, filename)
            try:
                with open(file_path, "rb") as fh:
                    chunk = fh.read(8000)
                    if b"\x00" in chunk:
                        continue
            except OSError:
                continue
            try:
                with open(file_path, "r", encoding="utf-8", errors="replace") as fh:
                    for line_num, line in enumerate(fh, 1):
                        if pattern.search(line):
                            rel_path = os.path.relpath(file_path, root).replace(os.sep, "/")
                            matches.append(f"{rel_path}:{line_num}: {line.rstrip()}")
                            if len(matches) >= max_results:
                                return matches
            except OSError:
                continue
    return matches


def _codegraph_references(symbol: str, root: str, *, timeout_s: float) -> tuple[bool, str]:
    """Query CodeGraph for symbol usages; returns (ok, output)."""
    from ._exec import _puppetmaster_cmd

    cmd = _puppetmaster_cmd("codegraph", "query", symbol)
    try:
        p = subprocess.run(
            cmd,
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            timeout=timeout_s,
        )
        output = (p.stdout or "").strip()
        if p.returncode != 0:
            if "no module named" in output.lower() or p.returncode == 127:
                return False, "CodeGraph unavailable"
            return False, output[:2000] or f"CodeGraph exit {p.returncode}"
        if not output:
            return False, "CodeGraph returned no matches"
        return True, output[:6000]
    except FileNotFoundError:
        return False, "CodeGraph unavailable"
    except subprocess.TimeoutExpired:
        return False, "CodeGraph timed out"
    except Exception as exc:
        return False, str(exc)


def get_symbol_references(
    symbol: str,
    root: str,
    *,
    timeout_ms: Optional[int] = None,
    max_results: int = 50,
) -> str:
    """Locate usages of ``symbol`` via CodeGraph when present, else text scan."""
    symbol = (symbol or "").strip()
    if not symbol:
        return "references: empty symbol"
    timeout_ms = timeout_ms if timeout_ms is not None else 8000
    try:
        timeout_ms_int = int(timeout_ms)
    except Exception:
        timeout_ms_int = 8000
    timeout_s = max(0.1, timeout_ms_int / 1000.0)

    chunks: list[str] = [f"References for `{symbol}`:"]
    cg_ok, cg_out = _codegraph_references(symbol, root, timeout_s=timeout_s)
    if cg_ok:
        chunks.append("CodeGraph:")
        chunks.append(cg_out)
        return "\n".join(chunks)

    chunks.append(f"CodeGraph: {cg_out}")
    matches = _text_scan_references(symbol, root, max_results=max_results)
    if matches:
        chunks.append("Text scan:")
        chunks.append("\n".join(matches))
        if len(matches) >= max_results:
            chunks.append(f"... (truncated to {max_results} matches) ...")
    else:
        chunks.append("Text scan: no matches found.")
    return "\n".join(chunks)


def _language_to_probe(language: str) -> tuple[bool, bool]:
    lang = (language or "auto").lower().strip()
    if lang == "python":
        return True, False
    if lang == "typescript":
        return False, True
    return True, True


def get_lsp_status(*, language: str, root: str, tools: Optional[LspToolAvailability] = None) -> str:
    tools = tools or discover_lsp_tools()
    probe_py, probe_ts = _language_to_probe(language)
    lines: list[str] = []
    lines.append("LSP status")
    if probe_py:
        lines.append(
            "Python: "
            + (
                "pyright available"
                if tools.python_pyright
                else "pyright not found"
            )
            + (
                f" (pyright-langserver: {bool(tools.python_pyright_langserver)})"
                if tools.python_pyright_langserver or tools.python_pyright
                else ""
            )
        )
    if probe_ts:
        lines.append(
            "TypeScript: "
            + ("tsc available" if tools.typescript_tsc else "tsc not found")
            + f" (tsserver: {bool(tools.typescript_tsserver)}, typescript-language-server: {bool(tools.typescript_typescript_language_server)})"
        )
    # Keep it small: only summarize.
    return "\n".join(lines)


def get_lsp_report(
    *,
    language: str,
    mode: str,
    root: str,
    timeout_ms: Optional[int] = None,
    tools: Optional[LspToolAvailability] = None,
    symbol: Optional[str] = None,
) -> str:
    language = (language or "auto").lower().strip()
    mode = (mode or "diagnostics").lower().strip()
    timeout_ms = timeout_ms if timeout_ms is not None else 8000
    try:
        timeout_ms_int = int(timeout_ms)
    except Exception:
        timeout_ms_int = 8000
    timeout_s = max(0.1, timeout_ms_int / 1000.0)

    root = root or os.getcwd()
    tools = tools or discover_lsp_tools()
    probe_py, probe_ts = _language_to_probe(language)

    if mode == "status":
        return get_lsp_status(language=language, root=root, tools=tools)

    if mode == "references":
        if not (symbol or "").strip():
            return "references mode requires a non-empty symbol."
        return get_symbol_references(symbol or "", root, timeout_ms=timeout_ms_int)

    # diagnostics
    chunks: list[str] = []
    if probe_py:
        if not tools.python_pyright:
            chunks.append("Python diagnostics: no tool available (pyright not found).")
        else:
            chunks.append("Python diagnostics:")
            try:
                cmd = [tools.python_pyright, "--outputjson", "."]
                rc, output = _run_command_capture(cmd, cwd=root, timeout_s=timeout_s)
                py_diags = parse_pyright_diagnostics(output)
                if not py_diags and output.strip():
                    # Provide parsing failure context (but keep output small).
                    chunks.append("  (No parsable pyright diagnostics in output.)")
                    # If pyright emitted errors but our parser didn't find them,
                    # fall back to raw output hint.
                    chunks.append("  (pyright exit code: " + str(rc) + ")")
                if py_diags:
                    chunks.append(f"  Parsed {len(py_diags)} diagnostics.")
                    chunks.append("  " + _format_diagnostics(py_diags).replace("\n", "\n  "))
            except subprocess.TimeoutExpired:
                chunks.append(f"  Timed out after {timeout_ms_int}ms.")
            except FileNotFoundError:
                chunks.append("  pyright executable vanished (not found).")
            except Exception as e:
                chunks.append("  pyright diagnostics failed: " + str(e))

    if probe_ts:
        if not tools.typescript_tsc:
            chunks.append("TypeScript diagnostics: no tool available (tsc not found).")
        else:
            chunks.append("TypeScript diagnostics:")
            try:
                tsconfig = os.path.join(root, "tsconfig.json")
                cmd: list[str] = [tools.typescript_tsc, "--noEmit", "--pretty", "false"]
                if os.path.isfile(tsconfig):
                    cmd.extend(["-p", tsconfig])
                rc, output = _run_command_capture(cmd, cwd=root, timeout_s=timeout_s)
                ts_diags = parse_tsc_diagnostics(output)
                if ts_diags:
                    chunks.append(f"  Parsed {len(ts_diags)} diagnostics.")
                    chunks.append("  " + _format_diagnostics(ts_diags).replace("\n", "\n  "))
                else:
                    chunks.append("  No TypeScript diagnostics parsed.")
                chunks.append(f"  (tsc exit code: {rc})")
            except subprocess.TimeoutExpired:
                chunks.append(f"  Timed out after {timeout_ms_int}ms.")
            except FileNotFoundError:
                chunks.append("  tsc executable vanished (not found).")
            except Exception as e:
                chunks.append("  tsc diagnostics failed: " + str(e))

    return "\n".join(chunks)

