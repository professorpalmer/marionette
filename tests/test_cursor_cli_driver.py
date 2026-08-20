"""Cursor CLI driver: stream-json parse + mocked subprocess (no live agent)."""

from __future__ import annotations

import io
import json
import os

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


def test_consume_stream_json_keeps_usage_from_non_result_events():
    """Cursor sometimes emits usage before the terminal result event."""
    lines = [
        json.dumps({
            "type": "assistant",
            "timestamp_ms": 1,
            "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
            "usage": {"inputTokens": 50, "outputTokens": 3, "cacheReadTokens": 200},
        }),
        json.dumps({
            "type": "result",
            "is_error": False,
            "result": "hi",
        }),
    ]
    parsed = consume_stream_json(lines, expect_partial=True)
    assert parsed["usage"]["inputTokens"] == 50
    assert parsed["usage"]["cacheReadTokens"] == 200


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


def test_consume_tool_hint_failed_status_carries_call_id():
    lines = [
        json.dumps({
            "type": "tool_call",
            "subtype": "started",
            "call_id": "c-fail",
            "tool_call": {
                "tool": {"name": "Read", "args": {"path": "x.py"}},
            },
        }),
        json.dumps({
            "type": "tool_call",
            "subtype": "failed",
            "call_id": "c-fail",
            "tool_call": {
                "tool": {"name": "Read", "args": {"path": "x.py"}},
            },
        }),
    ]
    hints = []
    consume_stream_json(lines, on_tool_hint=hints.append)
    assert hints[-1]["id"] == "c-fail"
    assert hints[-1]["status"] == "failed"


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


def test_consume_stream_json_accumulates_thinking_into_reasoning():
    lines = [
        json.dumps({
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Looking up."}],
            },
            "timestamp_ms": 1,
        }),
        json.dumps({"type": "thinking", "text": "Schedules was last. "}),
        json.dumps({
            "type": "thinking",
            "text": "No named section remains queued.",
        }),
        json.dumps({
            "type": "result",
            "is_error": False,
            "result": "Looking up.",
        }),
    ]
    thoughts: list[str] = []
    parsed = consume_stream_json(lines, on_reasoning_delta=thoughts.append)
    assert thoughts == [
        "Schedules was last. ",
        "No named section remains queued.",
    ]
    assert parsed["reasoning"] == (
        "Schedules was last. No named section remains queued."
    )


def test_driver_chat_stream_mocked_subprocess(monkeypatch, tmp_path):
    monkeypatch.delenv("HARNESS_CURSOR_CLI_MODE", raising=False)
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
    # Autopilot default is agent (Marionette owns host chrome).
    mode_idx = captured["cmd"].index("--mode")
    assert captured["cmd"][mode_idx + 1] == "agent"
    # Short prompts stay on argv (node+index.js spawn; no PowerShell 8k trap).
    joined = " ".join(str(x) for x in captured["cmd"])
    assert "hi" in joined
    assert resp.meta.get("prompt_via_file") is not True
    # Kernel system — not Marionette's skills dump.
    assert "CodeGraph" in joined or "puppetmaster codegraph" in joined
    assert "HOST MODE CONTRACT" in joined


def test_agent_child_env_puts_harness_python_first(monkeypatch, tmp_path):
    """Shell `python -m puppetmaster swarm` must see Marionette's venv, not a
    stale system install missing the swarm verb."""
    from pmharness.drivers.cursor_cli import _agent_child_env
    import sys
    from pathlib import Path

    scripts = str(Path(sys.executable).resolve().parent)
    monkeypatch.setenv("PATH", f"C:\\stale-python{os.pathsep}C:\\Windows\\System32")
    env = _agent_child_env()
    parts = (env.get("PATH") or "").split(os.pathsep)
    assert parts[0] == scripts
    assert env.get("HARNESS_PYTHON") == sys.executable
    assert env.get("MARIONETTE_TRACKABLE_SWARMS") == "1"

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
        captured["env"] = kwargs.get("env")
        return FakeProc()

    monkeypatch.setattr("pmharness.drivers.cursor_cli.subprocess.Popen", fake_popen)
    d = CursorCliDriver(name="cursor-cli:m", model="composer-2.5", agent_binary=str(fake_bin))
    d.chat_stream([{"role": "user", "content": "hi"}], on_delta=lambda _t: None)
    assert captured.get("env") is not None
    path0 = (captured["env"].get("PATH") or "").split(os.pathsep)[0]
    assert path0 == scripts


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


def test_system_preserves_cache_matrix_fair_protocol_blocks():
    from pmharness.drivers.cursor_cli import _system_for_cursor_agent

    fair = (
        "You are Marionette cache-matrix bench pilot.\n"
        "Stable marker: marionette_cache_matrix_stable_v1\n"
        "\n\n"
        "# Tool manifest (schema only; native loop differs)\n"
        '[{"type":"function","function":{"name":"cache_matrix_noop"}}]\n'
        "\n\n"
        "You are in Marionette Plan mode. Click Autopilot.\n"
    )
    out = _system_for_cursor_agent(fair)
    assert "marionette_cache_matrix_stable_v1" in out
    assert "# Tool manifest (schema only; native loop differs)" in out
    assert "cache_matrix_noop" in out
    assert "Click Autopilot" not in out


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
    # Trackable swarms only: ban MCP start_*; shell swarm is the path.
    assert "never call puppetmaster_start" in k or "never call" in k and "start_" in k
    assert "python -m puppetmaster swarm" in k
    assert "start_cursor_swarm" not in k or "never" in k


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


def test_host_mode_contract_survives_system_for_cursor_agent():
    from pmharness.drivers.cursor_cli import (
        _CURSOR_CLI_KERNEL_SYSTEM,
        _system_for_cursor_agent,
    )

    assert "HOST MODE CONTRACT" in _CURSOR_CLI_KERNEL_SYSTEM
    assert "switch to Agent mode" in _CURSOR_CLI_KERNEL_SYSTEM
    # Marionette skills/mode chrome must be stripped; kernel contract stays.
    fat = (
        "You are in Marionette Plan mode. Click Autopilot to execute.\n\n"
        "CODEGRAPH HAS ALREADY BEEN QUERIED FOR THIS TASK.\nsymbols: Foo\n"
    )
    out = _system_for_cursor_agent(fat)
    assert "HOST MODE CONTRACT" in out
    assert "CODEGRAPH HAS ALREADY BEEN QUERIED" in out
    assert "Click Autopilot" not in out
    # No generated guidance path that tells users to flip IDE modes.
    assert "switch to Agent mode" in out  # negation clause in the contract
    lower = out.lower()
    assert "never tell the user" in lower or "never ask the user" in lower


def test_resolve_cursor_execution_mode_defaults_and_overrides(monkeypatch):
    from pmharness.drivers.cursor_cli import (
        CursorCliDriver,
        resolve_cursor_execution_mode,
    )

    monkeypatch.delenv("HARNESS_CURSOR_CLI_MODE", raising=False)
    assert resolve_cursor_execution_mode(plan=False) == "agent"
    assert resolve_cursor_execution_mode(plan=True) == "ask"
    monkeypatch.setenv("HARNESS_CURSOR_CLI_MODE", "plan")
    assert resolve_cursor_execution_mode(plan=False) == "plan"
    assert resolve_cursor_execution_mode(plan=True) == "plan"
    # Env beats constructor explicit.
    assert resolve_cursor_execution_mode(plan=False, explicit="ask") == "plan"
    monkeypatch.delenv("HARNESS_CURSOR_CLI_MODE", raising=False)
    assert resolve_cursor_execution_mode(plan=False, explicit="ask") == "ask"

    d = CursorCliDriver(name="cursor-cli:m", model="composer-2.5")
    assert d.mode == "agent"
    assert d.apply_host_mode(plan=True) == "ask"
    assert d.mode == "ask"
    assert d.apply_host_mode(plan=False) == "agent"
    assert d.mode == "agent"


def test_cursor_cli_constructor_mode_sticky_and_env_wins(monkeypatch):
    from pmharness.drivers.cursor_cli import CursorCliDriver

    monkeypatch.delenv("HARNESS_CURSOR_CLI_MODE", raising=False)
    sticky = CursorCliDriver(name="cursor-cli:m", model="m", mode="ask")
    assert sticky.mode == "ask"
    assert sticky._mode_override == "ask"
    # apply_host_mode must not erase the constructor override.
    assert sticky.apply_host_mode(plan=False) == "ask"
    assert sticky.mode == "ask"
    assert sticky.apply_host_mode(plan=True) == "ask"
    assert sticky._mode_override == "ask"

    monkeypatch.setenv("HARNESS_CURSOR_CLI_MODE", "plan")
    env_wins = CursorCliDriver(name="cursor-cli:m", model="m", mode="ask")
    assert env_wins.mode == "plan"
    assert env_wins.apply_host_mode(plan=False) == "plan"
    monkeypatch.delenv("HARNESS_CURSOR_CLI_MODE", raising=False)


def test_consume_stream_json_tool_event_order_preserves_call_id():
    """Cursor stream: prose → tool_call(call_id) → prose keeps structured hints."""
    lines = [
        json.dumps({
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Looking"}]},
            "timestamp_ms": 1,
        }),
        json.dumps({
            "type": "tool_call",
            "subtype": "started",
            "call_id": "c-order",
            "tool_call": {"readToolCall": {"args": {"path": "a.py"}}},
        }),
        json.dumps({
            "type": "tool_call",
            "subtype": "completed",
            "call_id": "c-order",
            "tool_call": {"readToolCall": {"args": {"path": "a.py"}}},
        }),
        json.dumps({
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": " done"}]},
            "timestamp_ms": 2,
        }),
        json.dumps({"type": "result", "is_error": False, "result": "Looking done"}),
        "",
    ]
    deltas: list[str] = []
    hints: list = []
    out = consume_stream_json(lines, on_delta=deltas.append, on_tool_hint=hints.append)
    assert "".join(deltas) == "Looking done"
    assert len(hints) >= 2
    assert hints[0]["id"] == "c-order"
    assert hints[0]["status"] == "in_progress"
    assert hints[1]["id"] == "c-order"
    assert hints[1]["status"] == "completed"
    assert out.get("error") is None


def _stream_with_session(native_chat_id: str, text: str = "ok") -> str:
    return "\n".join([
        json.dumps({
            "type": "system",
            "subtype": "init",
            "session_id": native_chat_id,
            "model": "composer-2.5",
        }),
        json.dumps({
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
            "timestamp_ms": 1,
        }),
        json.dumps({
            "type": "result",
            "is_error": False,
            "result": text,
            "session_id": native_chat_id,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }),
        "",
    ])


def _install_fake_agent(monkeypatch, tmp_path, streams):
    """Patch Popen to return successive FakeProc streams; capture cmd argv."""
    fake_bin = tmp_path / "agent"
    fake_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    captured = {"cmds": []}
    queue = list(streams)

    class FakeProc:
        def __init__(self, stream: str):
            self.returncode = 0
            self.stdout = io.StringIO(stream)
            self.stderr = io.StringIO("")

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    def fake_popen(cmd, **kwargs):
        captured["cmds"].append(list(cmd))
        if not queue:
            raise AssertionError("unexpected extra agent spawn")
        return FakeProc(queue.pop(0))

    monkeypatch.setattr("pmharness.drivers.cursor_cli.subprocess.Popen", fake_popen)
    driver = CursorCliDriver(
        name="cursor-cli:m",
        model="composer-2.5",
        agent_binary=str(fake_bin),
        cwd=str(tmp_path),
    )
    return driver, captured


def test_first_call_command_shape_has_no_resume(monkeypatch, tmp_path):
    d, captured = _install_fake_agent(
        monkeypatch, tmp_path, [_stream_with_session("native-chat-1")],
    )
    resp = d.chat_stream(
        [{"role": "user", "content": "hi"}],
        on_delta=lambda _t: None,
        session_id="marionette-sess-a",
    )
    assert resp.error is None
    cmd = captured["cmds"][0]
    assert "--resume" not in cmd
    assert "--continue" not in cmd
    assert "marionette-sess-a" not in cmd
    assert d._native_chat_id == "native-chat-1"


def test_second_call_resumes_with_native_chat_id(monkeypatch, tmp_path):
    d, captured = _install_fake_agent(
        monkeypatch,
        tmp_path,
        [
            _stream_with_session("native-chat-1", "one"),
            _stream_with_session("native-chat-1", "two"),
        ],
    )
    marionette_id = "marionette-sess-a"
    d.chat_stream(
        [{"role": "user", "content": "turn1"}],
        on_delta=lambda _t: None,
        session_id=marionette_id,
    )
    resp = d.chat_stream(
        [{"role": "user", "content": "turn2"}],
        on_delta=lambda _t: None,
        session_id=marionette_id,
    )
    assert resp.error is None
    second = captured["cmds"][1]
    assert "--resume" in second
    resume_idx = second.index("--resume")
    assert second[resume_idx + 1] == "native-chat-1"
    assert marionette_id not in second
    assert "--continue" not in second
    assert resp.meta.get("cursor_cli_resume") == "native-chat-1"


def test_resumed_prompt_contains_only_current_turn(monkeypatch, tmp_path):
    """On --resume, argv prompt is the new turn only — not prior history/system."""
    from pmharness.drivers.cursor_cli import _current_turn_prompt

    d, captured = _install_fake_agent(
        monkeypatch,
        tmp_path,
        [
            _stream_with_session("native-chat-1", "one"),
            _stream_with_session("native-chat-1", "two"),
        ],
    )
    marionette_id = "marionette-sess-resume-prompt"
    prior_turn = "PRIOR_TURN_UNIQUE_MARKER_AAA"
    new_turn = "NEW_TURN_UNIQUE_MARKER_BBB"
    system = (
        "CODEGRAPH HAS ALREADY BEEN QUERIED\n"
        "SYSTEM_PREFIX_UNIQUE_MARKER_CCC\n"
        "some graph evidence"
    )
    d.chat_stream(
        [{"role": "user", "content": prior_turn}],
        on_delta=lambda _t: None,
        system=system,
        session_id=marionette_id,
    )
    resp = d.chat_stream(
        [
            {"role": "user", "content": prior_turn},
            {"role": "assistant", "content": "one"},
            {"role": "user", "content": new_turn},
        ],
        on_delta=lambda _t: None,
        system=system,
        session_id=marionette_id,
    )
    assert resp.error is None
    first_prompt = captured["cmds"][0][-1]
    second_prompt = captured["cmds"][1][-1]
    assert "--resume" not in captured["cmds"][0]
    assert "--resume" in captured["cmds"][1]

    # Cold call still packs system + history for native chat seeding.
    assert prior_turn in first_prompt
    assert "SYSTEM_PREFIX_UNIQUE_MARKER_CCC" in first_prompt
    assert "System:" in first_prompt

    # Resumed call: new turn only — no prior turn, system prefix, or role packing.
    assert new_turn in second_prompt
    assert prior_turn not in second_prompt
    assert "SYSTEM_PREFIX_UNIQUE_MARKER_CCC" not in second_prompt
    assert "System:" not in second_prompt
    assert "User:" not in second_prompt
    # Empty / unexpected history must not raise.
    assert _current_turn_prompt([]) == "hello"
    assert _current_turn_prompt([{"role": "assistant", "content": "only-asst"}]) == "hello"
    assert _current_turn_prompt([{"role": "user", "content": ""}]) == "hello"
    assert _current_turn_prompt([None, "bad", {"role": "user", "content": new_turn}]) == new_turn


def test_session_switch_resets_native_chat_id(monkeypatch, tmp_path):
    d, captured = _install_fake_agent(
        monkeypatch,
        tmp_path,
        [
            _stream_with_session("native-a", "a"),
            _stream_with_session("native-b", "b"),
        ],
    )
    d.chat(
        [{"role": "user", "content": "in-a"}],
        session_id="sess-a",
    )
    assert d._native_chat_id == "native-a"
    resp = d.chat(
        [{"role": "user", "content": "in-b"}],
        session_id="sess-b",
    )
    assert resp.error is None
    # New Marionette session must not resume the prior native chat.
    assert "--resume" not in captured["cmds"][1]
    assert d._native_chat_id == "native-b"
    assert "sess-a" not in captured["cmds"][1]
    assert "sess-b" not in captured["cmds"][1]


def test_never_passes_marionette_session_id_to_resume(monkeypatch, tmp_path):
    marionette_id = "harness-uuid-should-never-resume"
    d, captured = _install_fake_agent(
        monkeypatch,
        tmp_path,
        [
            _stream_with_session("cursor-native-xyz"),
            _stream_with_session("cursor-native-xyz"),
        ],
    )
    d.complete("first", session_id=marionette_id)
    d.complete("second", session_id=marionette_id)
    for cmd in captured["cmds"]:
        assert marionette_id not in cmd
    resume_idx = captured["cmds"][1].index("--resume")
    assert captured["cmds"][1][resume_idx + 1] == "cursor-native-xyz"


def test_parsed_session_id_persists_on_driver(monkeypatch, tmp_path):
    d, _captured = _install_fake_agent(
        monkeypatch, tmp_path, [_stream_with_session("persist-me")],
    )
    assert d._native_chat_id is None
    resp = d.chat_stream(
        [{"role": "user", "content": "hi"}],
        on_delta=lambda _t: None,
        session_id="sess-persist",
    )
    assert resp.error is None
    assert d._native_chat_id == "persist-me"
    assert resp.meta.get("session_id") == "persist-me"
    assert resp.meta.get("cursor_native_chat_id") == "persist-me"


def test_oneshot_without_session_does_not_resume(monkeypatch, tmp_path):
    d, captured = _install_fake_agent(
        monkeypatch,
        tmp_path,
        [
            _stream_with_session("native-kept"),
            _stream_with_session("native-oneshot"),
        ],
    )
    d.chat_stream(
        [{"role": "user", "content": "bound"}],
        on_delta=lambda _t: None,
        session_id="sess-bound",
    )
    assert d._native_chat_id == "native-kept"
    # One-shot: no session_id → no --resume; prior binding left intact.
    resp = d.complete("oneshot")
    assert resp.error is None
    assert "--resume" not in captured["cmds"][1]
    assert d._native_chat_id == "native-kept"


def test_model_change_resets_native_chat_id(monkeypatch, tmp_path):
    d, captured = _install_fake_agent(
        monkeypatch,
        tmp_path,
        [
            _stream_with_session("native-m1"),
            _stream_with_session("native-m2"),
        ],
    )
    d.chat([{"role": "user", "content": "a"}], session_id="sess-m")
    d.model = "composer-2.5-fast"
    d.chat([{"role": "user", "content": "b"}], session_id="sess-m")
    assert "--resume" not in captured["cmds"][1]
    assert d._native_chat_id == "native-m2"


def test_resume_failure_fails_clearly_and_clears_native(monkeypatch, tmp_path):
    fake_bin = tmp_path / "agent"
    fake_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    ok_stream = _stream_with_session("native-ok")
    fail_stream = "\n".join([
        json.dumps({
            "type": "result",
            "is_error": True,
            "result": "Failed to resume session: chat not found",
            "session_id": "",
        }),
        "",
    ])
    captured = {"cmds": []}

    class OkProc:
        returncode = 0
        stdout = io.StringIO(ok_stream)
        stderr = io.StringIO("")

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    class FailProc:
        returncode = 1
        stdout = io.StringIO(fail_stream)
        stderr = io.StringIO("chat not found\n")

        def wait(self, timeout=None):
            return 1

        def kill(self):
            pass

    procs = [OkProc(), FailProc()]

    def sequenced_popen(cmd, **kwargs):
        captured["cmds"].append(list(cmd))
        return procs.pop(0)

    monkeypatch.setattr(
        "pmharness.drivers.cursor_cli.subprocess.Popen", sequenced_popen,
    )
    d = CursorCliDriver(
        name="cursor-cli:m",
        model="composer-2.5",
        agent_binary=str(fake_bin),
        cwd=str(tmp_path),
    )
    d.chat([{"role": "user", "content": "hi"}], session_id="sess-r")
    assert d._native_chat_id == "native-ok"
    resp = d.chat([{"role": "user", "content": "again"}], session_id="sess-r")
    assert resp.error
    assert "failed to resume" in resp.error.lower()
    assert "native-ok" in resp.error
    assert d._native_chat_id is None
    # Must not silently retry a fresh turn without --resume.
    assert len(captured["cmds"]) == 2
    assert "--resume" in captured["cmds"][1]


def test_resume_noisy_stderr_markers_do_not_fail_successful_turn(
    monkeypatch, tmp_path,
):
    """Stderr resume-marker noise must not discard a successful --resume turn."""
    fake_bin = tmp_path / "agent"
    fake_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    ok_stream = _stream_with_session("native-ok")
    resume_ok_stream = _stream_with_session("native-ok", text="still good")
    captured = {"cmds": []}

    class OkProc:
        returncode = 0
        stdout = io.StringIO(ok_stream)
        stderr = io.StringIO("")

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    class NoisySuccessProc:
        returncode = 0
        stdout = io.StringIO(resume_ok_stream)
        # Warning-shaped noise that matches resume markers but is not a failure.
        stderr = io.StringIO(
            "warn: session not found in secondary index (ignored)\n"
        )

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    procs = [OkProc(), NoisySuccessProc()]

    def sequenced_popen(cmd, **kwargs):
        captured["cmds"].append(list(cmd))
        return procs.pop(0)

    monkeypatch.setattr(
        "pmharness.drivers.cursor_cli.subprocess.Popen", sequenced_popen,
    )
    d = CursorCliDriver(
        name="cursor-cli:m",
        model="composer-2.5",
        agent_binary=str(fake_bin),
        cwd=str(tmp_path),
    )
    d.chat([{"role": "user", "content": "hi"}], session_id="sess-noise")
    assert d._native_chat_id == "native-ok"
    resp = d.chat(
        [{"role": "user", "content": "again"}], session_id="sess-noise",
    )
    assert not resp.error
    assert resp.text == "still good"
    assert d._native_chat_id == "native-ok"
    assert len(captured["cmds"]) == 2
    assert "--resume" in captured["cmds"][1]
    resume_idx = captured["cmds"][1].index("--resume")
    assert captured["cmds"][1][resume_idx + 1] == "native-ok"


def test_driver_meta_preserves_explicit_zero_cache_and_served_model(
    monkeypatch, tmp_path,
):
    """Absent cache evidence stays absent; explicit zeros and served model kept."""
    fake_bin = tmp_path / "agent"
    fake_bin.write_text("x", encoding="utf-8")

    def _stream(usage, model="composer-2.5-served"):
        return "\n".join([
            json.dumps({
                "type": "system",
                "subtype": "init",
                "session_id": "native-1",
                "model": model,
            }),
            json.dumps({
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "hi"}],
                },
            }),
            json.dumps({
                "type": "result",
                "is_error": False,
                "result": "hi",
                "session_id": "native-1",
                "usage": usage,
            }),
            "",
        ])

    class FakeProc:
        def __init__(self, stream):
            self.returncode = 0
            self.stdout = io.StringIO(stream)
            self.stderr = io.StringIO("")

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    # Explicit zeros must survive into meta.
    monkeypatch.setattr(
        "pmharness.drivers.cursor_cli.subprocess.Popen",
        lambda *a, **k: FakeProc(
            _stream({
                "inputTokens": 10,
                "outputTokens": 1,
                "cacheReadTokens": 0,
                "cacheWriteInputTokens": 0,
            })
        ),
    )
    d = CursorCliDriver(
        name="cursor-cli:m",
        model="composer-2.5",
        agent_binary=str(fake_bin),
    )
    resp = d.chat([{"role": "user", "content": "hi"}])
    assert resp.meta.get("served_model") == "composer-2.5-served"
    assert resp.meta.get("raw_usage") is not None
    assert resp.meta["cache_read_tokens"] == 0
    assert resp.meta["cache_write_tokens"] == 0

    # Absent cache fields → omit keys (null evidence), keep raw_usage.
    monkeypatch.setattr(
        "pmharness.drivers.cursor_cli.subprocess.Popen",
        lambda *a, **k: FakeProc(
            _stream({"inputTokens": 10, "outputTokens": 1})
        ),
    )
    d2 = CursorCliDriver(
        name="cursor-cli:m",
        model="composer-2.5",
        agent_binary=str(fake_bin),
    )
    resp2 = d2.chat([{"role": "user", "content": "hi"}])
    assert "cache_read_tokens" not in resp2.meta
    assert "cache_write_tokens" not in resp2.meta
    assert resp2.meta.get("raw_usage") == {"inputTokens": 10, "outputTokens": 1}
    assert resp2.meta.get("served_model") == "composer-2.5-served"
    assert resp2.meta.get("requested_model") == "composer-2.5"
    assert resp2.meta.get("identity_status") == "verified"
    assert resp2.error is None
    assert resp2.model == "cursor-cli:m"


def test_cursor_fable_display_is_compatible():
    from pmharness.drivers.cursor_identity import cursor_models_compatible

    assert cursor_models_compatible(
        "claude-fable-5-high",
        "Claude Fable 5 High (200K)",
    )
    assert cursor_models_compatible(
        "claude-fable-5-high",
        "Claude Fable 5 High · No Thinking",
    )
    assert cursor_models_compatible(
        "claude-fable-5-high",
        "claude-fable-5-high",
    )
    assert not cursor_models_compatible(
        "claude-fable-5-high",
        "claude-fable-5-medium",
    )


def test_cursor_fable_vs_luna_is_incompatible():
    from pmharness.drivers.cursor_identity import cursor_models_compatible

    assert not cursor_models_compatible(
        "claude-fable-5-high",
        "gpt-5.6-luna-medium",
    )
    assert not cursor_models_compatible(
        "claude-fable-5-high",
        "GPT-5.6 Luna Medium 272K",
    )


def _print_stream(model, usage=None):
    return "\n".join([
        json.dumps({
            "type": "system",
            "subtype": "init",
            "session_id": "native-1",
            "model": model,
        }),
        json.dumps({
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "hi"}],
            },
        }),
        json.dumps({
            "type": "result",
            "is_error": False,
            "result": "hi",
            "session_id": "native-1",
            "usage": usage or {"inputTokens": 12, "outputTokens": 3},
        }),
        "",
    ])


class _PrintProc:
    def __init__(self, stream):
        self.returncode = 0
        self.stdout = io.StringIO(stream)
        self.stderr = io.StringIO("")

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


def test_print_path_accepts_fable_display_label(monkeypatch, tmp_path):
    fake_bin = tmp_path / "agent"
    fake_bin.write_text("x", encoding="utf-8")
    monkeypatch.setattr(
        "pmharness.drivers.cursor_cli.subprocess.Popen",
        lambda *a, **k: _PrintProc(
            _print_stream("Claude Fable 5 High (200K) No Thinking")
        ),
    )
    d = CursorCliDriver(
        name="cursor-cli:claude-fable-5-high",
        model="claude-fable-5-high",
        agent_binary=str(fake_bin),
    )
    resp = d.chat([{"role": "user", "content": "hi"}])
    assert resp.error is None
    assert resp.model == "cursor-cli:claude-fable-5-high"
    assert resp.meta["requested_model"] == "claude-fable-5-high"
    assert resp.meta["served_model"] == "Claude Fable 5 High (200K) No Thinking"
    assert resp.meta["identity_status"] == "verified"


def test_print_path_fails_closed_on_fable_vs_luna(monkeypatch, tmp_path):
    fake_bin = tmp_path / "agent"
    fake_bin.write_text("x", encoding="utf-8")
    monkeypatch.setattr(
        "pmharness.drivers.cursor_cli.subprocess.Popen",
        lambda *a, **k: _PrintProc(_print_stream("gpt-5.6-luna-medium")),
    )
    d = CursorCliDriver(
        name="cursor-cli:claude-fable-5-high",
        model="claude-fable-5-high",
        agent_binary=str(fake_bin),
    )
    resp = d.chat([{"role": "user", "content": "hi"}])
    assert resp.error
    assert "gpt-5.6-luna-medium" in resp.error
    assert "claude-fable-5-high" in resp.error
    assert resp.model == "gpt-5.6-luna-medium"
    assert resp.meta["requested_model"] == "claude-fable-5-high"
    assert resp.meta["served_model"] == "gpt-5.6-luna-medium"
    assert resp.meta["identity_status"] == "mismatch"
    assert d._native_chat_id is None
