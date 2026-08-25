"""Harness Chrome auth handoff: shared profile + CDP port, never cookies."""
from __future__ import annotations

import types

from harness.pilot import VALID_ACTION_KINDS, build_tools_schema
from harness.send_loop_phases import LOCAL_ACTION_KINDS, PLAN_SKIP_KINDS
import harness.browser_auth as auth


def test_auth_off_under_pytest_by_default(monkeypatch):
    monkeypatch.delenv("HARNESS_BROWSER_AUTH", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/test_browser_auth.py")
    assert auth.auth_env_enabled() is False
    assert auth.ensure_shared_browser_env() == {}


def test_explicit_auth_publishes_shared_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_BROWSER_AUTH", "1")
    monkeypatch.setenv("HARNESS_BROWSER_HEADED", "1")
    monkeypatch.setenv("PM_BROWSER_USER_DATA_DIR", str(tmp_path / "profile"))
    monkeypatch.delenv("PM_BROWSER_CDP_PORT", raising=False)
    monkeypatch.delenv("PM_BROWSER_HEADED", raising=False)
    applied = auth.ensure_shared_browser_env()
    assert applied["cdp_port"] == auth.DEFAULT_CDP_PORT
    assert applied["user_data_dir"] == str(tmp_path / "profile")
    assert applied["headed"] == "1"


def test_handoff_uses_engine_and_never_returns_cookies(monkeypatch, tmp_path):
    import harness.browser as browser

    monkeypatch.setenv("PM_BROWSER_USER_DATA_DIR", str(tmp_path / "profile"))
    monkeypatch.setenv("PM_BROWSER_CDP_PORT", "9333")
    captured = {}
    prev_engine = browser._engine
    prev_err = browser._ENGINE_ERR

    def fake_handoff(url):
        captured["url"] = url
        return (
            "Navigated to %s\nTitle: Login\n\nAuth handoff: complete login "
            "or Cloudflare in the visible Chrome window. Do not paste "
            "passwords or cookies into chat. Workers reuse this session "
            "(port=9333, persistent=yes, profile=%s)."
            % (url, tmp_path / "profile")
        )

    try:
        browser._ENGINE_ERR = ""
        browser._engine = types.SimpleNamespace(
            auth_handoff=fake_handoff,
            __name__="puppetmaster.browser_cdp",
        )
        out = auth.browser_auth_handoff("https://challonge.com/users/sign_in")
    finally:
        browser._engine = prev_engine
        browser._ENGINE_ERR = prev_err
    assert captured["url"] == "https://challonge.com/users/sign_in"
    assert "Auth handoff" in out
    assert "Do not paste passwords or cookies" in out
    assert "Set-Cookie" not in out
    assert "challonge.com" in out


def test_handoff_requires_url():
    assert "url is required" in auth.browser_auth_handoff("")


def test_dispatch_auth_handoff_returns_engine_string(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from harness.pilot import PilotAction
    from harness.send_loop_phases import dispatch_local_action

    monkeypatch.setattr(
        "harness.browser_auth.browser_auth_handoff",
        lambda url: "Auth handoff: complete login at %s" % url,
    )
    session = SimpleNamespace(_append_action_result=MagicMock())
    act = PilotAction(
        kind="browser_auth_handoff",
        url="https://example.com/login",
        arguments={"url": "https://example.com/login"},
    )
    events = list(dispatch_local_action(session, act, "a-auth", False, [], plan=False))
    assert events[0].data["types"] == ["browser_auth_handoff"]
    session._append_action_result.assert_called_once()
    logged = session._append_action_result.call_args[0][2]
    assert "Auth handoff" in logged
    assert "example.com/login" in logged
    assert "browser_auth_handoff" in VALID_ACTION_KINDS
    assert "browser_auth_handoff" in LOCAL_ACTION_KINDS
    assert "browser_auth_handoff" in PLAN_SKIP_KINDS
    names = [
        (item.get("function") or {}).get("name")
        for item in build_tools_schema(browser_enabled=True)
    ]
    assert "browser_auth_handoff" in names
