"""Cursor CLI auth helper: status / login guidance with mocked subprocess."""

from __future__ import annotations

import json
import subprocess

import pytest

from harness import cursor_cli_auth as auth


def test_get_status_missing_binary(monkeypatch):
    auth.invalidate_status_cache()
    monkeypatch.setattr(auth, "resolve_agent_binary", lambda: None)
    st = auth.get_status(refresh=True)
    assert st["installed"] is False
    assert st["authenticated"] is False
    assert st["auth_kind"] == "cursor_account"
    assert "not found" in (st.get("error") or "").lower() or "Install" in (st.get("error") or "")


def test_get_status_logged_in_json(monkeypatch):
    monkeypatch.setattr(auth, "resolve_agent_binary", lambda: "/fake/agent")
    auth.invalidate_status_cache()

    class Proc:
        returncode = 0
        stdout = json.dumps({"loggedIn": True, "email": "u@example.com"})
        stderr = ""

    monkeypatch.setattr(auth, "_run_agent", lambda *a, **k: Proc())
    st = auth.get_status(refresh=True)
    assert st["installed"] is True
    assert st["authenticated"] is True
    assert st["label"] == "u@example.com"


def test_get_status_not_logged_in_text(monkeypatch):
    monkeypatch.setattr(auth, "resolve_agent_binary", lambda: "/fake/agent")
    auth.invalidate_status_cache()

    class Proc:
        returncode = 0
        stdout = "Not logged in. Run `agent login`."
        stderr = ""

    monkeypatch.setattr(auth, "_run_agent", lambda *a, **k: Proc())
    st = auth.get_status(refresh=True)
    assert st["authenticated"] is False


def test_get_status_isAuthenticated_field(monkeypatch):
    monkeypatch.setattr(auth, "resolve_agent_binary", lambda: "/fake/agent")
    auth.invalidate_status_cache()

    class Proc:
        returncode = 0
        stdout = json.dumps({
            "status": "unauthenticated",
            "isAuthenticated": False,
            "message": "Not logged in",
        })
        stderr = ""

    monkeypatch.setattr(auth, "_run_agent", lambda *a, **k: Proc())
    st = auth.get_status(refresh=True)
    assert st["authenticated"] is False


def test_get_status_cache_avoids_respawn(monkeypatch):
    monkeypatch.setattr(auth, "resolve_agent_binary", lambda: "/fake/agent")
    auth.invalidate_status_cache()
    calls = []

    class Proc:
        returncode = 0
        stdout = json.dumps({"isAuthenticated": False, "status": "unauthenticated"})
        stderr = ""

    def run(*a, **k):
        calls.append(a)
        return Proc()

    monkeypatch.setattr(auth, "_run_agent", run)
    assert auth.get_status(refresh=True)["authenticated"] is False
    assert auth.get_status()["authenticated"] is False
    assert len(calls) == 1


def test_start_login_missing_binary(monkeypatch):
    monkeypatch.setattr(auth, "resolve_agent_binary", lambda: None)
    res = auth.start_login()
    assert res["ok"] is False
    assert res["launched"] is False


def test_start_login_launches(monkeypatch):
    monkeypatch.setattr(auth, "resolve_agent_binary", lambda: "/fake/agent")
    calls = []

    def fake_popen(cmd, **kwargs):
        calls.append(cmd)
        class P:
            pass
        return P()

    monkeypatch.setattr(auth.subprocess, "Popen", fake_popen)
    res = auth.start_login()
    assert res["ok"] is True
    assert res["launched"] is True
    assert res["auth_kind"] == "cursor_account"
    assert calls and calls[0][-1] == "login"
    assert "trust" in (res.get("hint") or "").lower()


def test_ensure_workspace_trusted(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "resolve_agent_binary", lambda: "/fake/agent")
    monkeypatch.setattr(auth, "prewarm_agent", lambda: None)
    captured = {}

    class Proc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def run(args, **kwargs):
        captured["args"] = args
        return Proc()

    monkeypatch.setattr(auth, "_run_agent", run)
    res = auth.ensure_workspace_trusted(str(tmp_path))
    assert res["ok"] is True
    assert res["trusted"] is True
    assert res["workspace"] == str(tmp_path.resolve())
    assert "--trust" in captured["args"]
    assert "--print" in captured["args"]
    assert "--workspace" in captured["args"]
    assert str(tmp_path.resolve()) in captured["args"]


def test_ensure_workspace_trusted_missing_dir(monkeypatch):
    monkeypatch.setattr(auth, "resolve_agent_binary", lambda: "/fake/agent")
    res = auth.ensure_workspace_trusted("/no/such/workspace/path")
    assert res["trusted"] is False
    assert res["ok"] is False


def test_ensure_workspace_trusted_timeout_is_soft(monkeypatch, tmp_path):
    """Cold-start trust must not raise — Sign in already succeeded in the UI."""
    import subprocess

    monkeypatch.setattr(auth, "resolve_agent_binary", lambda: "/fake/agent")
    warmed = []

    def boom(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="agent", timeout=20)

    monkeypatch.setattr(auth, "_run_agent", boom)
    monkeypatch.setattr(auth, "prewarm_agent", lambda: warmed.append(1))
    res = auth.ensure_workspace_trusted(str(tmp_path))
    assert res["trusted"] is False
    assert "timed out" in (res.get("error") or "").lower()
    assert warmed == [1]


def test_login_token_sentinel(monkeypatch):
    monkeypatch.delenv("CURSOR_CLI_LOGIN", raising=False)
    monkeypatch.setattr(auth, "is_authenticated", lambda: False)
    assert auth.login_token_if_ready() is None
    monkeypatch.setenv("CURSOR_CLI_LOGIN", "1")
    assert auth.login_token_if_ready() == "1"
    monkeypatch.delenv("CURSOR_CLI_LOGIN", raising=False)
    monkeypatch.setattr(auth, "is_authenticated", lambda: True)
    assert auth.login_token_if_ready() == "1"


def test_list_models_fallback(monkeypatch):
    monkeypatch.setattr(auth, "resolve_agent_binary", lambda: None)
    models = auth.list_models()
    assert "auto" not in models  # Cursor router — Marionette picks the pilot
    assert "sonnet-4" in models
    assert "composer-2.5" in models
    assert "cursor-grok-4.5-high" in models


def test_parse_agent_models_text():
    sample = """Available models

auto - Auto (default)
composer-2.5 - Composer 2.5
cursor-grok-4.5-high - Cursor Grok 4.5
gpt-5.5-high - GPT-5.5 1M High

Tip: use --model <id>
"""
    ids = auth._parse_agent_models_text(sample)
    assert "auto" not in ids
    assert ids == [
        "composer-2.5",
        "cursor-grok-4.5-high",
        "gpt-5.5-high",
    ]


def test_list_models_live_parses_text(monkeypatch):
    monkeypatch.setattr(auth, "resolve_agent_binary", lambda: "/fake/agent")

    class Proc:
        returncode = 0
        stdout = "composer-2.5 - Composer 2.5\ncursor-grok-4.5-medium - Grok\n"
        stderr = ""

    monkeypatch.setattr(auth, "_run_agent", lambda *a, **k: Proc())
    models = auth.list_models(live=True)
    assert "composer-2.5" in models
    assert "cursor-grok-4.5-medium" in models
