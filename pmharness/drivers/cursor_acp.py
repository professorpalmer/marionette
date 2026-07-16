from __future__ import annotations

"""Warm Cursor Agent ACP driver — persistent `agent acp` stdio session.

Hermes/Grok-style: amortize cold start by keeping one ACP process + session
alive across turns. Falls back to ``CursorCliDriver`` (--print) when ACP is
disabled or the handshake fails.

Wire format (Cursor docs): JSON-RPC 2.0, newline-delimited, over stdio.
  initialize → authenticate(cursor_login) → session/new → session/prompt*
"""

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .base import SYSTEM_PROMPT, DriverResponse
from .cursor_cli import (
    INSTALL_HINT,
    CursorCliDriver,
    _messages_to_prompt,
    resolve_agent_exec,
)


def cursor_acp_enabled() -> bool:
    """Warm ACP path (default ON). Set ``HARNESS_CURSOR_ACP=0`` to force --print.

    Live probe on Windows: turn-1 ``session/prompt`` ~11s, turn-2 on the same
    process ~2s. Per-turn ``--print`` stays ~10–12s forever — so warm ACP is
    the daily-driver default. Auth is best-effort (short timeout); a hung
    authenticate must not block the session.
    """
    raw = (os.environ.get("HARNESS_CURSOR_ACP") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _extract_update_text(update: Any) -> str:
    """Best-effort assistant text from a session/update payload."""
    if not isinstance(update, dict):
        return ""
    # Nested under "update" (ACP) or flat.
    inner = update.get("update") if isinstance(update.get("update"), dict) else update
    if not isinstance(inner, dict):
        return ""
    kind = str(
        inner.get("sessionUpdate")
        or inner.get("session_update")
        or inner.get("type")
        or ""
    ).lower()
    content = inner.get("content")
    text = ""
    if isinstance(content, dict):
        if content.get("type") == "text" or "text" in content:
            text = str(content.get("text") or "")
    elif isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                parts.append(block)
        text = "".join(parts)
    if not text and isinstance(inner.get("text"), str):
        text = inner["text"]

    # Prefer streaming chunks; also accept full message shapes.
    if kind in (
        "agent_message_chunk",
        "agent_message",
        "message",
        "assistant_message_chunk",
        "text_delta",
        "delta",
    ):
        return text
    if kind in ("agent_thought_chunk", "thought_chunk", "reasoning", "thinking"):
        return ""  # handled separately
    # Unknown kind with text — treat as assistant delta (forward-compatible).
    if text and "thought" not in kind and "tool" not in kind:
        return text
    return ""


def _extract_thought_text(update: Any) -> str:
    if not isinstance(update, dict):
        return ""
    inner = update.get("update") if isinstance(update.get("update"), dict) else update
    if not isinstance(inner, dict):
        return ""
    kind = str(
        inner.get("sessionUpdate")
        or inner.get("session_update")
        or inner.get("type")
        or ""
    ).lower()
    if "thought" not in kind and "reason" not in kind and "thinking" not in kind:
        return ""
    content = inner.get("content")
    if isinstance(content, dict):
        return str(content.get("text") or "")
    if isinstance(content, str):
        return content
    return str(inner.get("text") or "")


def _extract_tool_hint(update: Any) -> str:
    if not isinstance(update, dict):
        return ""
    inner = update.get("update") if isinstance(update.get("update"), dict) else update
    if not isinstance(inner, dict):
        return ""
    kind = str(
        inner.get("sessionUpdate")
        or inner.get("session_update")
        or inner.get("type")
        or ""
    ).lower()
    if "tool" not in kind:
        return ""
    for key in ("toolName", "tool_name", "title", "name"):
        val = inner.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    tool_call = inner.get("toolCall") or inner.get("tool_call")
    if isinstance(tool_call, dict):
        for k in tool_call:
            if k and isinstance(k, str):
                return k
    return "tool"


class AcpTransport:
    """JSON-RPC NDJSON client over a subprocess stdin/stdout pair."""

    def __init__(self, proc: subprocess.Popen) -> None:
        self.proc = proc
        self._lock = threading.Lock()
        self._next_id = 1
        self._pending: Dict[int, "queue.Queue[dict]"] = {}
        self._closed = False
        self.on_session_update: Optional[Callable[[dict], None]] = None
        self._update_buf: List[dict] = []
        self._reader = threading.Thread(
            target=self._read_loop, name="cursor-acp-reader", daemon=True
        )
        self._reader.start()

    def set_session_update_handler(
        self, handler: Optional[Callable[[dict], None]]
    ) -> None:
        """Attach/detach streaming handler; flush any buffered updates."""
        self.on_session_update = handler
        if handler is None:
            return
        buffered = self._update_buf
        self._update_buf = []
        for params in buffered:
            try:
                handler(params)
            except Exception:
                pass

    def close(self) -> None:
        self._closed = True
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=3)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass

    def alive(self) -> bool:
        return (not self._closed) and (self.proc.poll() is None)

    def request(
        self,
        method: str,
        params: Optional[dict] = None,
        *,
        timeout: float = 60.0,
    ) -> dict:
        if not self.alive():
            return {"error": {"message": "acp process dead"}}
        with self._lock:
            mid = self._next_id
            self._next_id += 1
            waitq: "queue.Queue[dict]" = queue.Queue(maxsize=1)
            self._pending[mid] = waitq
            msg: dict = {"jsonrpc": "2.0", "id": mid, "method": method}
            if params is not None:
                msg["params"] = params
            try:
                assert self.proc.stdin is not None
                self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
                self.proc.stdin.flush()
            except Exception as exc:
                self._pending.pop(mid, None)
                return {"error": {"message": f"write failed: {exc}"}}
        try:
            return waitq.get(timeout=timeout)
        except queue.Empty:
            with self._lock:
                self._pending.pop(mid, None)
            return {"error": {"message": f"timeout waiting for {method}"}}

    def notify(self, method: str, params: Optional[dict] = None) -> None:
        msg: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        try:
            with self._lock:
                if self.proc.stdin is None:
                    return
                self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
                self.proc.stdin.flush()
        except Exception:
            pass

    def _respond(self, req_id: Any, result: dict) -> None:
        msg = {"jsonrpc": "2.0", "id": req_id, "result": result}
        try:
            with self._lock:
                if self.proc.stdin is None:
                    return
                self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
                self.proc.stdin.flush()
        except Exception:
            pass

    def _read_loop(self) -> None:
        stdout = self.proc.stdout
        if stdout is None:
            return
        try:
            while not self._closed:
                readline = getattr(stdout, "readline", None)
                if callable(readline):
                    raw = readline()
                else:
                    try:
                        raw = next(iter(stdout))
                    except StopIteration:
                        raw = ""
                if not raw:
                    break
                line = raw.strip() if isinstance(raw, str) else str(raw).strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    continue
                if not isinstance(msg, dict):
                    continue
                # Server → client request (permissions, fs, …)
                if "method" in msg and "id" in msg and "result" not in msg:
                    self._handle_server_request(msg)
                    continue
                # Response to our request
                if "id" in msg and ("result" in msg or "error" in msg):
                    mid = msg.get("id")
                    try:
                        mid_i = int(mid)  # type: ignore[arg-type]
                    except (TypeError, ValueError):
                        continue
                    with self._lock:
                        waitq = self._pending.pop(mid_i, None)
                    if waitq is not None:
                        try:
                            waitq.put_nowait(msg)
                        except Exception:
                            pass
                    continue
                # Notification — resolve handler after read so prompt() can
                # attach on_session_update before chunks arrive.
                method = str(msg.get("method") or "")
                if method in ("session/update", "cursor/update_todos"):
                    params = msg.get("params") or {}
                    on_update = self.on_session_update
                    if callable(on_update):
                        try:
                            on_update(params if isinstance(params, dict) else {})
                        except Exception:
                            pass
                    elif isinstance(params, dict):
                        self._update_buf.append(params)
        finally:
            # Unblock waiters on death
            with self._lock:
                pending = list(self._pending.items())
                self._pending.clear()
            for _, waitq in pending:
                try:
                    waitq.put_nowait({"error": {"message": "acp reader closed"}})
                except Exception:
                    pass

    def _handle_server_request(self, msg: dict) -> None:
        method = str(msg.get("method") or "")
        req_id = msg.get("id")
        # Auto-approve tool permissions so the warm loop never stalls the UI.
        if method in ("session/request_permission", "requestPermission"):
            self._respond(
                req_id,
                {"outcome": {"outcome": "selected", "optionId": "allow-once"}},
            )
            return
        # Decline client-side fs/terminal capability requests (we advertise none).
        self._respond(req_id, {})


class WarmAcpSession:
    """One long-lived ``agent acp`` process + ACP session id."""

    def __init__(
        self,
        *,
        model: str,
        cwd: Optional[str],
        timeout: int = 600,
        transport_factory: Optional[Callable[[], AcpTransport]] = None,
    ) -> None:
        self.model = model
        self.cwd = cwd
        self.timeout = timeout
        self._transport_factory = transport_factory
        self.transport: Optional[AcpTransport] = None
        self.session_id: Optional[str] = None
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            t = self.transport
            self.transport = None
            self.session_id = None
        if t is not None:
            t.close()

    def ensure(self) -> AcpTransport:
        with self._lock:
            if self.transport is not None and self.transport.alive() and self.session_id:
                return self.transport
            old = self.transport
            self.transport = None
            self.session_id = None
        if old is not None:
            try:
                old.close()
            except Exception:
                pass
        transport = (
            self._transport_factory()
            if self._transport_factory is not None
            else self._spawn_transport()
        )
        self._handshake(transport)
        with self._lock:
            if self.transport is not None and self.transport.alive() and self.session_id:
                try:
                    transport.close()
                except Exception:
                    pass
                return self.transport
            self.transport = transport
            return transport

    def _spawn_transport(self) -> AcpTransport:
        exec_prefix = resolve_agent_exec()
        if not exec_prefix:
            raise RuntimeError(f"Cursor Agent CLI not found. {INSTALL_HINT}")
        cmd = [*exec_prefix, "acp"]
        workspace = self.cwd
        popen_kwargs: dict = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "cwd": workspace or None,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "bufsize": 1,
        }
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen(cmd, **popen_kwargs)
        return AcpTransport(proc)

    def _handshake(self, transport: AcpTransport) -> None:
        init = transport.request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                },
                "clientInfo": {"name": "marionette", "version": "0.9.76"},
            },
            timeout=30.0,
        )
        if init.get("error"):
            raise RuntimeError(f"acp initialize failed: {init.get('error')}")
        transport.notify("initialized", {})
        # CLI login is often already on disk; authenticate can hang with no
        # response. Cap wait tightly and proceed — session/new is the real gate.
        transport.request(
            "authenticate",
            {"methodId": "cursor_login"},
            timeout=5.0,
        )
        params: dict = {
            "cwd": self.cwd or os.getcwd(),
            "mcpServers": [],
        }
        if self.model:
            params["model"] = self.model
        created = transport.request("session/new", params, timeout=60.0)
        if created.get("error"):
            raise RuntimeError(f"acp session/new failed: {created.get('error')}")
        result = created.get("result") or {}
        sid = result.get("sessionId") or result.get("session_id")
        if not sid:
            raise RuntimeError("acp session/new returned no sessionId")
        self.session_id = str(sid)
        # Prefer ask/plan when Marionette asked for a read-only CLI mode.
        mode = (os.environ.get("HARNESS_CURSOR_CLI_MODE") or "").strip() or "ask"
        if mode in ("ask", "plan"):
            transport.request(
                "session/set_mode",
                {"sessionId": self.session_id, "modeId": mode},
                timeout=5.0,
            )

    def prompt(
        self,
        text: str,
        *,
        on_delta: Optional[Callable[[str], None]] = None,
        on_reasoning_delta: Optional[Callable[[str], None]] = None,
        on_tool_hint: Optional[Callable[[str], None]] = None,
        timeout: Optional[float] = None,
    ) -> dict:
        transport = self.ensure()
        assert self.session_id
        chunks: List[str] = []

        def _on_update(params: dict) -> None:
            thought = _extract_thought_text(params)
            if thought and on_reasoning_delta is not None:
                try:
                    on_reasoning_delta(thought)
                except Exception:
                    pass
            hint = _extract_tool_hint(params)
            if hint and on_tool_hint is not None:
                # Surface Cursor-native tools as hints only (never host tool_calls).
                try:
                    on_tool_hint(hint)
                except Exception:
                    pass
            piece = _extract_update_text(params)
            if piece:
                chunks.append(piece)
                if on_delta is not None:
                    try:
                        on_delta(piece)
                    except Exception:
                        pass

        transport.set_session_update_handler(_on_update)
        try:
            resp = transport.request(
                "session/prompt",
                {
                    "sessionId": self.session_id,
                    "prompt": [{"type": "text", "text": text}],
                },
                timeout=float(timeout if timeout is not None else self.timeout),
            )
        finally:
            transport.set_session_update_handler(None)

        if resp.get("error"):
            # Force respawn next turn.
            self.close()
            return {"error": resp.get("error"), "text": "".join(chunks)}
        result = resp.get("result") or {}
        stop = result.get("stopReason") or result.get("stop_reason")
        final_text = "".join(chunks)
        if not final_text:
            # Some agents only put the final string in the result.
            for key in ("text", "result", "message"):
                val = result.get(key)
                if isinstance(val, str) and val.strip():
                    final_text = val
                    break
        return {
            "text": final_text,
            "stop_reason": stop,
            "session_id": self.session_id,
            "result": result,
        }


class CursorAcpDriver:
    """Warm ACP pilot with automatic --print fallback."""

    supports_streaming = True

    def __init__(
        self,
        name: str,
        model: str,
        *,
        max_tokens: int = 8000,
        timeout: int = 600,
        mode: Optional[str] = None,
        agent_binary: Optional[str] = None,
        cwd: Optional[str] = None,
        session: Optional[WarmAcpSession] = None,
        fallback: Optional[CursorCliDriver] = None,
    ) -> None:
        self.name = name
        self.model = model
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.mode = mode
        self.agent_binary = agent_binary
        self.cwd = cwd
        workspace = None
        raw = (cwd or os.environ.get("HARNESS_REPO") or "").strip()
        if raw:
            try:
                workspace = str(Path(raw).resolve())
            except OSError:
                workspace = raw
        self._workspace = workspace
        self._session = session or WarmAcpSession(
            model=model, cwd=workspace, timeout=timeout
        )
        self._fallback = fallback or CursorCliDriver(
            name=name,
            model=model,
            max_tokens=max_tokens,
            timeout=timeout,
            mode=mode,
            agent_binary=agent_binary,
            cwd=cwd,
        )
        self._acp_disabled = False
        self._acp_fail_reason = ""
        # Hide first-turn handshake behind session open when possible.
        if session is None:
            threading.Thread(
                target=self.prewarm, name="cursor-acp-prewarm", daemon=True
            ).start()

    def prewarm(self) -> None:
        """Background handshake so the first user prompt can hit a live session."""
        try:
            self._session.ensure()
        except Exception as exc:
            self._acp_fail_reason = str(exc)

    def close(self) -> None:
        self._session.close()

    def _run_acp(
        self,
        messages: list,
        *,
        system: Optional[str],
        on_delta: Optional[Callable[[str], None]],
        on_reasoning_delta: Optional[Callable[[str], None]],
        on_tool_hint: Optional[Callable[[str], None]],
    ) -> DriverResponse:
        t0 = time.time()
        prompt = _messages_to_prompt(messages, system, lean=True)
        try:
            out = self._session.prompt(
                prompt,
                on_delta=on_delta,
                on_reasoning_delta=on_reasoning_delta,
                on_tool_hint=on_tool_hint,
                timeout=float(self.timeout),
            )
        except Exception as exc:
            self._acp_fail_reason = str(exc)
            # Drop a dead transport so the next turn can respawn; do NOT
            # permanently disable ACP (that forced ~12s --print forever).
            tr = self._session.transport
            if tr is None or not tr.alive():
                self._session.close()
            raise
        if out.get("error") and not (out.get("text") or "").strip():
            self._acp_fail_reason = str(out.get("error"))
            tr = self._session.transport
            if tr is None or not tr.alive():
                self._session.close()
            raise RuntimeError(self._acp_fail_reason)
        return DriverResponse(
            text=str(out.get("text") or ""),
            latency_ms=(time.time() - t0) * 1000.0,
            model=self.name,
            meta={
                "tool_calls": [],
                "session_id": out.get("session_id") or "",
                "cursor_cli": True,
                "cursor_acp": True,
                "cursor_cli_internal_tools": [],
                "host_tools_ignored": True,
                "stop_reason": out.get("stop_reason"),
            },
        )

    def _run_stream(
        self,
        messages: list,
        *,
        tools: list | None = None,
        system: str | None = None,
        on_delta: Callable[[str], None] | None = None,
        on_reasoning_delta: Callable[[str], None] | None = None,
        on_tool_hint: Callable[[str], None] | None = None,
    ) -> DriverResponse:
        _ = tools
        if cursor_acp_enabled() and not self._acp_disabled:
            try:
                return self._run_acp(
                    messages,
                    system=system,
                    on_delta=on_delta,
                    on_reasoning_delta=on_reasoning_delta,
                    on_tool_hint=on_tool_hint,
                )
            except Exception:
                pass
        return self._fallback._run_stream(
            messages,
            tools=None,
            system=system,
            on_delta=on_delta,
            on_reasoning_delta=on_reasoning_delta,
            on_tool_hint=on_tool_hint,
        )

    def complete(self, task_prompt: str, *, system: str = SYSTEM_PROMPT) -> DriverResponse:
        return self._run_stream(
            [{"role": "user", "content": task_prompt}],
            system=system,
        )

    def chat(
        self,
        messages: list,
        *,
        tools: list | None = None,
        system: str | None = None,
        session_id: str | None = None,
    ) -> DriverResponse:
        _ = session_id
        return self._run_stream(messages, tools=tools, system=system)

    def chat_stream(
        self,
        messages: list,
        *,
        tools: list | None = None,
        system: str | None = None,
        on_delta: Callable[[str], None],
        session_id: str | None = None,
        on_reasoning_delta: Callable[[str], None] | None = None,
        on_tool_hint: Callable[[str], None] | None = None,
    ) -> DriverResponse:
        _ = session_id
        return self._run_stream(
            messages,
            tools=tools,
            system=system,
            on_delta=on_delta,
            on_reasoning_delta=on_reasoning_delta,
            on_tool_hint=on_tool_hint,
        )
