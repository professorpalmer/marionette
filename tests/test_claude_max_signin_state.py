"""Claude Max sign-in state: the live failure of 2026-11-15.

The real state had a Claude Max OAuth access token in keys.json and one
anthropic pool entry ``env:ANTHROPIC_API_KEY`` of type api_key, with no
refresh token, exhausted on 401 "OAuth access token has expired". These
tests rebuild each step of that chain.
"""

from __future__ import annotations

import io
import json
import os
import time

import pytest

from harness import credential_pool as cp
from harness import keys as keys_mod


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("ANTHROPIC_TOKEN", "")
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    stored_keys: dict = {}
    monkeypatch.setattr(keys_mod, "_read_keys", lambda: dict(stored_keys))
    monkeypatch.setattr(
        keys_mod, "_write_keys",
        lambda k: (stored_keys.clear(), stored_keys.update(k)),
    )
    cp.clear_pools_for_tests()
    yield {"dir": tmp_path, "keys": stored_keys}
    cp.clear_pools_for_tests()


def _disk_pools(path) -> dict:
    with open(os.path.join(path, "auth_pool.json"), encoding="utf-8") as f:
        return json.load(f)["pools"]


def _write_disk_pools(path, pools: dict) -> None:
    with open(os.path.join(path, "auth_pool.json"), "w", encoding="utf-8") as f:
        json.dump({"version": 1, "pools": pools, "strategies": {}}, f)


def _signed_in(entry_id="max1", token="sk-ant-oat01-fresh", refresh="rt-1"):
    return {
        "id": entry_id, "label": "cary@example.com", "auth_type": "oauth",
        "source": "manual:pkce", "access_token": token, "refresh_token": refresh,
        "expires_at_ms": int(time.time() * 1000) + 3_600_000,
    }


def test_a_process_that_loads_another_pool_keeps_the_claude_sign_in(state):
    """Step 1: a persist from a process that never loaded the anthropic pool
    erased it from disk (``_persist_all`` wrote only the loaded pools)."""
    _write_disk_pools(state["dir"], {"anthropic": [_signed_in()]})

    cp.add_api_key("openrouter", "sk-or-v1-abcdefabcdef")

    anthropic = _disk_pools(state["dir"]).get("anthropic") or []
    assert [e["id"] for e in anthropic] == ["max1"]
    assert anthropic[0]["refresh_token"] == "rt-1"


def test_a_stale_process_does_not_erase_a_new_sign_in(state):
    """Step 2: a process with an older in-memory pool wrote it back over a
    sign-in that another process (the backend) had just saved."""
    stale = cp.add_api_key("anthropic", "sk-ant-oat01-expired")
    pools = _disk_pools(state["dir"])
    pools["anthropic"].append(_signed_in())
    _write_disk_pools(state["dir"], pools)

    cp.report_failure("anthropic", stale.id, status_code=401,
                      message="OAuth access token has expired")

    assert "max1" in [e["id"] for e in _disk_pools(state["dir"])["anthropic"]]
    assert cp.resolve_entry("anthropic").runtime_token == "sk-ant-oat01-fresh"


def test_a_removed_entry_stays_removed(state):
    """The merge must not bring back an entry this process signed out."""
    entry = cp.add_oauth_entry("anthropic", access_token="sk-ant-oat01-x",
                               refresh_token="rt", label="a")
    assert cp.remove_entry("anthropic", entry.id)
    cp.add_api_key("openrouter", "sk-or-v1-abcdefabcdef")
    assert not _disk_pools(state["dir"]).get("anthropic")


def test_an_expired_subscription_token_without_refresh_is_never_healthy_again(
    state, monkeypatch,
):
    """Step 3: the seeded copy of an OAuth token was an api_key entry. A 401
    made it exhausted for 5 minutes, then it looked healthy again, so
    Settings said "connected" and the pilot sent it again."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-oat01-expired-copy")
    pool = cp.load_pool("anthropic")
    (seeded,) = pool.entries()
    assert seeded.auth_type == cp.AUTH_TYPE_OAUTH
    assert seeded.refresh_token is None

    assert cp.report_failure(
        "anthropic", seeded.id, status_code=401,
        message='{"type":"error","error":{"type":"authentication_error",'
                '"message":"OAuth access token has expired."}}',
    ) is None

    later = time.time() + cp.EXHAUSTED_TTL_401 + 60
    monkeypatch.setattr(cp.time, "time", lambda: later)
    assert not cp.has_healthy_credential("anthropic")
    assert seeded.last_status == cp.STATUS_DEAD


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_sign_in_replaces_the_stale_copy_and_the_pilot_uses_it(state, monkeypatch):
    """Step 4: a Claude Max sign-in must store a refreshable OAuth entry that
    the pilot selects first, and must not copy the access token into
    keys.json (that copy was the source of the stale api_key entry)."""
    from harness import oauth_anthropic as oa

    state["keys"]["anthropic"] = "sk-ant-oat01-expired-copy"
    stale = cp.add_api_key("anthropic", "sk-ant-oat01-expired-copy",
                           label="env:ANTHROPIC_API_KEY")
    cp.report_failure("anthropic", stale.id, status_code=401,
                      message="OAuth access token has expired")

    body = {
        "access_token": "sk-ant-oat01-fresh", "refresh_token": "rt-new",
        "expires_in": 28800, "account": {"email_address": "cary@example.com"},
    }
    monkeypatch.setattr(
        oa.urllib.request, "urlopen",
        lambda req, timeout=20: _Resp(json.dumps(body).encode()),
    )
    start = oa.start_anthropic_pkce_login()
    sid = start["session_id"]
    expected_state = oa._pending[sid]["state"]

    out = oa.complete_anthropic_pkce_login(sid, f"code123#{expected_state}")

    assert out["status"] == "done"
    assert out["label"] == "cary@example.com"
    entries = cp.load_pool("anthropic").entries()
    assert [e.runtime_token for e in entries] == ["sk-ant-oat01-fresh"]
    assert entries[0].auth_type == cp.AUTH_TYPE_OAUTH
    assert entries[0].refresh_token == "rt-new"
    assert "anthropic" not in state["keys"]
    assert cp.resolve_entry("anthropic").runtime_token == "sk-ant-oat01-fresh"
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-oat01-fresh"


def test_a_turned_off_provider_does_not_drive_the_pilot(state, monkeypatch):
    """The real state had anthropic in disconnected.json while its pool entry
    still served the pilot (401 at 12:21). Off must mean off."""
    cp.add_oauth_entry("anthropic", access_token="sk-ant-oat01-fresh",
                       refresh_token="rt", label="a")
    monkeypatch.setattr(keys_mod, "get_disconnected", lambda: {"anthropic"})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")

    assert cp.resolve_entry_for_env("ANTHROPIC_API_KEY") is None
    assert not cp.credential_satisfied("ANTHROPIC_API_KEY")
    cp.clear_pools_for_tests()
    cp.load_pool("anthropic")
    assert os.environ["ANTHROPIC_API_KEY"] == ""


def test_the_stored_live_entry_loads_as_a_sign_in_and_turns_expired(state):
    """The exact stored entry from the real auth_pool.json (token masked)."""
    _write_disk_pools(state["dir"], {"anthropic": [{
        "id": "f5763f4f", "label": "env:ANTHROPIC_API_KEY", "auth_type": "api_key",
        "source": "env:ANTHROPIC_API_KEY", "access_token": "sk-ant-oat01-" + "x" * 95,
        "refresh_token": None, "last_status": "exhausted", "last_error_code": 401,
        "last_status_at": time.time(),
    }]})
    (entry,) = cp.load_pool("anthropic").entries()
    assert entry.auth_type == cp.AUTH_TYPE_OAUTH

    cp.report_failure("anthropic", entry.id, status_code=401,
                      message="OAuth access token has expired.")

    from harness import accounts
    row = accounts.account_row(accounts.find_provider("anthropic"))
    assert (row.state, row.action) == ("expired", "sign_in")


def test_a_refused_code_shows_the_server_reason_not_the_fallback_404(state, monkeypatch):
    """Live probe 2026-11-15: platform.claude.com answers 400 invalid_grant for
    a bad code; the console.anthropic.com fallback answers 404. The old loop
    showed only the 404."""
    import urllib.error
    from harness import oauth_anthropic as oa

    calls = []

    def refuse(req, timeout=20):
        calls.append(req.full_url)
        body = io.BytesIO(json.dumps({
            "error": "invalid_grant",
            "error_description": "Invalid 'code' in request.",
        }).encode())
        raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, body)

    monkeypatch.setattr(oa.urllib.request, "urlopen", refuse)
    sid = oa.start_anthropic_pkce_login()["session_id"]

    out = oa.complete_anthropic_pkce_login(sid, "not-a-real-code")

    assert out["error"] == "Token exchange failed: HTTP 400: Invalid 'code' in request."
    assert calls == [oa._OAUTH_TOKEN_URLS[0]]
