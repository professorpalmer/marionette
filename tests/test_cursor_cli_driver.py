"""Cursor CLI driver: stream-json parse + mocked subprocess (no live agent)."""

from __future__ import annotations

import io
import json

import pytest

from pmharness.drivers.cursor_cli import (
    CursorCliDriver,
    consume_stream_json,
    resolve_agent_binary,
)


def test_consume_partial_deltas_and_skip_flushes():
    lines = [
        json.dumps({
            "type": "system", "subtype": "init",
            "session_id": "s1", "model": "sonnet-4",
        }),
        json.dumps({
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Hel"}]},
            "timestamp_ms": 1,
        }),
        json.dumps({
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "lo"}]},
            "timestamp_ms": 2,
        }),
        # buffered flush before tool — skip (has model_call_id)
        json.dumps({
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Hello"}]},
            "timestamp_ms": 3,
            "model_call_id": "mc1",
        }),
        json.dumps({
            "type": "tool_call",
            "subtype": "started",
            "call_id": "c1",
            "tool_call": {"readToolCall": {"args": {"path": "a.py"}}},
        }),
        # final full message without timestamp — skip when we already streamed
        json.dumps({
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Hello"}]},
        }),
        json.dumps({
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "Hello",
            "session_id": "s1",
            "usage": {"input_tokens": 2, "output_tokens": 1},
        }),
    ]
    deltas = []
    hints = []
    parsed = consume_stream_json(
        lines,
        on_delta=deltas.append,
        on_tool_hint=hints.append,
        expect_partial=True,
    )
    assert deltas == ["Hel", "lo"]
    assert parsed["text"] == "Hello"
    assert len(hints) == 1
    assert hints[0]["name"] == "read_file"
    assert hints[0]["goal"] == "a.py"
    assert hints[0]["id"] == "c1"
    assert hints[0]["status"] == "in_progress"
    assert parsed["tool_calls"][0]["function"]["name"] == "readToolCall"
    assert json.loads(parsed["tool_calls"][0]["function"]["arguments"])["path"] == "a.py"
    assert parsed["session_id"] == "s1"
    assert parsed["error"] is None


def test_consume_tool_hint_unwraps_generic_tool_key():
    lines = [
        json.dumps({
            "type": "tool_call",
            "subtype": "started",
            "call_id": "c9",
            "tool_call": {
                "tool": {"name": "Shell", "args": {"command": "ls"}},
            },
        }),
        json.dumps({
            "type": "tool_call",
            "subtype": "completed",
            "call_id": "c9",
            "tool_call": {
                "tool": {"name": "Shell", "args": {"command": "ls"}},
            },
        }),
    ]
    hints = []
    consume_stream_json(lines, on_tool_hint=hints.append)
    assert hints[0]["name"] == "run_command"
    assert hints[0]["goal"] == "ls"
    assert hints[0]["status"] == "in_progress"
    assert hints[1]["status"] == "completed"
    assert hints[1]["id"] == "c9"


def test_consume_mcp_tool_hint_uses_server_and_tool_name():
    """mcpToolCall must not paint as 'Tool Call MCP: tool'."""
    lines = [
        json.dumps({
            "type": "tool_call",
            "subtype": "started",
            "call_id": "m1",
            "tool_call": {
                "mcpToolCall": {
                    "args": {
                        "serverIdentifier": "user-puppetmaster",
                        "toolName": "puppetmaster_status",
                    },
                },
            },
        }),
        json.dumps({
            "type": "tool_call",
            "subtype": "started",
            "call_id": "m2",
            "tool_call": {
                "mcpToolCall": {
                    "args": {
                        "providerIdentifier": "MCP",
                        "toolName": "tool",
                    },
                },
            },
        }),
    ]
    hints = []
    consume_stream_json(lines, on_tool_hint=hints.append)
    assert hints[0]["name"] == "call_mcp"
    assert hints[0]["goal"] == "user-puppetmaster/puppetmaster_status"
    assert hints[1]["name"] == "call_mcp"
    # Placeholder server/tool names are dropped — empty goal, not "MCP: tool".
    assert "goal" not in hints[1] or not hints[1].get("goal")


def test_consume_result_error():
    lines = [
        json.dumps({
            "type": "result",
            "is_error": True,
            "result": "not logged in",
        }),
    ]
    parsed = consume_stream_json(lines)
    assert parsed["error"] == "not logged in"


def test_driver_chat_stream_mocked_subprocess(monkeypatch, tmp_path):
    fake_bin = tmp_path / "agent"
    fake_bin.write_text("#!/bin/sh\n", encoding="utf-8")

    stream = "\n".join([
        json.dumps({
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "a"}]},
            "timestamp_ms": 1,
        }),
        json.dumps({
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "b"}]},
            "timestamp_ms": 2,
        }),
        json.dumps({
            "type": "result",
            "is_error": False,
            "result": "ab",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }),
        "",
    ])

    class FakeProc:
        returncode = 0
        stdout = io.StringIO(stream)
        stderr = io.StringIO("")

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return FakeProc()

    monkeypatch.setattr("pmharness.drivers.cursor_cli.subprocess.Popen", fake_popen)

    d = CursorCliDriver(
        name="cursor-cli:auto",
        model="auto",
        agent_binary=str(fake_bin),
    )
    assert d.supports_streaming is True
    deltas = []
    resp = d.chat_stream(
        [{"role": "user", "content": "hi"}],
        on_delta=deltas.append,
    )
    assert resp.error is None
    assert deltas == ["a", "b"]
    assert resp.text == "ab"
    assert resp.meta.get("pool_rotate") is False
    # Cursor-native tool names must never re-enter Marionette's dispatcher.
    assert resp.meta.get("tool_calls") == []
    assert "--print" in captured["cmd"]
    assert "--trust" in captured["cmd"]
    assert "stream-json" in captured["cmd"]
    assert "--stream-partial-output" in captured["cmd"]
    assert "--model" in captured["cmd"]
    assert "auto" in captured["cmd"]
    assert "--mode" in captured["cmd"]
    assert "ask" in captured["cmd"]
    # Short prompts stay on argv (node+index.js spawn; no PowerShell 8k trap).
    joined = " ".join(str(x) for x in captured["cmd"])
    assert "hi" in joined
    assert resp.meta.get("prompt_via_file") is not True
    # Kernel system — not Marionette's skills dump.
    assert "CodeGraph" in joined or "puppetmaster codegraph" in joined


def test_long_prompt_never_in_argv(monkeypatch, tmp_path):
    fake_bin = tmp_path / "agent.exe"
    fake_bin.write_text("x", encoding="utf-8")
    stream = "\n".join([
        json.dumps({
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
            "timestamp_ms": 1,
        }),
        json.dumps({"type": "result", "is_error": False, "result": "ok"}),
        "",
    ])

    class FakeProc:
        returncode = 0
        stdout = io.StringIO(stream)
        stderr = io.StringIO("")

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return FakeProc()

    monkeypatch.setattr("pmharness.drivers.cursor_cli.subprocess.Popen", fake_popen)
    huge = "x" * 20_000
    d = CursorCliDriver(name="cursor-cli:m", model="composer-2.5", agent_binary=str(fake_bin))
    resp = d.complete(huge)
    assert resp.error is None
    assert huge not in " ".join(str(x) for x in captured["cmd"])
    assert resp.meta.get("prompt_via_file") is True
    # Spill pointer keeps the ask inline and forbids tool-reading the file.
    joined = " ".join(str(x) for x in captured["cmd"])
    assert "Do NOT use read/grep" in joined or "pmh-cursor-cli-" in joined


def test_ask_mode_leans_history():
    from pmharness.drivers.cursor_cli import _messages_to_prompt

    msgs = [
        {"role": "user", "content": "old " * 5_000},
        {"role": "assistant", "content": "ack"},
        {"role": "user", "content": "who am I talking to?"},
    ]
    huge_sys = "SKILLS\n" + ("x" * 20_000)
    lean = _messages_to_prompt(msgs, huge_sys, lean=True)
    assert "who am I talking to?" in lean
    assert "SKILLS" not in lean  # skills dump stripped
    assert "puppetmaster codegraph" in lean.lower() or "CodeGraph" in lean
    assert len(lean) < 20_000


def test_system_keeps_codegraph_addendum_only():
    from pmharness.drivers.cursor_cli import _system_for_cursor_agent

    sys = (
        "long pilot preamble " + ("z" * 5000) + "\n\n"
        "CODEGRAPH HAS ALREADY BEEN QUERIED FOR THIS TASK.\nsymbols: Foo\n\n"
        "more noise"
    )
    out = _system_for_cursor_agent(sys)
    assert "CODEGRAPH HAS ALREADY BEEN QUERIED" in out
    assert "long pilot preamble" not in out
    assert "more noise" not in out


def test_driver_drops_cursor_native_tool_calls(monkeypatch, tmp_path):
    """readToolCall/grepToolCall stay internal — not Marionette native tools."""
    fake_bin = tmp_path / "agent.exe"
    fake_bin.write_text("x", encoding="utf-8")
    stream = "\n".join([
        json.dumps({
            "type": "tool_call",
            "subtype": "started",
            "call_id": "c1",
            "tool_call": {"readToolCall": {"args": {"path": "a.py"}}},
        }),
        json.dumps({
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
            "timestamp_ms": 1,
        }),
        json.dumps({"type": "result", "is_error": False, "result": "hi"}),
        "",
    ])

    class FakeProc:
        returncode = 0
        stdout = io.StringIO(stream)
        stderr = io.StringIO("")

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr(
        "pmharness.drivers.cursor_cli.subprocess.Popen",
        lambda *a, **k: FakeProc(),
    )
    d = CursorCliDriver(name="cursor-cli:m", model="composer-2.5", agent_binary=str(fake_bin))
    resp = d.chat_stream([{"role": "user", "content": "hi"}], on_delta=lambda _d: None)
    assert resp.meta.get("tool_calls") == []
    assert "readToolCall" in (resp.meta.get("cursor_cli_internal_tools") or [])


def test_resolve_agent_exec_prefers_node_index(tmp_path):
    from pmharness.drivers.cursor_cli import resolve_agent_exec

    root = tmp_path / "cursor-agent"
    ver = root / "versions" / "2026.07.09-deadbeef"
    ver.mkdir(parents=True)
    (ver / "node.exe").write_text("", encoding="utf-8")
    (ver / "index.js").write_text("", encoding="utf-8")
    cmd = root / "agent.cmd"
    cmd.write_text("@echo off\n", encoding="utf-8")
    exec_argv = resolve_agent_exec(str(cmd))
    assert exec_argv[0].endswith("node.exe")
    assert exec_argv[1].endswith("index.js")


def test_build_cmd_passes_trust_and_workspace(tmp_path):
    fake_bin = tmp_path / "agent.exe"
    fake_bin.write_text("x", encoding="utf-8")
    ws = tmp_path / "proj"
    ws.mkdir()
    d = CursorCliDriver(
        name="cursor-cli:m",
        model="composer-2.5",
        agent_binary=str(fake_bin),
        cwd=str(ws),
    )
    cmd = d._build_cmd("hi")
    assert "--trust" in cmd
    assert "--approve-mcps" in cmd
    assert "--workspace" in cmd
    assert str(ws.resolve()) in cmd


def test_kernel_steers_mcp_before_shell_codegraph():
    from pmharness.drivers.cursor_cli import _CURSOR_CLI_KERNEL_SYSTEM

    k = _CURSOR_CLI_KERNEL_SYSTEM.lower()
    assert "puppetmaster_codegraph" in k
    assert "mcp" in k
    assert "query_wiki" in k or "search_wiki" in k
    assert "finding" in k or "plumbing" in k
    # Shell remains a fallback, not the only path.
    assert "python -m puppetmaster codegraph" in k


def test_driver_missing_binary_errors(monkeypatch):
    monkeypatch.setattr(
        "pmharness.drivers.cursor_cli.resolve_agent_binary",
        lambda: None,
    )
    d = CursorCliDriver(name="cursor-cli:auto", model="auto", agent_binary=None)
    resp = d.complete("ping")
    assert resp.error
    assert "not found" in resp.error.lower() or "Install" in resp.error


def test_resolve_agent_binary_prefers_which(monkeypatch, tmp_path):
    agent = tmp_path / "agent.exe"
    agent.write_text("x", encoding="utf-8")
    monkeypatch.setattr(
        "pmharness.drivers.cursor_cli.shutil.which",
        lambda name: str(agent) if name == "agent" else None,
    )
    assert resolve_agent_binary() == str(agent)


def test_no_pool_rotate_helpers():
    """Cursor CLI must not wire CredentialPool bearer rotate."""
    import pmharness.drivers.cursor_cli as mod
    src = open(mod.__file__, encoding="utf-8").read()
    assert "_pool_rotate_on_http_error" not in src
    assert "report_failure" not in src
    assert "resolve_entry" not in src
