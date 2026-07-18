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
    _extract_tool_event,
    _extract_tool_hint,
    _extract_update_text,
    _reap_acp_child_tree,
    cursor_acp_enabled,
    release_owned_warm_acp,
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
    _next_pid = 91000

    def __init__(self) -> None:
        self.stdin = _FakePipe()
        self.stdout = _FakePipe()
        self.stderr = _FakePipe()
        self._code: Optional[int] = None
        _FakeProc._next_pid += 1
        self.pid = _FakeProc._next_pid
        self.terminate_calls = 0
        self.kill_calls = 0
        self._agent = threading.Thread(target=self._serve, daemon=True)
        self._session_id = "sess-warm-1"
        self._prompt_count = 0
        self._agent.start()

    def poll(self) -> Optional[int]:
        return self._code

    def terminate(self) -> None:
        self.terminate_calls += 1
        self._code = 0
        self.stdout.close()

    def kill(self) -> None:
        self.kill_calls += 1
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
    assert _extract_tool_hint(params) == "run_command"


def test_extract_tool_event_prefers_acp_kind_and_path():
    """Cursor ACP often sends kind+locations with no toolName — never bare 'tool'."""
    params = {
        "update": {
            "sessionUpdate": "tool_call",
            "toolCallId": "call_001",
            "kind": "read",
            "status": "in_progress",
            "locations": [{"path": "C:/proj/harness/server.py"}],
        }
    }
    ev = _extract_tool_event(params)
    assert ev is not None
    assert ev["name"] == "read_file"
    assert ev["goal"].endswith("server.py")
    assert ev["id"] == "call_001"
    assert _extract_tool_hint(params) == "read_file"


def test_extract_tool_event_skips_think_and_bare_tool_fallback():
    think = {
        "update": {
            "sessionUpdate": "tool_call",
            "toolCallId": "t1",
            "kind": "think",
        }
    }
    assert _extract_tool_event(think) is None
    # No kind/name/title — still emit via call id, never the literal "tool"
    bare = {
        "update": {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "call_x",
            "status": "completed",
        }
    }
    ev = _extract_tool_event(bare)
    assert ev is not None
    assert ev["name"] != "tool"
    assert ev["id"] == "call_x"
    assert ev["status"] == "completed"


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


def _live_session(monkeypatch=None):
    proc = _FakeProc()
    transport = AcpTransport(proc)
    session = WarmAcpSession(
        model="m",
        cwd="C:\\ws",
        transport_factory=lambda: transport,
    )
    session.ensure()
    return proc, transport, session


def test_close_is_idempotent_and_clears_session():
    proc, transport, session = _live_session()
    assert session.session_id == "sess-warm-1"
    assert session.transport is transport
    session.close()
    assert session.transport is None
    assert session.session_id is None
    assert transport._closed is True
    # Second close must not raise or re-touch a live process.
    before_term = proc.terminate_calls
    session.close()
    transport.close()
    assert proc.terminate_calls == before_term
    assert session.transport is None


def test_windows_close_reaps_owned_child_tree(monkeypatch):
    proc, transport, session = _live_session()
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))

        class _R:
            returncode = 0

        return _R()

    monkeypatch.setattr(cursor_acp.subprocess, "run", fake_run)
    monkeypatch.setattr(cursor_acp.sys, "platform", "win32")
    monkeypatch.setattr(cursor_acp.os, "name", "nt")
    session.close()
    assert calls, "Windows close must invoke taskkill for the owned ACP pid"
    assert calls[0][:2] == ["taskkill", "/PID"]
    assert calls[0][2] == str(proc.pid)
    assert "/T" in calls[0] and "/F" in calls[0]
    # Tree kill plus terminate (stdio unblock) — both expected on Windows.
    assert proc.terminate_calls >= 1
    # Clean close → further session/transport close must not taskkill again.
    calls.clear()
    before_term = proc.terminate_calls
    session.close()
    transport.close()
    assert calls == []
    assert proc.terminate_calls == before_term


def test_non_windows_close_does_not_taskkill(monkeypatch):
    proc, transport, session = _live_session()
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))

        class _R:
            returncode = 0

        return _R()

    monkeypatch.setattr(cursor_acp.subprocess, "run", fake_run)
    monkeypatch.setattr(cursor_acp.sys, "platform", "linux")
    monkeypatch.setattr(cursor_acp.os, "name", "posix")
    session.close()
    assert calls == []
    assert proc.terminate_calls >= 1


def test_reap_refuses_self_and_invalid_pids(monkeypatch):
    monkeypatch.setattr(cursor_acp.sys, "platform", "win32")
    monkeypatch.setattr(cursor_acp.os, "name", "nt")
    calls: list[list[str]] = []
    monkeypatch.setattr(
        cursor_acp.subprocess,
        "run",
        lambda cmd, **k: calls.append(list(cmd)),
    )
    assert _reap_acp_child_tree(None) is False
    assert _reap_acp_child_tree(0) is False
    assert _reap_acp_child_tree(1) is False
    assert _reap_acp_child_tree(cursor_acp.os.getpid()) is False
    assert calls == []


def test_owner_hooks_session_switch_interrupt_shutdown_close(monkeypatch):
    proc = _FakeProc()
    transport = AcpTransport(proc)
    session = WarmAcpSession(
        model="m", cwd="C:\\ws", transport_factory=lambda: transport
    )
    session.ensure()
    drv = CursorAcpDriver(
        name="cursor-cli:m",
        model="m",
        session=session,
        fallback=type("F", (), {"_run_stream": staticmethod(lambda *a, **k: None)})(),  # type: ignore[arg-type]
    )
    monkeypatch.setattr(cursor_acp.sys, "platform", "linux")
    closed: list[str] = []
    real_close = session.close

    def track_close():
        closed.append("close")
        real_close()

    monkeypatch.setattr(session, "close", track_close)
    drv.on_session_switch()
    drv.on_interrupt()
    drv.on_shutdown()
    # First hook closes; later hooks stay idempotent (still call close, which is no-op).
    assert closed == ["close", "close", "close"]
    assert session.transport is None


def test_workspace_change_closes_only_when_root_differs(tmp_path):
    ws_a = tmp_path / "ws-a"
    ws_b = tmp_path / "ws-b"
    ws_a.mkdir()
    ws_b.mkdir()
    proc = _FakeProc()
    transport = AcpTransport(proc)
    session = WarmAcpSession(
        model="m", cwd=str(ws_a), transport_factory=lambda: transport
    )
    session.ensure()
    drv = CursorAcpDriver(
        name="cursor-cli:m",
        model="m",
        session=session,
        fallback=type("F", (), {"_run_stream": staticmethod(lambda *a, **k: None)})(),  # type: ignore[arg-type]
    )
    # Same root → keep warm session.
    drv.on_workspace_change(str(ws_a))
    assert session.transport is transport
    assert session.session_id == "sess-warm-1"
    # Different root → close/reap so next ensure respawns.
    drv.on_workspace_change(str(ws_b))
    assert session.transport is None
    assert session.session_id is None
    assert session.cwd is not None
    assert str(ws_b.resolve()) == session.cwd


def test_release_owned_warm_acp_routes_reasons():
    hits: list[str] = []

    class _Pilot:
        def on_session_switch(self):
            hits.append("switch")

        def on_interrupt(self):
            hits.append("interrupt")

        def on_shutdown(self):
            hits.append("shutdown")

        def on_workspace_change(self, cwd=None):
            hits.append(f"workspace:{cwd}")

    owner = type("Owner", (), {})()
    owner.pilot = _Pilot()
    owner.config = type("C", (), {"repo": "C:\\live"})()
    release_owned_warm_acp(owner, reason="session_switch")
    release_owned_warm_acp(owner, reason="interrupt")
    release_owned_warm_acp(owner, reason="shutdown")
    release_owned_warm_acp(owner, reason="workspace")
    release_owned_warm_acp(owner, reason="workspace", cwd="C:\\override")
    assert hits == [
        "switch",
        "interrupt",
        "shutdown",
        "workspace:C:\\live",
        "workspace:C:\\override",
    ]


def test_no_action_after_clean_close_on_windows(monkeypatch):
    """After a clean close, further close/reap must not signal again."""
    proc, transport, session = _live_session()
    calls: list[list[str]] = []
    monkeypatch.setattr(
        cursor_acp.subprocess,
        "run",
        lambda cmd, **k: calls.append(list(cmd)),
    )
    monkeypatch.setattr(cursor_acp.sys, "platform", "win32")
    monkeypatch.setattr(cursor_acp.os, "name", "nt")
    session.close()
    assert len(calls) == 1
    calls.clear()
    before_term = proc.terminate_calls
    # Clean close: transport already closed; WarmAcpSession holds no transport.
    session.close()
    CursorAcpDriver(
        name="n",
        model="m",
        session=session,
        fallback=type("F", (), {"_run_stream": staticmethod(lambda *a, **k: None)})(),  # type: ignore[arg-type]
    ).close()
    assert calls == []
    assert proc.terminate_calls == before_term
