"""Warm Cursor ACP driver: mocked stdio transport (no live agent)."""

from __future__ import annotations

import json
import threading
from typing import List, Optional

import pytest

from pmharness.drivers import cursor_acp
from pmharness.drivers.cursor_acp import (
    AcpTransport,
    CursorAcpDriver,
    WarmAcpSession,
    _extract_tool_hint,
    _extract_update_text,
    cursor_acp_enabled,
)


class _FakePipe:
    def __init__(self) -> None:
        self._buf: List[str] = []
        self._cv = threading.Condition()
        self._closed = False

    def write(self, data: str) -> int:
        with self._cv:
            self._buf.append(data)
            self._cv.notify_all()
        return len(data)

    def flush(self) -> None:
        return

    def close(self) -> None:
        with self._cv:
            self._closed = True
            self._cv.notify_all()

    def readline(self) -> str:
        with self._cv:
            while not self._buf and not self._closed:
                self._cv.wait(timeout=0.05)
            if not self._buf:
                return ""
            chunk = self._buf.pop(0)
        # May contain multiple lines
        if "\n" in chunk:
            line, rest = chunk.split("\n", 1)
            if rest:
                with self._cv:
                    self._buf.insert(0, rest)
            return line + "\n"
        return chunk


class _FakeProc:
    def __init__(self) -> None:
        self.stdin = _FakePipe()
        self.stdout = _FakePipe()
        self.stderr = _FakePipe()
        self._code: Optional[int] = None
        self._agent = threading.Thread(target=self._serve, daemon=True)
        self._session_id = "sess-warm-1"
        self._prompt_count = 0
        self._agent.start()

    def poll(self) -> Optional[int]:
        return self._code

    def terminate(self) -> None:
        self._code = 0
        self.stdout.close()

    def kill(self) -> None:
        self._code = 1
        self.stdout.close()

    def wait(self, timeout: Optional[float] = None) -> int:
        return int(self._code or 0)

    def _serve(self) -> None:
        while self._code is None:
            line = self.stdin.readline()
            if not line:
                if self._code is not None:
                    break
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            mid = msg.get("id")
            method = msg.get("method")
            if method == "initialize":
                self._reply(mid, {"protocolVersion": 1})
            elif method == "authenticate":
                self._reply(mid, {"authenticated": True})
            elif method == "session/new":
                self._reply(mid, {"sessionId": self._session_id})
            elif method == "session/prompt":
                self._prompt_count += 1
                # Stream two chunks then finish.
                self._notify(
                    "session/update",
                    {
                        "sessionId": self._session_id,
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": "pong"},
                        },
                    },
                )
                self._notify(
                    "session/update",
                    {
                        "sessionId": self._session_id,
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": "-ok"},
                        },
                    },
                )
                self._reply(
                    mid,
                    {
                        "stopReason": "end_turn",
                        "usage": {"inputTokens": 120, "outputTokens": 8},
                    },
                )
            elif method == "initialized":
                continue
            elif mid is not None:
                self._reply(mid, {})

    def _reply(self, mid, result) -> None:
        self.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}) + "\n")

    def _notify(self, method: str, params: dict) -> None:
        self.stdout.write(
            json.dumps({"jsonrpc": "2.0", "method": method, "params": params}) + "\n"
        )


def test_cursor_acp_enabled_default(monkeypatch):
    monkeypatch.delenv("HARNESS_CURSOR_ACP", raising=False)
    assert cursor_acp_enabled() is True
    monkeypatch.setenv("HARNESS_CURSOR_ACP", "0")
    assert cursor_acp_enabled() is False


def test_extract_update_text_chunk():
    params = {
        "update": {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "hi"},
        }
    }
    assert _extract_update_text(params) == "hi"


def test_extract_tool_hint():
    params = {
        "update": {
            "sessionUpdate": "tool_call",
            "toolName": "ShellToolCall",
        }
    }
    assert _extract_tool_hint(params) == "ShellToolCall"


def test_warm_session_reuses_process_across_prompts():
    proc = _FakeProc()
    transport = AcpTransport(proc)
    session = WarmAcpSession(
        model="cursor-grok-4.5-high",
        cwd="C:\\tmp\\ws",
        transport_factory=lambda: transport,
    )
    # First ensure performs handshake
    session.ensure()
    assert session.session_id == "sess-warm-1"
    deltas1: List[str] = []
    out1 = session.prompt("Reply pong", on_delta=deltas1.append, timeout=5.0)
    assert out1["text"] == "pong-ok"
    assert deltas1 == ["pong", "-ok"]
    assert proc._prompt_count == 1

    # Second prompt must reuse same transport/session (no second handshake).
    same = session.ensure()
    assert same is transport
    out2 = session.prompt("again", timeout=5.0)
    assert out2["text"] == "pong-ok"
    assert proc._prompt_count == 2
    session.close()


def test_driver_falls_back_to_print_when_acp_handshake_fails(monkeypatch):
    class BoomSession(WarmAcpSession):
        def prompt(self, *a, **k):
            raise RuntimeError("handshake boom")

    class Fallback:
        def __init__(self):
            self.called = False

        def _run_stream(self, messages, **kwargs):
            self.called = True
            from pmharness.drivers.base import DriverResponse

            return DriverResponse(text="fallback", model="cursor-cli:x")

    fb = Fallback()
    drv = CursorAcpDriver(
        name="cursor-cli:x",
        model="x",
        session=BoomSession(model="x", cwd=None),
        fallback=fb,  # type: ignore[arg-type]
    )
    monkeypatch.setenv("HARNESS_CURSOR_ACP", "1")
    resp = drv.chat_stream(
        [{"role": "user", "content": "hi"}],
        on_delta=lambda _t: None,
    )
    assert fb.called is True
    assert resp.text == "fallback"
    # Transient ACP failure must not permanently disable the warm path.
    assert drv._acp_disabled is False


def test_driver_uses_acp_when_session_works(monkeypatch):
    proc = _FakeProc()
    transport = AcpTransport(proc)
    session = WarmAcpSession(
        model="m",
        cwd="C:\\ws",
        transport_factory=lambda: transport,
    )

    class NoFallback:
        def _run_stream(self, *a, **k):
            raise AssertionError("must not fall back")

    drv = CursorAcpDriver(
        name="cursor-cli:m",
        model="m",
        session=session,
        fallback=NoFallback(),  # type: ignore[arg-type]
    )
    monkeypatch.setenv("HARNESS_CURSOR_ACP", "1")
    deltas: List[str] = []
    resp = drv.chat_stream(
        [{"role": "user", "content": "who are you?"}],
        on_delta=deltas.append,
    )
    assert resp.text == "pong-ok"
    assert resp.meta.get("cursor_acp") is True
    assert resp.meta.get("billing") == "plan"
    assert resp.meta.get("tool_calls") == []
    assert resp.tokens_in == 120
    assert resp.tokens_out == 8
    assert deltas == ["pong", "-ok"]
    drv.close()
