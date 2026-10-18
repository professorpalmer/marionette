"""A plain 429 cools one key for one model, not for every model."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

import pytest

from harness import credential_pool as cp
from pmharness.drivers.openai_compat import OpenAICompatDriver


@pytest.fixture
def pool_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    cp.clear_pools_for_tests()
    yield tmp_path
    cp.clear_pools_for_tests()


def _two_keys():
    a = cp.add_api_key("openrouter", "sk-aaa-1111111111", label="a")
    b = cp.add_api_key("openrouter", "sk-bbb-2222222222", label="b")
    return a, b


def _for(model):
    return cp.resolve_entry("openrouter", model).access_token


def test_model_429_moves_only_that_model(pool_dir):
    a, b = _two_keys()
    nxt = cp.report_failure("openrouter", a.id, status_code=429,
                            message="rate limited", model="kimi-k3")
    assert nxt == b.access_token
    assert _for("kimi-k3") == b.access_token
    assert _for("deepseek-v4-pro") == a.access_token
    assert cp.resolve_entry("openrouter").access_token == a.access_token
    entry = next(e for e in cp.load_pool("openrouter").entries() if e.id == a.id)
    assert (entry.last_status or cp.STATUS_OK) == cp.STATUS_OK


def test_cooldown_follows_retry_after_and_expires(pool_dir, monkeypatch):
    a, b = _two_keys()
    clock = {"t": 1000.0}
    monkeypatch.setattr(cp.time, "time", lambda: clock["t"])
    cp.report_failure("openrouter", a.id, status_code=429, message="slow down",
                      model="kimi-k3", retry_after=5)
    assert _for("kimi-k3") == b.access_token
    clock["t"] += 6
    assert _for("kimi-k3") == a.access_token


def test_default_cooldown_and_cap(pool_dir, monkeypatch):
    a, _b = _two_keys()
    clock = {"t": 1000.0}
    monkeypatch.setattr(cp.time, "time", lambda: clock["t"])
    cp.report_failure("openrouter", a.id, status_code=429, message="x", model="m1")
    cp.report_failure("openrouter", a.id, status_code=429, message="x", model="m2",
                      retry_after=10 ** 9)
    cooling = next(e for e in cp.load_pool("openrouter").entries() if e.id == a.id).extra[cp.MODEL_COOLDOWNS]
    assert cooling["m1"] == 1000.0 + cp.MODEL_COOLDOWN_429
    assert cooling["m2"] == 1000.0 + cp.EXHAUSTED_TTL_429


def test_cooldown_persists_and_expired_entries_are_dropped(pool_dir, monkeypatch):
    a, _b = _two_keys()
    clock = {"t": 1000.0}
    monkeypatch.setattr(cp.time, "time", lambda: clock["t"])
    cp.report_failure("openrouter", a.id, status_code=429, message="x", model="old", retry_after=2)
    clock["t"] += 10
    cp.report_failure("openrouter", a.id, status_code=429, message="x", model="new", retry_after=30)
    stored = json.loads((pool_dir / "credential_pool.json").read_text()) if (pool_dir / "credential_pool.json").exists() else None
    cp.clear_pools_for_tests()
    entry = next(e for e in cp.load_pool("openrouter").entries() if e.id == a.id)
    assert set(entry.extra[cp.MODEL_COOLDOWNS]) == {"new"}
    assert stored is None or stored["version"] == 1


def test_plan_limit_429_still_benches_the_whole_key(pool_dir):
    a, b = _two_keys()
    cp.report_failure("openrouter", a.id, status_code=429,
                      message="usage limit reached for your plan", model="kimi-k3")
    assert _for("deepseek-v4-pro") == b.access_token


def test_429_without_a_model_keeps_the_old_whole_key_rule(pool_dir):
    a, b = _two_keys()
    cp.report_failure("openrouter", a.id, status_code=429, message="rate limited")
    assert _for("deepseek-v4-pro") == b.access_token


class _FakeHTTPError(urllib.error.HTTPError):
    def __init__(self, code: int, body: str):
        super().__init__("https://example.test/v1/chat/completions", code, "err",
                         hdrs=None, fp=io.BytesIO(body.encode("utf-8")))


def test_driver_429_on_one_model_leaves_the_key_for_another(pool_dir, monkeypatch):
    a, b = _two_keys()
    ok_body = json.dumps({
        "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }).encode("utf-8")

    class _Ok:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return ok_body

    seen = []

    def fake_urlopen(req, timeout=None):
        body = json.loads(req.data.decode("utf-8"))
        auth = req.get_header("Authorization") or ""
        seen.append((body["model"], auth.split()[-1]))
        if body["model"] == "kimi-k3" and a.access_token in auth:
            raise _FakeHTTPError(429, "rate limited for this model, retry after 7 seconds")
        return _Ok()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    def drv(model):
        return OpenAICompatDriver(name="or", model=model, base_url="https://example.test/v1",
                                  api_key_env="OPENROUTER_API_KEY")

    first = drv("kimi-k3").complete("ping")
    assert first.text == "hi", (first.error, seen)
    assert drv("deepseek-v4-pro").complete("ping").text == "hi"
    assert seen == [
        ("kimi-k3", a.access_token),
        ("kimi-k3", b.access_token),
        ("deepseek-v4-pro", a.access_token),
    ]


def test_rotate_to_another_key_does_not_wait_out_the_old_retry_after(pool_dir):
    a, b = _two_keys()
    driver = OpenAICompatDriver(name="or", model="kimi-k3", base_url="https://example.test/v1",
                                api_key_env="OPENROUTER_API_KEY")
    assert driver._key() == a.access_token
    sleeps = []
    nxt = driver._pool_rotate_on_http_error(429, "retry after 7 seconds", sleep=sleeps.append)
    assert nxt == b.access_token
    assert driver._pool_entry_id == b.id
    assert sleeps == [0.25]
