"""One account row per provider, from the store the pilot reads."""

from __future__ import annotations

import time

import pytest

from harness import accounts
from harness import credential_pool as cp
from harness import keys as keys_mod


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN", "OPENROUTER_API_KEY"):
        monkeypatch.setenv(name, "")
    stored: dict = {}
    monkeypatch.setattr(keys_mod, "_read_keys", lambda: dict(stored))
    monkeypatch.setattr(keys_mod, "_write_keys",
                        lambda k: (stored.clear(), stored.update(k)))
    monkeypatch.setattr(keys_mod, "get_disconnected", lambda: set())
    cp.clear_pools_for_tests()
    accounts.clear_checks_for_tests()
    yield stored
    cp.clear_pools_for_tests()
    accounts.clear_checks_for_tests()


def _row(name: str) -> dict:
    return accounts.account_row(accounts.find_provider(name)).to_dict()


def test_nothing_stored_is_not_set_with_sign_in_first(state):
    row = _row("anthropic")
    assert row["state"] == "not_set"
    assert row["action"] == "sign_in"
    assert row["methods"] == ["sign_in", "api_key"]


def test_a_sign_in_shows_the_account_not_the_env_name(state):
    cp.add_oauth_entry("anthropic", access_token="sk-ant-oat01-fresh",
                       refresh_token="rt", label="cary@example.com",
                       expires_at_ms=int(time.time() * 1000) + 3_600_000)
    row = _row("anthropic")
    assert (row["state"], row["account"], row["action"]) == (
        "signed_in", "cary@example.com", "")


def test_the_live_bug_state_reads_expired_not_connected(state, monkeypatch):
    """Cary's state: a refresh-less sign-in token that answered 401."""
    entry = cp.add_api_key("anthropic", "sk-ant-oat01-expired-copy",
                           label="env:ANTHROPIC_API_KEY")
    cp.report_failure("anthropic", entry.id, status_code=401,
                      message="OAuth access token has expired.")
    later = time.time() + cp.EXHAUSTED_TTL_401 + 60
    monkeypatch.setattr(cp.time, "time", lambda: later)

    row = _row("anthropic")
    assert row["state"] == "expired"
    assert row["action"] == "sign_in"
    assert "env:" not in row["account"]
    assert accounts.auth_failure_text("anthropic") == (
        "pilot: Claude (Anthropic) sign-in expired. Sign in again in "
        "Settings > Accounts.")


def test_a_rejected_key_names_the_real_fix(state):
    entry = cp.add_api_key("openrouter", "sk-or-v1-0123456789")
    cp.report_failure("openrouter", entry.id, status_code=401, message="bad key")
    row = _row("openrouter")
    assert (row["state"], row["action"]) == ("rejected", "add_key")
    assert "Add a new key in Settings > Accounts" in accounts.auth_failure_text("openrouter")


def test_probe_success_records_the_check_and_clears_a_failure(state, monkeypatch):
    entry = cp.add_oauth_entry("anthropic", access_token="sk-ant-oat01-fresh",
                               refresh_token="rt", label="cary@example.com",
                               expires_at_ms=int(time.time() * 1000) + 3_600_000)
    seen = []
    monkeypatch.setattr(accounts, "_http_status",
                        lambda url, headers: (seen.append(headers), (200, ""))[1])

    row = accounts.probe_account("anthropic")["row"]

    assert row["state"] == "signed_in"
    assert row["check"]["ok"] is True and row["check"]["status"] == 200
    assert seen[0]["Authorization"] == "Bearer sk-ant-oat01-fresh"
    assert entry.last_status == cp.STATUS_OK


def test_probe_401_on_a_token_without_renewal_turns_it_expired(state, monkeypatch):
    cp.add_api_key("anthropic", "sk-ant-oat01-expired-copy")
    monkeypatch.setattr(accounts, "_http_status",
                        lambda url, headers: (401, "OAuth access token has expired."))

    row = accounts.probe_account("anthropic")["row"]

    assert row["state"] == "expired"
    assert row["check"] == {**row["check"], "ok": False, "status": 401}


def test_probe_401_renews_a_sign_in_and_checks_again(state, monkeypatch):
    cp.add_oauth_entry("anthropic", access_token="sk-ant-oat01-old",
                       refresh_token="rt-1", label="cary@example.com",
                       expires_at_ms=int(time.time() * 1000) + 3_600_000)
    monkeypatch.setattr(
        "harness.oauth_anthropic.refresh_anthropic_tokens",
        lambda rt: {"access_token": "sk-ant-oat01-new", "expires_in": 3600,
                    "refresh_token": "rt-2"},
    )
    answers = iter([(401, "expired"), (200, "")])
    monkeypatch.setattr(accounts, "_http_status", lambda url, headers: next(answers))

    row = accounts.probe_account("anthropic")["row"]

    assert row["state"] == "signed_in"
    assert row["check"]["ok"] is True


def test_disconnected_provider_is_off(state, monkeypatch):
    monkeypatch.setattr(keys_mod, "get_disconnected", lambda: {"openrouter"})
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-0123456789")
    row = _row("openrouter")
    assert (row["state"], row["action"]) == ("off", "turn_on")


def test_every_row_has_a_known_state(state):
    for row in accounts.account_rows():
        assert row["state"] in accounts.STATES


def test_a_codex_sign_in_that_nothing_renews_stays_expired(state, monkeypatch):
    """Codex entries carry a refresh token, but the pool has no Codex
    refresher. A 401 must not flip back to "signed in" after the cooldown."""
    entry = cp.add_oauth_entry("openai-codex", access_token="codex-access",
                               refresh_token="codex-refresh", label="cary@example.com")
    cp.report_failure("openai-codex", entry.id, status_code=401, message="expired")
    later = time.time() + cp.EXHAUSTED_TTL_401 + 60
    monkeypatch.setattr(cp.time, "time", lambda: later)
    assert _row("openai-codex")["state"] == "expired"


def test_a_new_codex_sign_in_keeps_the_other_codex_accounts(state):
    first = cp.add_oauth_entry("openai-codex", access_token="codex-a",
                               refresh_token="r-a", label="a@example.com")
    cp.add_oauth_entry("openai-codex", access_token="codex-b",
                       refresh_token="r-b", label="b@example.com")
    tokens = [e.runtime_token for e in cp.load_pool("openai-codex").entries()]
    assert sorted(tokens) == ["codex-a", "codex-b"]
    assert cp.resolve_entry("openai-codex").runtime_token == "codex-b"
    assert first.priority == 1
