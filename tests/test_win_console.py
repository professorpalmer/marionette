"""The Windows hidden-console default must cover every subprocess site without
clobbering deliberate console choices (wiki_backend's DETACHED_PROCESS, a
debug CREATE_NEW_CONSOLE)."""
import io
import json
import os
import subprocess
import sys
from unittest.mock import patch

import pytest

from harness import win_console


def test_effective_creationflags_adds_no_window_by_default():
    no_window = win_console._CREATE_NO_WINDOW
    assert win_console.effective_creationflags(0) == no_window
    group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
    assert win_console.effective_creationflags(group) == group | no_window


def test_effective_creationflags_respects_explicit_console_choices():
    for explicit in (
        win_console._CREATE_NEW_CONSOLE,
        win_console._DETACHED_PROCESS,
        win_console._CREATE_NO_WINDOW,
        win_console._DETACHED_PROCESS | win_console._CREATE_NO_WINDOW,
    ):
        assert win_console.effective_creationflags(explicit) == explicit


def test_run_command_retains_process_group_and_no_window():
    """command_policy passes CREATE_NEW_PROCESS_GROUP; win_console ORs NO_WINDOW."""
    group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
    no_window = win_console._CREATE_NO_WINDOW
    assert win_console.effective_creationflags(group) == group | no_window


@pytest.mark.skipif(os.name != "nt", reason="Windows console semantics")
def test_hide_child_consoles_is_installed_and_children_still_run():
    # Importing harness (done above) installs the default on Windows.
    assert subprocess.Popen.__init__ is win_console._hidden_popen_init
    proc = subprocess.run(
        [sys.executable, "-c", "print('ok')"], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0
    assert "ok" in proc.stdout


def test_cursor_cli_popen_sets_create_no_window(monkeypatch, tmp_path):
    from pmharness.drivers.cursor_cli import CursorCliDriver

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
        captured["kwargs"] = kwargs
        return FakeProc()

    monkeypatch.setattr("pmharness.drivers.cursor_cli.subprocess.Popen", fake_popen)
    monkeypatch.setattr(
        "pmharness.drivers.cursor_cli.sys.platform", "win32", raising=False,
    )
    monkeypatch.setattr(
        "pmharness.drivers.cursor_cli.subprocess.CREATE_NO_WINDOW",
        0x08000000,
        raising=False,
    )
    d = CursorCliDriver(
        name="cursor-cli:auto", model="auto", agent_binary=str(fake_bin),
    )
    resp = d.chat_stream([{"role": "user", "content": "hi"}], on_delta=lambda *_: None)
    assert resp.error is None
    assert captured["kwargs"].get("creationflags") == 0x08000000


def test_cursor_acp_popen_sets_create_no_window(monkeypatch):
    """Top-level agent acp spawn gets CREATE_NO_WINDOW; MCP grandchildren do not.

    Cursor Agent's Node process may still spawn MCP servers without windowsHide /
    CREATE_NO_WINDOW. That requires an upstream Cursor fix — Marionette cannot
    reach those grandchildren from this Popen.
    """
    from pmharness.drivers import cursor_acp as acp_mod

    captured = {}

    class FakeProc:
        pid = 4242
        stdin = io.StringIO()
        stdout = io.StringIO()
        stderr = io.StringIO()

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

        def terminate(self):
            pass

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return FakeProc()

    monkeypatch.setattr(acp_mod.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(acp_mod.sys, "platform", "win32")
    monkeypatch.setattr(acp_mod.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(acp_mod, "resolve_agent_exec", lambda: ["agent"])

    session = acp_mod.WarmAcpSession(model="m", cwd=None)
    # Bypass handshake — only assert spawn flags.
    transport = session._spawn_transport()
    assert transport is not None
    assert captured["kwargs"].get("creationflags") == 0x08000000
    assert "acp" in captured["cmd"]
