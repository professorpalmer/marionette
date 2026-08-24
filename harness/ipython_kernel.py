from __future__ import annotations

"""Session-scoped persistent Python REPL for the ``run_ipython`` pilot tool.

Steals Prime Agent's *persistent kernel* insight without IPython monoculture:
native file/shell/swarm tools stay first-class; this is one extra tool for
stateful probes (variables survive across turns in the same session).

Uses IPython's InteractiveShell when installed; otherwise falls back to a
stdlib ``code.InteractiveInterpreter`` so the harness stays usable without a
new hard dependency (Marionette remains stdlib-first).
"""

import ast
import contextlib
import io
import os
import threading
import traceback
from dataclasses import dataclass
from typing import Any, Optional

DEFAULT_TIMEOUT_SEC = 60.0
DEFAULT_OUTPUT_CAP = 64 * 1024
MAX_IPYTHON_DEPTH = 2
_ipython_depth = threading.local()
_INSTALL_HINT = (
    "IPython is not installed. For richer display/repr, run: "
    "pip install ipython  (stdlib fallback kernel is still active)"
)


@dataclass
class KernelResult:
    ok: bool
    output: str
    error: str = ""
    backend: str = "stdlib"
    timed_out: bool = False


def _clamp(text: str, cap: int = DEFAULT_OUTPUT_CAP) -> str:
    if not text:
        return ""
    if len(text) <= cap:
        return text
    omitted = len(text) - cap
    return text[:cap] + f"\n… [truncated {omitted} chars]"


def _split_last_expression(source: str):
    """Return (exec_source, eval_source_or_None) for last-line display."""
    try:
        tree = ast.parse(source, mode="exec")
    except SyntaxError:
        return source, None
    if not tree.body:
        return source, None
    last = tree.body[-1]
    if not isinstance(last, ast.Expr):
        return source, None
    exec_body = tree.body[:-1]
    if not hasattr(ast, "unparse"):
        return source, None
    try:
        if exec_body:
            exec_mod = ast.Module(body=exec_body, type_ignores=[])
            if hasattr(ast, "fix_missing_locations"):
                ast.fix_missing_locations(exec_mod)
            exec_src = ast.unparse(exec_mod)
        else:
            exec_src = ""
        eval_src = ast.unparse(last.value)
    except Exception:
        return source, None
    return exec_src, eval_src


class PersistentPythonKernel:
    """One persistent user namespace per ConversationalSession."""

    def __init__(self, cwd: str) -> None:
        self.cwd = os.path.realpath(cwd) if cwd else os.getcwd()
        self._ns: dict = {"__name__": "__main__"}
        self._backend = "stdlib"
        self._ipython_shell: Any = None
        self._lock = threading.RLock()
        self._closed = False
        self._try_init_ipython()

    def _try_init_ipython(self) -> None:
        try:
            from IPython.core.interactiveshell import InteractiveShell
        except Exception:
            return
        try:
            # Fresh shell — do not touch InteractiveShell.instance() singleton.
            shell = InteractiveShell(user_ns=self._ns)
            self._ipython_shell = shell
            self._ns = shell.user_ns
            self._backend = "ipython"
        except Exception:
            self._ipython_shell = None
            self._backend = "stdlib"

    @property
    def backend(self) -> str:
        return self._backend

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._ipython_shell = None
            self._ns.clear()

    def execute(
        self,
        code: str,
        *,
        timeout: float = DEFAULT_TIMEOUT_SEC,
        output_cap: int = DEFAULT_OUTPUT_CAP,
    ) -> KernelResult:
        if self._closed:
            return KernelResult(
                ok=False, output="", error="kernel closed", backend=self._backend,
            )
        source = (code or "").strip()
        if not source:
            return KernelResult(
                ok=False, output="", error="empty code", backend=self._backend,
            )

        box: dict = {"result": None, "error": None}

        def _target() -> None:
            try:
                box["result"] = self._execute_locked(source)
            except Exception as exc:
                box["error"] = exc

        thread = threading.Thread(target=_target, name="marionette-ipython", daemon=True)
        thread.start()
        thread.join(timeout=max(0.1, float(timeout)))
        if thread.is_alive():
            return KernelResult(
                ok=False,
                output="",
                error=f"execution timed out after {timeout:.0f}s",
                backend=self._backend,
                timed_out=True,
            )
        if box["error"] is not None:
            return KernelResult(
                ok=False,
                output="",
                error=_clamp(f"{type(box['error']).__name__}: {box['error']}", output_cap),
                backend=self._backend,
            )
        result = box["result"]
        assert isinstance(result, KernelResult)
        return KernelResult(
            ok=result.ok,
            output=_clamp(result.output, output_cap),
            error=_clamp(result.error, output_cap),
            backend=result.backend,
            timed_out=result.timed_out,
        )

    def _execute_locked(self, source: str) -> KernelResult:
        with self._lock:
            prev_cwd = os.getcwd()
            stdout = io.StringIO()
            stderr = io.StringIO()
            try:
                os.chdir(self.cwd)
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    if self._ipython_shell is not None:
                        return self._run_ipython(source, stdout, stderr)
                    return self._run_stdlib(source, stdout, stderr)
            finally:
                try:
                    os.chdir(prev_cwd)
                except Exception:
                    pass

    def _run_ipython(
        self, source: str, stdout: io.StringIO, stderr: io.StringIO,
    ) -> KernelResult:
        shell = self._ipython_shell
        try:
            result = shell.run_cell(source, store_history=False)
        except Exception:
            err = traceback.format_exc()
            return KernelResult(
                ok=False,
                output=_combine(stdout.getvalue(), stderr.getvalue(), ""),
                error=err,
                backend="ipython",
            )
        out = _combine(stdout.getvalue(), stderr.getvalue(), "")
        if result is not None and getattr(result, "result", None) is not None:
            try:
                display = repr(result.result)
            except Exception:
                display = str(result.result)
            out = _combine(out, "", display)
        err_text = ""
        if result is not None and getattr(result, "error_in_exec", None):
            err_text = str(result.error_in_exec)
            return KernelResult(
                ok=False, output=out, error=err_text or "execution error",
                backend="ipython",
            )
        success = True
        if result is not None and hasattr(result, "success"):
            success = bool(result.success)
        return KernelResult(
            ok=success, output=out, error="" if success else (err_text or "failed"),
            backend="ipython",
        )

    def _run_stdlib(
        self, source: str, stdout: io.StringIO, stderr: io.StringIO,
    ) -> KernelResult:
        import code

        interp = code.InteractiveInterpreter(locals=self._ns)
        exec_src, eval_src = _split_last_expression(source)
        try:
            if exec_src.strip():
                compiled = compile(exec_src, "<run_ipython>", "exec")
                interp.runcode(compiled)
            display = ""
            if eval_src is not None:
                try:
                    value = eval(compile(eval_src, "<run_ipython>", "eval"), self._ns, self._ns)
                except Exception:
                    interp.showtraceback()
                    value = None
                else:
                    if value is not None:
                        display = repr(value)
                        self._ns["_"] = value
            elif not exec_src.strip():
                # Whole source was a single expression that unparse couldn't
                # peel (e.g. 3.9) — exec as-is.
                compiled = compile(source, "<run_ipython>", "exec")
                interp.runcode(compiled)
            out = _combine(stdout.getvalue(), stderr.getvalue(), display)
            # InteractiveInterpreter writes tracebacks to stderr via showtraceback.
            err_blob = stderr.getvalue()
            if "Traceback" in err_blob and not display and not stdout.getvalue().strip():
                return KernelResult(
                    ok=False, output=out, error=err_blob.strip() or "error",
                    backend="stdlib",
                )
            return KernelResult(ok=True, output=out, error="", backend="stdlib")
        except Exception:
            err = traceback.format_exc()
            return KernelResult(
                ok=False,
                output=_combine(stdout.getvalue(), stderr.getvalue(), ""),
                error=err,
                backend="stdlib",
            )


def _combine(stdout: str, stderr: str, display: str) -> str:
    parts = []
    if stdout:
        parts.append(stdout.rstrip("\n"))
    if stderr:
        parts.append(stderr.rstrip("\n"))
    if display:
        parts.append(display)
    return "\n".join(p for p in parts if p)


def get_or_create_kernel(session: Any) -> PersistentPythonKernel:
    """Lazy session attribute ``_ipython_kernel``."""
    existing = getattr(session, "_ipython_kernel", None)
    if isinstance(existing, PersistentPythonKernel) and not existing._closed:
        # Retarget cwd if workspace moved.
        config = getattr(session, "config", None)
        cwd = (
            getattr(config, "repo", None)
            or getattr(config, "state_dir", None)
            or os.getcwd()
        )
        if cwd and os.path.realpath(str(cwd)) != existing.cwd:
            existing.cwd = os.path.realpath(str(cwd))
        return existing
    config = getattr(session, "config", None)
    cwd = (
        getattr(config, "repo", None)
        or getattr(config, "state_dir", None)
        or os.getcwd()
    )
    kernel = PersistentPythonKernel(str(cwd))
    try:
        setattr(session, "_ipython_kernel", kernel)
    except Exception:
        pass
    return kernel


def ipython_install_hint_if_stdlib(kernel: PersistentPythonKernel) -> Optional[str]:
    """Non-fatal note when falling back to stdlib (once-friendly)."""
    if kernel.backend == "stdlib":
        return _INSTALL_HINT
    return None
