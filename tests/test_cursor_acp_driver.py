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
    _cursor_acp_terminal_fields,
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
        self.set_mode_calls: list[str] = []
        # When set, session/set_mode replies with this JSON-RPC error payload.
        self.set_mode_error: Optional[dict] = None
        # session/prompt result. None omits stopReason (protocol close only).
        self.stop_reason: Optional[str] = "end_turn"
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
            elif method == "session/set_mode":
                mode_id = str((msg.get("params") or {}).get("modeId") or "")
                self.set_mode_calls.append(mode_id)
                if self.set_mode_error is not None:
                    self._reply_error(mid, self.set_mode_error)
                else:
                    self._reply(mid, {})
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
                result = {
                    "usage": {"inputTokens": 120, "outputTokens": 8},
                }
                if self.stop_reason is not None:
                    result["stopReason"] = self.stop_reason
                self._reply(mid, result)
            elif method == "initialized":
                continue
            elif mid is not None:
                self._reply(mid, {})

    def _reply(self, mid, result) -> None:
        self.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}) + "\n")

    def _reply_error(self, mid, error) -> None:
        self.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "error": error}) + "\n")

    def _notify(self, method: str, params: dict) -> None:
        self.stdout.write(
            json.dumps({"jsonrpc": "2.0", "method": method, "params": params}) + "\n"
        )


def test_cursor_acp_enabled_default(monkeypatch):
    monkeypatch.delenv("HARNESS_CURSOR_ACP", raising=False)
    assert cursor_acp_enabled() is False
    assert cursor_acp_enabled("auto") is False
    monkeypatch.setenv("HARNESS_CURSOR_ACP", "1")
    assert cursor_acp_enabled() is True
    assert cursor_acp_enabled("auto") is True
    assert cursor_acp_enabled("") is True
    assert cursor_acp_enabled("claude-fable-5-high") is False
    monkeypatch.setenv("HARNESS_CURSOR_ACP", "0")
    assert cursor_acp_enabled() is False
    assert cursor_acp_enabled("auto") is False


def test_cursor_acp_refuses_explicit_model_even_when_opted_in(monkeypatch):
    """Opt-in ACP is only for auto/empty — explicit pins must use --print."""
    monkeypatch.setenv("HARNESS_CURSOR_ACP", "1")
    assert cursor_acp_enabled("claude-fable-5-high") is False
    assert cursor_acp_enabled("gpt-5.6-luna-medium") is False
    assert cursor_acp_enabled("auto") is True

    class Fallback:
        def _run_stream(self, *a, **k):
            from pmharness.drivers.base import DriverResponse
            return DriverResponse(text="print-path", model="cursor-cli:fable")

    class MustNotPrompt(WarmAcpSession):
        def prompt(self, *a, **k):
            raise AssertionError("ACP must not run for explicit picker models")

    drv = CursorAcpDriver(
        name="cursor-cli:claude-fable-5-high",
        model="claude-fable-5-high",
        session=MustNotPrompt(model="claude-fable-5-high", cwd=None),
        fallback=Fallback(),  # type: ignore[arg-type]
    )
    resp = drv.chat_stream(
        [{"role": "user", "content": "hi"}],
        on_delta=lambda _t: None,
    )
    assert resp.text == "print-path"


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


def test_warm_session_reuses_process_across_prompts(monkeypatch):
    monkeypatch.delenv("HARNESS_CURSOR_CLI_MODE", raising=False)
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
    # Autopilot default → agent (not ask).
    assert "agent" in proc.set_mode_calls
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
        name="cursor-cli:auto",
        model="auto",
        session=BoomSession(model="auto", cwd=None),
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
        model="auto",
        cwd="C:\\ws",
        transport_factory=lambda: transport,
    )

    class NoFallback:
        def _run_stream(self, *a, **k):
            raise AssertionError("must not fall back")

    drv = CursorAcpDriver(
        name="cursor-cli:auto",
        model="auto",
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
    assert resp.error is None
    assert resp.meta.get("cursor_acp") is True
    assert resp.meta.get("billing") == "plan"
    assert resp.meta.get("requested_model") == "auto"
    assert resp.meta.get("identity_status") == "auto"
    assert resp.meta.get("tool_calls") == []
    assert resp.meta.get("finish_reason") == "completed"
    assert resp.meta.get("stream_terminal") == "stop"
    assert resp.meta.get("stream_started") is True
    assert resp.meta.get("api_mode") == "cursor_acp"
    assert resp.meta.get("wire_mode") == "cursor_acp"
    assert resp.meta.get("last_provider_event") == "session/prompt"
    assert resp.meta.get("stop_reason") == "end_turn"
    assert resp.tokens_in == 120
    assert resp.tokens_out == 8
    assert deltas == ["pong", "-ok"]
    assert drv.requires_explicit_terminal is True
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


def test_acp_apply_host_mode_plan_uses_ask(monkeypatch):
    monkeypatch.delenv("HARNESS_CURSOR_CLI_MODE", raising=False)

    class _Fallback:
        def __init__(self) -> None:
            self.mode = "agent"
            self._mode_override = None

        def apply_host_mode(self, *, plan: bool = False) -> str:
            from pmharness.drivers.cursor_cli import resolve_cursor_execution_mode
            self.mode = resolve_cursor_execution_mode(
                plan=plan, explicit=self._mode_override,
            )
            return self.mode

        def _run_stream(self, *a, **k):
            return None

    proc = _FakeProc()
    transport = AcpTransport(proc)
    session = WarmAcpSession(
        model="m", cwd="C:\\ws", transport_factory=lambda: transport,
    )
    session.ensure()
    assert session.mode == "agent"
    fallback = _Fallback()
    drv = CursorAcpDriver(
        name="cursor-cli:m",
        model="m",
        session=session,
        fallback=fallback,  # type: ignore[arg-type]
    )
    assert drv.apply_host_mode(plan=True) == "ask"
    assert session.mode == "ask"
    assert "ask" in proc.set_mode_calls
    assert drv.apply_host_mode(plan=False) == "agent"
    assert session.mode == "agent"


def test_acp_constructor_mode_sticky_and_env_wins(monkeypatch):
    monkeypatch.delenv("HARNESS_CURSOR_CLI_MODE", raising=False)
    sticky = CursorAcpDriver(name="n", model="m", mode="ask")
    assert sticky.mode == "ask"
    assert sticky._mode_override == "ask"
    assert sticky._session._mode_override == "ask"
    assert sticky._fallback._mode_override == "ask"
    assert sticky.apply_host_mode(plan=False) == "ask"
    assert sticky._session.mode == "ask"
    assert sticky._fallback.mode == "ask"

    monkeypatch.setenv("HARNESS_CURSOR_CLI_MODE", "plan")
    env_wins = CursorAcpDriver(name="n", model="m", mode="ask")
    assert env_wins.mode == "plan"
    assert env_wins.apply_host_mode(plan=False) == "plan"
    monkeypatch.delenv("HARNESS_CURSOR_CLI_MODE", raising=False)


def test_acp_set_mode_error_fails_handshake(monkeypatch):
    """Handshake must not claim success when session/set_mode returns error."""
    monkeypatch.delenv("HARNESS_CURSOR_CLI_MODE", raising=False)
    proc = _FakeProc()
    proc.set_mode_error = {"code": -32000, "message": "unknown mode"}
    transport = AcpTransport(proc)
    session = WarmAcpSession(
        model="m", cwd="C:\\ws", transport_factory=lambda: transport,
    )
    with pytest.raises(RuntimeError, match="session/set_mode failed"):
        session.ensure()
    assert session.session_id is None
    assert session.transport is None


def test_acp_live_set_mode_failure_invalidates_session(monkeypatch):
    """Live mode-switch failure must close the stale ACP session."""
    monkeypatch.delenv("HARNESS_CURSOR_CLI_MODE", raising=False)

    class _Fallback:
        def __init__(self) -> None:
            self.mode = "agent"
            self._mode_override = None

        def apply_host_mode(self, *, plan: bool = False) -> str:
            from pmharness.drivers.cursor_cli import resolve_cursor_execution_mode
            self.mode = resolve_cursor_execution_mode(
                plan=plan, explicit=self._mode_override,
            )
            return self.mode

        def _run_stream(self, *a, **k):
            return None

    proc = _FakeProc()
    transport = AcpTransport(proc)
    session = WarmAcpSession(
        model="m", cwd="C:\\ws", transport_factory=lambda: transport,
    )
    session.ensure()
    assert session.session_id == "sess-warm-1"
    # Reject the next set_mode (Plan → ask).
    proc.set_mode_error = {"code": -32000, "message": "mode rejected"}
    fallback = _Fallback()
    drv = CursorAcpDriver(
        name="cursor-cli:m",
        model="m",
        session=session,
        fallback=fallback,  # type: ignore[arg-type]
    )
    # Driver swallows; session must still be invalidated and CLI keep ask.
    assert drv.apply_host_mode(plan=True) == "ask"
    assert session.session_id is None
    assert session.transport is None
    assert fallback.mode == "ask"


def _acp_driver(monkeypatch, *, stop_reason="end_turn"):
    """Production-shaped warm ACP driver with a mocked stdio agent."""
    proc = _FakeProc()
    proc.stop_reason = stop_reason
    transport = AcpTransport(proc)
    session = WarmAcpSession(
        model="auto",
        cwd="C:\\ws",
        transport_factory=lambda: transport,
    )

    class NoFallback:
        def _run_stream(self, *a, **k):
            raise AssertionError("must not fall back")

    monkeypatch.setenv("HARNESS_CURSOR_ACP", "1")
    drv = CursorAcpDriver(
        name="cursor-cli:auto",
        model="auto",
        session=session,
        fallback=NoFallback(),  # type: ignore[arg-type]
    )
    return drv


@pytest.mark.parametrize(
    "stop, finish, terminal, has_error",
    [
        ("end_turn", "completed", "stop", False),
        ("end-turn", "completed", "stop", False),
        ("stop", "completed", "stop", False),
        ("completed", "completed", "stop", False),
        ("", "completed", "stop", False),
        (None, "completed", "stop", False),
        ("max_tokens", "length", "length", True),
        ("max-tokens", "length", "length", True),
        ("length", "length", "length", True),
        ("max_turn_requests", "incomplete", "incomplete", True),
        ("incomplete", "incomplete", "incomplete", True),
        ("refusal", "content_filter", "content_filter", True),
        ("content_filter", "content_filter", "content_filter", True),
        ("cancelled", "cancelled", "cancelled", True),
        ("canceled", "cancelled", "cancelled", True),
        ("error", "failed", "error", True),
        ("failed", "failed", "error", True),
        ("mystery_code", "mystery_code", "incomplete", True),
    ],
)
def test_cursor_acp_terminal_fields_map_stop_reasons(stop, finish, terminal, has_error):
    meta, err = _cursor_acp_terminal_fields(stop, stream_started=True)
    assert meta["finish_reason"] == finish
    assert meta["stream_terminal"] == terminal
    assert meta["stream_started"] is True
    assert meta["api_mode"] == "cursor_acp"
    assert meta["wire_mode"] == "cursor_acp"
    assert meta["last_provider_event"] == "session/prompt"
    if has_error:
        assert err
        assert meta["finish_reason"] not in ("completed", "stop")
        assert meta["stream_terminal"] != "stop"
    else:
        assert err is None
        assert meta["finish_reason"] == "completed"
        assert meta["stream_terminal"] == "stop"


def test_cursor_acp_terminal_fields_reads_result_stop_reason():
    meta, err = _cursor_acp_terminal_fields(
        None, result={"stopReason": "end_turn"}, stream_started=True,
    )
    assert err is None
    assert meta["finish_reason"] == "completed"
    assert meta["stop_reason"] == "end_turn"


def test_cursor_acp_missing_stop_reason_stamps_completed_not_text(monkeypatch):
    """Successful session/prompt with no stopReason uses the protocol event."""
    drv = _acp_driver(monkeypatch, stop_reason=None)
    resp = drv.chat([{"role": "user", "content": "who are you?"}])
    assert resp.error is None
    assert resp.text == "pong-ok"
    assert resp.meta["finish_reason"] == "completed"
    assert resp.meta["stream_terminal"] == "stop"
    assert resp.meta["stream_started"] is True
    assert resp.meta["api_mode"] == "cursor_acp"
    assert resp.meta["wire_mode"] == "cursor_acp"
    assert resp.meta["last_provider_event"] == "session/prompt"
    assert resp.meta["tool_calls"] == []
    drv.close()


def test_cursor_acp_max_tokens_is_length_not_natural(monkeypatch):
    drv = _acp_driver(monkeypatch, stop_reason="max_tokens")
    resp = drv.chat_stream(
        [{"role": "user", "content": "write a lot"}],
        on_delta=lambda _t: None,
    )
    assert resp.error
    assert "max_tokens" in resp.error
    assert resp.text == "pong-ok"
    assert resp.meta["finish_reason"] == "length"
    assert resp.meta["stream_terminal"] == "length"
    assert resp.meta["stop_reason"] == "max_tokens"
    assert resp.meta["wire_mode"] == "cursor_acp"
    assert resp.meta["tool_calls"] == []
    drv.close()


def test_cursor_acp_cancelled_and_error_are_not_natural(monkeypatch):
    cancelled = _acp_driver(monkeypatch, stop_reason="cancelled")
    c_resp = cancelled.chat([{"role": "user", "content": "hi"}])
    assert c_resp.error
    assert c_resp.meta["finish_reason"] == "cancelled"
    assert c_resp.meta["stream_terminal"] == "cancelled"
    assert c_resp.meta["finish_reason"] not in ("completed", "stop")
    assert c_resp.meta["tool_calls"] == []
    cancelled.close()

    failed = _acp_driver(monkeypatch, stop_reason="error")
    e_resp = failed.chat([{"role": "user", "content": "hi"}])
    assert e_resp.error
    assert e_resp.meta["finish_reason"] == "failed"
    assert e_resp.meta["stream_terminal"] == "error"
    assert e_resp.meta["stream_terminal"] != "stop"
    assert e_resp.meta["tool_calls"] == []
    failed.close()


def test_cursor_acp_unrecognized_stop_reason_fails_closed(monkeypatch):
    drv = _acp_driver(monkeypatch, stop_reason="mystery_code")
    resp = drv.chat([{"role": "user", "content": "hi"}])
    assert resp.error
    assert "unrecognized" in resp.error
    assert resp.meta["finish_reason"] == "mystery_code"
    assert resp.meta["stream_terminal"] == "incomplete"
    assert resp.meta["finish_reason"] not in ("completed", "stop")
    assert resp.meta["stream_terminal"] != "stop"
    assert resp.meta["tool_calls"] == []
    drv.close()


def test_cursor_acp_prose_send_emits_natural_and_records_stream_wire(
    tmp_path, monkeypatch,
):
    """Successful ACP prose finalizes and receipts record streamed ACP wire."""
    from harness.config import HarnessConfig
    from harness.conversation import ConversationalSession
    from harness.stream_performance_store import StreamPerformanceReceiptStore

    monkeypatch.setattr(
        "harness.send_loop.profile_skips_auto_inject",
        lambda session: (True, True),
    )
    drv = _acp_driver(monkeypatch)
    cfg = HarnessConfig(
        driver="cursor-cli:auto",
        state_dir=str(tmp_path),
        repo=str(tmp_path),
    )
    session = ConversationalSession(cfg)
    session.harness_session_id = "sess-acp"
    session.pilot = drv
    monkeypatch.setattr(session, "_resolve_append_only", lambda: False)
    monkeypatch.setattr(session, "_get_codegraph_context", lambda msg: "")
    monkeypatch.setattr(session, "_maybe_compact_history", lambda **k: iter(()))
    events = list(session.send("who are you?"))
    done = next(e for e in events if e.kind == "assistant_done")
    assert done.data.get("stop_cause") == "natural"
    assert done.data.get("finish_reason") == "completed"
    assert not any(e.kind == "error" for e in events)
    rows = StreamPerformanceReceiptStore(str(tmp_path)).list_receipts("sess-acp")
    assert rows
    assert rows[0]["wire_mode"] == "cursor_acp"
    assert rows[0]["api_mode"] == "cursor_acp"
    assert rows[0]["stream_started"] is True
    assert rows[0]["terminal_cause"] == "natural"
    drv.close()


def test_cursor_acp_incomplete_send_does_not_finalize_or_run_tools(
    tmp_path, monkeypatch,
):
    from harness.config import HarnessConfig
    from harness.conversation import ConversationalSession

    monkeypatch.setattr(
        "harness.send_loop.profile_skips_auto_inject",
        lambda session: (True, True),
    )

    def boom(*_a, **_k):
        raise AssertionError("execute_turn_actions must not run")

    monkeypatch.setattr("harness.send_loop.execute_turn_actions", boom)
    drv = _acp_driver(monkeypatch, stop_reason="max_tokens")
    cfg = HarnessConfig(
        driver="cursor-cli:auto",
        state_dir=str(tmp_path),
        repo=str(tmp_path),
    )
    session = ConversationalSession(cfg)
    session.harness_session_id = "sess-acp-len"
    session.pilot = drv
    monkeypatch.setattr(session, "_resolve_append_only", lambda: False)
    monkeypatch.setattr(session, "_get_codegraph_context", lambda msg: "")
    monkeypatch.setattr(session, "_maybe_compact_history", lambda **k: iter(()))
    events = list(session.send("write a long essay"))
    kinds = [e.kind for e in events]
    assert "assistant_done" not in kinds
    err = next(e for e in events if e.kind == "error")
    assert err.data.get("terminal_cause") == "length"
    assert err.data.get("finish_reason") == "length"
    drv.close()
