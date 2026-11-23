"""A key that the provider refuses for its account moves the pool to the next key.

Live case 2026-11-22: the first OpenCode Go key got HTTP 403 "An active
OpenCode Go subscription is required". The pool did not rotate on 403, so two
new keys behind it never got a request, and each turn failed.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

import pytest

from harness import accounts
from harness import credential_pool as cp
from harness import keys as keys_mod
from pmharness.drivers import error_classifier as ec
from pmharness.drivers.openai_compat import OpenAICompatDriver

NO_SUBSCRIPTION = json.dumps({"error": {
    "type": "permission_error",
    "message": "An active OpenCode Go subscription is required to use OpenCode Go models.",
}})


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    for name in ("OPENCODE_GO_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    stored: dict = {}
    monkeypatch.setattr(keys_mod, "_read_keys", lambda: dict(stored))
    monkeypatch.setattr(keys_mod, "_write_keys",
                        lambda k: (stored.clear(), stored.update(k)))
    monkeypatch.setattr(keys_mod, "get_disconnected", lambda: set())
    monkeypatch.setattr(keys_mod, "unmark_disconnected", lambda reach: None)
    cp.clear_pools_for_tests()
    accounts.clear_checks_for_tests()
    yield stored
    cp.clear_pools_for_tests()
    accounts.clear_checks_for_tests()


class _HTTPError(urllib.error.HTTPError):
    def __init__(self, code: int, body: str):
        super().__init__("https://example.test/v1/chat/completions", code, "err",
                         hdrs=None, fp=io.BytesIO(body.encode("utf-8")))


class _Ok:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps({
            "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }).encode("utf-8")


def _go_driver() -> OpenAICompatDriver:
    return OpenAICompatDriver(name="go", model="deepseek-v4.1-flash",
                              base_url="https://example.test/v1",
                              api_key_env="OPENCODE_GO_API_KEY")


def test_a_403_for_the_account_is_a_key_failure_and_a_plain_403_is_not():
    assert ec.key_scoped_failure(403, NO_SUBSCRIPTION)
    assert ec.key_scoped_failure(401, "bad key")
    assert ec.key_scoped_failure(402, "")
    assert ec.key_scoped_failure(429, "")
    # The request, not the key: moderation, region policy, a model without access.
    assert not ec.key_scoped_failure(403, '{"error":{"message":"Input was flagged"}}')
    assert not ec.key_scoped_failure(403, "RegionError: requires explicit opt in")
    assert not ec.key_scoped_failure(500, NO_SUBSCRIPTION)


def test_driver_moves_to_the_next_key_on_a_no_subscription_403(state, monkeypatch):
    dead = cp.add_api_key("opencode-go", "sk-dead-aaaaaaaaaaaa")
    good = cp.add_api_key("opencode-go", "sk-good-bbbbbbbbbbbb")
    driver = _go_driver()
    sent = []

    def fake_urlopen(req, timeout=None):
        auth = req.get_header("Authorization") or ""
        sent.append(auth)
        if dead.access_token in auth:
            raise _HTTPError(403, NO_SUBSCRIPTION)
        return _Ok()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    first = driver.complete("ping")
    second = _go_driver().complete("ping")

    assert first.error is None and first.text == "hi"
    assert second.error is None
    # One refused request, then the good key. The next turn starts on the good key.
    assert [dead.access_token in a for a in sent] == [True, False, False]
    assert dead.last_status == cp.STATUS_EXHAUSTED and dead.last_error_code == 403
    assert good.last_status == cp.STATUS_OK


def test_a_request_scoped_403_keeps_the_key(state, monkeypatch):
    only = cp.add_api_key("opencode-go", "sk-good-aaaaaaaaaaaa")
    cp.add_api_key("opencode-go", "sk-good-bbbbbbbbbbbb")

    def fake_urlopen(req, timeout=None):
        raise _HTTPError(403, '{"error":{"type":"RegionError","message":"requires explicit opt in"}}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    resp = _go_driver().complete("ping")

    assert resp.error and resp.error.startswith("HTTP 403")
    assert only.last_status == cp.STATUS_OK


def test_a_key_saved_in_settings_goes_first(state):
    keys_mod.set_api_key("opencode-go", "sk-old-aaaaaaaaaaaa")
    keys_mod.set_api_key("opencode-go", "sk-new-bbbbbbbbbbbb")

    assert cp.resolve_entry("opencode-go").runtime_token == "sk-new-bbbbbbbbbbbb"
    assert state["opencode-go"] == "sk-new-bbbbbbbbbbbb"
    public = cp.list_pool_public("opencode-go")["entries"]
    assert [e["first"] for e in public] == [True, False]
    assert public[0]["masked"].endswith("bbbb")
    # The older key stays as the next key.
    assert len(public) == 2 and all(e["usable"] for e in public)


def test_saving_a_stored_key_again_moves_it_first(state):
    keys_mod.set_api_key("opencode-go", "sk-one-aaaaaaaaaaaa")
    keys_mod.set_api_key("opencode-go", "sk-two-bbbbbbbbbbbb")
    keys_mod.set_api_key("opencode-go", "sk-one-aaaaaaaaaaaa")

    assert cp.resolve_entry("opencode-go").runtime_token == "sk-one-aaaaaaaaaaaa"
    assert len(cp.load_pool("opencode-go").entries()) == 2


def test_an_extra_key_goes_last(state):
    keys_mod.set_api_key("opencode-go", "sk-one-aaaaaaaaaaaa")
    cp.add_api_key("opencode-go", "sk-extra-bbbbbbbbbb")

    assert cp.resolve_entry("opencode-go").runtime_token == "sk-one-aaaaaaaaaaaa"


def test_public_list_marks_a_refused_key(state):
    dead = cp.add_api_key("opencode-go", "sk-dead-aaaaaaaaaaaa")
    cp.add_api_key("opencode-go", "sk-good-bbbbbbbbbbbb")
    cp.report_failure("opencode-go", dead.id, status_code=403, message=NO_SUBSCRIPTION)

    by_mask = {e["masked"][-4:]: e for e in cp.list_pool_public("opencode-go")["entries"]}

    assert (by_mask["aaaa"]["usable"], by_mask["aaaa"]["first"]) == (False, False)
    assert (by_mask["bbbb"]["usable"], by_mask["bbbb"]["first"]) == (True, True)
    assert by_mask["aaaa"]["last_error_code"] == 403


def test_every_pool_with_a_key_is_listed(state):
    cp.add_api_key("zai", "sk-zai-aaaaaaaaaaaa")
    cp.add_api_key("opencode-go", "sk-go-bbbbbbbbbbbbb")

    listed = {p["provider"]: len(p["entries"]) for p in cp.list_all_pools_public()["pools"]}

    assert listed == {"zai": 1, "opencode-go": 1}


def test_removing_the_stored_key_points_the_key_file_at_the_next_key(state, monkeypatch):
    keys_mod.set_api_key("opencode-go", "sk-old-aaaaaaaaaaaa")
    keys_mod.set_api_key("opencode-go", "sk-new-bbbbbbbbbbbb")
    new = cp.resolve_entry("opencode-go")

    assert cp.remove_entry("opencode-go", new.id)

    assert state["opencode-go"] == "sk-old-aaaaaaaaaaaa"
    import os
    assert os.environ["OPENCODE_GO_API_KEY"] == "sk-old-aaaaaaaaaaaa"


def test_removing_the_last_key_does_not_bring_it_back(state):
    keys_mod.set_api_key("opencode-go", "sk-only-aaaaaaaaaaa")
    only = cp.resolve_entry("opencode-go")

    assert cp.remove_entry("opencode-go", only.id)
    cp.clear_pools_for_tests()  # a restart loads the pool from disk again

    assert "opencode-go" not in state
    assert cp.load_pool("opencode-go").entries() == []
    import os
    assert not os.environ.get("OPENCODE_GO_API_KEY")


def test_check_of_one_key_marks_it_and_reports_the_provider_answer(state, monkeypatch):
    dead = cp.add_api_key("opencode-go", "sk-dead-aaaaaaaaaaaa")
    good = cp.add_api_key("opencode-go", "sk-good-bbbbbbbbbbbb")

    def fake_probe(provider, token):
        if token == dead.access_token:
            return 403, "An active OpenCode Go subscription is required to use OpenCode Go models."
        return 200, ""

    monkeypatch.setattr(accounts, "_probe_request", fake_probe)

    refused = accounts.check_pool_entry("opencode-go", dead.id)["check"]
    accepted = accounts.check_pool_entry("opencode-go", good.id)["check"]

    assert (refused["ok"], refused["status"]) == (False, 403)
    assert "subscription is required" in refused["message"]
    assert (accepted["ok"], accepted["status"]) == (True, 200)
    assert dead.last_status == cp.STATUS_EXHAUSTED
    assert cp.resolve_entry("opencode-go").id == good.id
    assert accounts.check_pool_entry("opencode-go", "nope")["error"]


def test_the_go_check_sends_a_chat_request_because_models_accepts_a_dead_account(state, monkeypatch):
    seen = []

    def fake_http(url, headers, body=None):
        seen.append((url, body))
        return 403, "An active OpenCode Go subscription is required to use OpenCode Go models."

    monkeypatch.setattr(accounts, "_http_status", fake_http)

    status, _ = accounts._probe_request(accounts.find_provider("opencode-go"), "sk-any-aaaaaaaaaaaaa")

    assert status == 403
    url, body = seen[0]
    assert url.endswith("/chat/completions")
    assert body["max_tokens"] == 1 and body["messages"]


def test_one_more_key_through_the_api_goes_last_and_keeps_the_key_file(state):
    from types import SimpleNamespace

    from harness.api import providers as api

    svc = SimpleNamespace(diag=lambda *a, **k: None)
    code, first = api.post_auth_pools_add(
        {"provider": "opencode-go", "api_key": "sk-one-aaaaaaaaaaaa"}, svc)
    code2, second = api.post_auth_pools_add(
        {"provider": "opencode-go", "api_key": "sk-two-bbbbbbbbbbbb", "label": "spare"}, svc)

    assert (code, code2) == (200, 200)
    assert state["opencode-go"] == "sk-one-aaaaaaaaaaaa"
    assert [e["first"] for e in second["entries"]] == [True, False]
    assert second["entries"][1]["label"] == "spare"
    assert second["name"] == "OpenCode Go"


def test_the_pool_listing_names_the_providers_that_take_one_more_key(state):
    from harness.api import providers as api

    code, listing = api.get_auth_pools()

    assert code == 200
    takes = {row["provider"]: row["name"] for row in listing["key_providers"]}
    assert takes["opencode-go"] == "OpenCode Go"
    assert "openrouter" in takes and "cursor" in takes
    # Sign-in only, or a pool that no account row reads.
    assert "openai-codex" not in takes and "gemini" not in takes


def test_the_check_route_returns_the_answer_and_the_pool(state, monkeypatch):
    from harness.api import providers as api

    dead = cp.add_api_key("opencode-go", "sk-dead-aaaaaaaaaaaa")
    monkeypatch.setattr(accounts, "_probe_request", lambda provider, token: (403, "no plan"))

    code, reply = api.post_auth_pools_check({"provider": "opencode-go", "entry_id": dead.id})

    assert code == 200 and reply["ok"] is True
    assert reply["check"] == {**reply["check"], "ok": False, "status": 403, "message": "no plan"}
    assert reply["entries"][0]["usable"] is False
    assert api.post_auth_pools_check({"provider": "opencode-go"})[0] == 400
