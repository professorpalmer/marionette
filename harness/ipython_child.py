"""Child process for the ``run_ipython`` kernel. Stdlib only, no harness imports.

The parent starts it with ``python -I <this file>``. Requests and replies are
JSON lines on private copies of stdin and stdout. Fd 0, 1 and 2 then point at
the null device, so a cell can not read a request or write into a reply.

The child runs each cell in its main thread. A SIGINT from the parent stops
the cell with KeyboardInterrupt, and the namespace stays. The child exits
when its parent exits, also while a cell runs.
"""
from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import signal
import sys
import threading
import time
import traceback


def _split_last_expression(source):
    """Return (exec_source, eval_source_or_None) to show the last expression."""
    try:
        tree = ast.parse(source, mode="exec")
    except SyntaxError:
        return source, None
    if not tree.body or not isinstance(tree.body[-1], ast.Expr) or not hasattr(ast, "unparse"):
        return source, None
    try:
        body = tree.body[:-1]
        exec_src = ast.unparse(ast.Module(body=body, type_ignores=[])) if body else ""
        return exec_src, ast.unparse(tree.body[-1].value)
    except Exception:
        return source, None


def _combine(*parts):
    return "\n".join(p.rstrip("\n") for p in parts if p)


class _Runner:
    def __init__(self):
        self.ns = {"__name__": "__main__"}
        self.shell = None
        try:
            from IPython.core.interactiveshell import InteractiveShell

            self.shell = InteractiveShell(user_ns=self.ns)
            self.ns = self.shell.user_ns
        except Exception:
            self.shell = None

    @property
    def backend(self):
        return "ipython" if self.shell is not None else "stdlib"

    def run(self, source):
        stdout, stderr = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                if self.shell is not None:
                    ok, display, error = self._run_ipython(source)
                else:
                    ok, display, error = self._run_stdlib(source, stderr)
        except KeyboardInterrupt:
            output = _combine(stdout.getvalue(), stderr.getvalue())
            return {"ok": False, "output": output, "error": "", "interrupted": True}
        return {
            "ok": ok,
            "output": _combine(stdout.getvalue(), stderr.getvalue(), display),
            "error": error,
        }

    def _run_ipython(self, source):
        result = self.shell.run_cell(source, store_history=False)
        if isinstance(getattr(result, "error_in_exec", None), KeyboardInterrupt):
            raise KeyboardInterrupt
        display = ""
        if getattr(result, "result", None) is not None:
            try:
                display = repr(result.result)
            except Exception:
                display = str(result.result)
        if getattr(result, "error_in_exec", None) is not None:
            return False, display, str(result.error_in_exec) or "execution error"
        if not getattr(result, "success", True):
            return False, display, "failed"
        return True, display, ""

    def _run_stdlib(self, source, stderr):
        exec_src, eval_src = _split_last_expression(source)
        try:
            if eval_src is None:
                exec(compile(source, "<run_ipython>", "exec"), self.ns)
                return True, "", ""
            if exec_src.strip():
                exec(compile(exec_src, "<run_ipython>", "exec"), self.ns)
            value = eval(compile(eval_src, "<run_ipython>", "eval"), self.ns)
        except KeyboardInterrupt:
            raise
        except BaseException:
            return False, "", traceback.format_exc().strip()
        if value is None:
            return True, "", ""
        self.ns["_"] = value
        return True, repr(value), ""


def _exit_with_parent(parent_pid):
    # The cell may never return, so a stdin EOF alone can not end the child.
    while True:
        time.sleep(1.0)
        if os.getppid() != parent_pid:
            os._exit(0)


class _CellInterrupts:
    """SIGINT stops a running cell. Between cells it does nothing.

    The parent can send SIGINT just after a cell ends. Without this guard
    that signal ends the child and the namespace is lost.
    """

    def __init__(self):
        self.running = False
        if hasattr(signal, "SIGINT"):
            signal.signal(signal.SIGINT, self._handle)

    def _handle(self, signum, frame):
        if self.running:
            raise KeyboardInterrupt


def main():
    interrupts = _CellInterrupts()
    requests = os.fdopen(os.dup(0), "r", encoding="utf-8")
    replies = os.fdopen(os.dup(1), "w", encoding="utf-8")
    null = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(null, fd)
    sys.stdin = io.StringIO("")
    threading.Thread(
        target=_exit_with_parent, args=(os.getppid(),), daemon=True,
    ).start()

    runner = _Runner()
    replies.write(json.dumps({"ready": True, "backend": runner.backend}) + "\n")
    replies.flush()
    for line in requests:
        try:
            request = json.loads(line)
        except ValueError:
            continue
        try:
            os.chdir(request.get("cwd") or ".")
        except OSError:
            pass
        interrupts.running = True
        try:
            reply = runner.run(str(request.get("code") or ""))
        except KeyboardInterrupt:
            reply = {"ok": False, "output": "", "error": "", "interrupted": True}
        finally:
            interrupts.running = False
        cap = int(request.get("cap") or 0)
        if cap:
            reply["output"] = reply["output"][: cap * 2]
            reply["error"] = reply["error"][: cap * 2]
        reply["id"] = request.get("id")
        replies.write(json.dumps(reply) + "\n")
        replies.flush()


if __name__ == "__main__":
    main()
