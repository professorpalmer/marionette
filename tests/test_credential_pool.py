"""Credential pool: select, rotate, plan-limit immediate rotate."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time

import pytest

from harness import credential_pool as cp


@pytest.fixture
def pool_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    cp.clear_pools_for_tests()
    yield tmp_path
    cp.clear_pools_for_tests()


def test_add_and_select_fill_first(pool_dir):
    a = cp.add_api_key("openrouter", "sk-aaa-1111111111", label="a")
    b = cp.add_api_key("openrouter", "sk-bbb-2222222222", label="b")
    tok = cp.resolve_token("openrouter")
    assert tok == a.access_token
    # fill_first keeps using first until exhausted
    assert cp.resolve_token("openrouter") == a.access_token
    assert a.id != b.id


def test_rotate_on_plan_limit(pool_dir):
    a = cp.add_api_key("cursor", "key-aaaa-11111111", label="cursor-1")
    b = cp.add_api_key("cursor", "key-bbbb-22222222", label="cursor-2")
    assert cp.resolve_token("cursor") == a.access_token
    nxt = cp.report_failure(
        "cursor",
        a.id,
        status_code=429,
        message="usage limit reached for your plan",
    )
    assert nxt == b.access_token


def test_persist_roundtrip(pool_dir):
    cp.add_api_key("openai", "sk-persist-abcdefgh", label="p")
    path = os.path.join(str(pool_dir), "auth_pool.json")
    assert os.path.isfile(path)
    cp.clear_pools_for_tests()
    tok = cp.resolve_token("openai")
    assert tok == "sk-persist-abcdefgh"
    data = json.loads(open(path, encoding="utf-8").read())
    assert "openai" in data["pools"]


def test_persist_hardens_auth_pool_file(pool_dir):
    """auth_pool.json must get the same owner-only ACL/mode as keys.json."""
    cp.add_api_key("openai", "sk-harden-abcdefgh", label="h")
    path = os.path.join(str(pool_dir), "auth_pool.json")
    assert os.path.isfile(path)
    if os.name == "nt":
        out = subprocess.run(
            ["icacls", path], capture_output=True, text=True, timeout=15
        ).stdout
        assert "BUILTIN\\Users" not in out
        assert "Authenticated Users" not in out
        user = os.environ.get("USERNAME", "")
        assert user and user.lower() in out.lower()
    else:
        assert os.stat(path).st_mode & 0o777 == 0o600


def test_concurrent_select_and_rotate_is_thread_safe(pool_dir):
    """select + mark_exhausted_and_rotate must serialize under the pool lock."""
    a = cp.add_api_key("openrouter", "sk-conc-aaaaaaaaaa", label="a")
    b = cp.add_api_key("openrouter", "sk-conc-bbbbbbbbbb", label="b")
    pool = cp.load_pool("openrouter")
    errors: list[BaseException] = []
    barrier = threading.Barrier(8)
    select_hits = {"n": 0}
    hits_lock = threading.Lock()

    def _select_burst() -> None:
        try:
            barrier.wait(timeout=5)
            for _ in range(40):
                chosen = pool.select()
                if chosen is not None:
                    with hits_lock:
                        select_hits["n"] += 1
        except BaseException as exc:  # noqa: BLE001 — collect for main thread
            errors.append(exc)

    def _rotate_burst() -> None:
        try:
            barrier.wait(timeout=5)
            for i in range(20):
                entry_id = a.id if i % 2 == 0 else b.id
                pool.mark_exhausted_and_rotate(
                    entry_id, error_code=429, message="usage limit reached"
                )
                # Reset via the public API (also lock-aware via persist paths)
                # so the next select can still succeed.
                pool.reset_cooldowns()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=_select_burst if i < 4 else _rotate_burst)
        for i in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive()
    assert errors == []
    # mark_exhausted and reset_cooldowns take separate lock acquisitions, so
    # select may briefly see an empty pool under concurrency. The invariant is
    # thread safety (no exceptions / corrupted counters), not a perfect hit rate.
    assert select_hits["n"] > 0
    # mark_exhausted_and_rotate also calls select(), so request_count exceeds
    # the direct-select hit counter — both paths must stay consistent ints.
    total = sum(e.request_count for e in pool.entries())
    assert total >= select_hits["n"]
    assert all(isinstance(e.request_count, int) and e.request_count >= 0
               for e in pool.entries())
    assert time.time() > 0


def test_public_list_masks_secret(pool_dir):
    cp.add_api_key("openrouter", "sk-or-v1-secrettoken99", label="or1")
    pub = cp.list_pool_public("openrouter")
    assert pub["entries"]
    assert "secrettoken99" not in json.dumps(pub)
    assert pub["entries"][0]["masked"]


def test_is_plan_limit_message():
    assert cp.is_plan_limit_message("ChatGPT usage limit reached")
    assert not cp.is_plan_limit_message("connection reset")


def test_xai_oauth_mirrors_xai_api_key_env(pool_dir, monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("XAI_OAUTH_TOKEN", raising=False)
    cp.add_oauth_entry(
        "xai-oauth",
        access_token="xai-oauth-mirror-token-99",
        label="sg",
    )
    assert os.environ.get("XAI_OAUTH_TOKEN") == "xai-oauth-mirror-token-99"
    assert os.environ.get("XAI_API_KEY") == "xai-oauth-mirror-token-99"
    assert cp.credential_satisfied("XAI_API_KEY") is True
    assert cp.peek_token_for_env("XAI_API_KEY") == "xai-oauth-mirror-token-99"
