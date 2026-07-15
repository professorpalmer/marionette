import io
import json
import urllib.request
import urllib.error
import pytest

from pmharness.drivers.openai_compat import OpenAICompatDriver
from pmharness.drivers.anthropic import AnthropicDriver
import pmharness.drivers.retry


@pytest.fixture(autouse=True)
def mock_retry_sleep(monkeypatch):
    orig_with_retry = pmharness.drivers.retry.with_retry
    def mock_with_retry(fn, **kwargs):
        kwargs["sleep"] = lambda x: None
        return orig_with_retry(fn, **kwargs)
    monkeypatch.setattr(pmharness.drivers.retry, "with_retry", mock_with_retry)


def test_anthropic_prompt_caching_enabled(monkeypatch):
    driver = AnthropicDriver(
        name="claude-3-haiku",
        model="claude-3-haiku-20240307",
        base_url="https://api.anthropic.com/v1",
        api_key_env="ANTHROPIC_API_KEY",
        enable_prompt_cache=True
    )
    driver._key = lambda: "fake-key"

    captured_reqs = []

    def mock_urlopen(req, timeout=None):
        captured_reqs.append(req)
        resp_data = {
            "content": [{"type": "text", "text": "Anthropic caching works"}],
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_creation_input_tokens": 80,
                "cache_read_input_tokens": 10
            },
            "stop_reason": "end_turn"
        }
        res_fp = io.BytesIO(json.dumps(resp_data).encode("utf-8"))
        class FakeResponse:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def read(self):
                return res_fp.read()
        return FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)

    resp = driver.complete("test user prompt", system="my custom system prompt")

    assert len(captured_reqs) == 1
    req = captured_reqs[0]

    # Check Headers
    assert req.headers.get("Anthropic-beta") == "prompt-caching-2024-07-31"

    # Check Body -- stable system breakpoint defaults to extended 1h TTL
    body_data = json.loads(req.data.decode("utf-8"))
    assert body_data["system"] == [
        {
            "type": "text",
            "text": "my custom system prompt",
            "cache_control": {"type": "ephemeral", "ttl": "1h"}
        }
    ]

    # Check returned Response Meta values -- tokens_in is inclusive
    # (uncached input_tokens + cache write + cache read).
    assert resp.meta.get("cache_write_tokens") == 80
    assert resp.meta.get("cache_read_tokens") == 10
    assert resp.tokens_in == 190  # 100 + 80 + 10
    assert resp.tokens_out == 50
    assert resp.meta.get("cache_write_1h_tokens") == 80


def test_anthropic_prompt_caching_disabled(monkeypatch):
    driver = AnthropicDriver(
        name="claude-3-haiku",
        model="claude-3-haiku-20240307",
        base_url="https://api.anthropic.com/v1",
        api_key_env="ANTHROPIC_API_KEY",
        enable_prompt_cache=False
    )
    driver._key = lambda: "fake-key"

    captured_reqs = []

    def mock_urlopen(req, timeout=None):
        captured_reqs.append(req)
        resp_data = {
            "content": [{"type": "text", "text": "Anthropic caching disabled"}],
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50
            },
            "stop_reason": "end_turn"
        }
        res_fp = io.BytesIO(json.dumps(resp_data).encode("utf-8"))
        class FakeResponse:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def read(self):
                return res_fp.read()
        return FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)

    resp = driver.complete("test user prompt", system="my custom system prompt")

    assert len(captured_reqs) == 1
    req = captured_reqs[0]

    # Check Headers - anthropic-beta should NOT be present
    assert "Anthropic-beta" not in req.headers

    # Check Body - system prompt is a flat string
    body_data = json.loads(req.data.decode("utf-8"))
    assert body_data["system"] == "my custom system prompt"

    # Check returned Response Meta values
    assert resp.meta.get("cache_write_tokens") == 0
    assert resp.meta.get("cache_read_tokens") == 0


def test_openai_compat_prompt_caching(monkeypatch):
    driver = OpenAICompatDriver(
        name="openai-test",
        model="gpt-4o",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY"
    )
    driver._key = lambda: "fake-key"

    def mock_urlopen(req, timeout=None):
        resp_data = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "OpenAI content"
                },
                "finish_reason": "stop"
            }],
            "usage": {
                "prompt_tokens": 120,
                "completion_tokens": 40,
                "cost": 0.0123,
                "prompt_tokens_details": {
                    "cached_tokens": 90
                }
            }
        }
        res_fp = io.BytesIO(json.dumps(resp_data).encode("utf-8"))
        class FakeResponse:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def read(self):
                return res_fp.read()
        return FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)

    resp_complete = driver.complete("Hello", system="system prompt")
    assert resp_complete.meta.get("cache_read_tokens") == 90
    assert resp_complete.meta.get("provider_cost_usd") == pytest.approx(0.0123)

    resp_chat = driver.chat(messages=[{"role": "user", "content": "Hello"}], system="system prompt")
    assert resp_chat.meta.get("cache_read_tokens") == 90
    assert resp_chat.meta.get("provider_cost_usd") == pytest.approx(0.0123)


def test_openai_compat_prompt_caching_missing(monkeypatch):
    driver = OpenAICompatDriver(
        name="openai-test",
        model="gpt-4o",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY"
    )
    driver._key = lambda: "fake-key"

    def mock_urlopen(req, timeout=None):
        resp_data = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "OpenAI content"
                },
                "finish_reason": "stop"
            }],
            "usage": {
                "prompt_tokens": 120,
                "completion_tokens": 40
            }
        }
        res_fp = io.BytesIO(json.dumps(resp_data).encode("utf-8"))
        class FakeResponse:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def read(self):
                return res_fp.read()
        return FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)

    resp_complete = driver.complete("Hello", system="system prompt")
    assert resp_complete.meta.get("cache_read_tokens") == 0

    resp_chat = driver.chat(messages=[{"role": "user", "content": "Hello"}], system="system prompt")
    assert resp_chat.meta.get("cache_read_tokens") == 0


def test_openai_compat_chat_stream_prompt_caching(monkeypatch):
    driver = OpenAICompatDriver(
        name="openai-test",
        model="gpt-4o",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY"
    )
    driver._key = lambda: "fake-key"

    def mock_urlopen(req, timeout=None):
        chunks = [
            {"choices": [{"delta": {"content": "Hello "}}]},
            {"choices": [{"delta": {"content": "world!"}}]},
            {
                "choices": [],
                "usage": {
                    "prompt_tokens": 120,
                    "completion_tokens": 40,
                    "cost": 0.0042,
                    "prompt_tokens_details": {
                        "cached_tokens": 85
                    }
                }
            }
        ]
        
        lines = []
        for chunk in chunks:
            lines.append(f"data: {json.dumps(chunk)}\n".encode("utf-8"))
        lines.append(b"data: [DONE]\n")

        class FakeResponse:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def __iter__(self):
                return iter(lines)
            def read(self):
                return b"".join(lines)
        return FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)

    deltas = []
    resp_stream = driver.chat_stream(
        messages=[{"role": "user", "content": "Hello"}],
        system="system prompt",
        on_delta=lambda d: deltas.append(d)
    )

    assert "".join(deltas) == "Hello world!"
    assert resp_stream.meta.get("cache_read_tokens") == 85
    assert resp_stream.meta.get("provider_cost_usd") == pytest.approx(0.0042)


def _count_cache_markers(body: dict) -> int:
    """Count every cache_control marker across system, tools, and messages."""
    n = 0
    sysv = body.get("system")
    if isinstance(sysv, list):
        n += sum(1 for b in sysv if isinstance(b, dict) and b.get("cache_control"))
    for t in body.get("tools", []) or []:
        if isinstance(t, dict) and t.get("cache_control"):
            n += 1
    for m in body.get("messages", []) or []:
        c = m.get("content")
        if isinstance(c, list):
            n += sum(1 for b in c if isinstance(b, dict) and b.get("cache_control"))
    return n


def _mk_driver():
    return AnthropicDriver(
        name="claude-x", model="claude-x",
        base_url="https://api.anthropic.com/v1",
        api_key_env="ANTHROPIC_API_KEY", enable_prompt_cache=True,
    )


def _openai_tool(name: str = "x"):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "desc",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }


def _marker_on(msg):
    c = msg.get("content")
    if isinstance(c, list):
        for b in c:
            if isinstance(b, dict) and b.get("cache_control"):
                return b["cache_control"]
    return None


def test_history_uses_two_stable_breakpoints():
    # A multi-message history must carry a breakpoint on BOTH the last and the
    # second-to-last message so the growing prefix is reused as a cache READ next
    # turn (the multi-turn cost win), not just a single moving marker.
    d = _mk_driver()
    msgs = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
        {"role": "user", "content": "third"},
    ]
    body = d._build_body(msgs, tools=[_openai_tool()], system="sys")
    out = body["messages"]
    assert _marker_on(out[-1]) is not None, "last message must be cache-marked"
    assert _marker_on(out[-2]) is not None, "second-to-last message must be cache-marked (stable prefix)"


def test_all_claude_markers_default_to_1h_ttl():
    # AGNT-style all-1h: system + last-tool + both history markers get ttl:1h.
    d = _mk_driver()
    msgs = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
        {"role": "user", "content": "third"},
    ]
    body = d._build_body(msgs, tools=[_openai_tool("read_file")], system="sys")

    assert body["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert body["tools"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}

    hist_last = _marker_on(body["messages"][-1])
    hist_prev = _marker_on(body["messages"][-2])
    assert hist_last == {"type": "ephemeral", "ttl": "1h"}
    assert hist_prev == {"type": "ephemeral", "ttl": "1h"}


def test_env_5m_drops_ttl_on_stable_and_history_markers(monkeypatch):
    monkeypatch.setenv("HARNESS_ANTHROPIC_CACHE_TTL", "5m")
    d = _mk_driver()
    msgs = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
    ]
    body = d._build_body(msgs, tools=[_openai_tool()], system="sys")

    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "ttl" not in body["system"][0]["cache_control"]
    assert body["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "ttl" not in body["tools"][-1]["cache_control"]
    # History markers also lose ttl under the 5m/off override.
    assert _marker_on(body["messages"][-1]) == {"type": "ephemeral"}
    assert "ttl" not in _marker_on(body["messages"][-1])
    assert _marker_on(body["messages"][-2]) == {"type": "ephemeral"}
    assert "ttl" not in _marker_on(body["messages"][-2])


def test_total_cache_breakpoints_within_anthropic_limit():
    # Anthropic allows at most 4 cache_control breakpoints. system + last-tool +
    # two history markers = 4 exactly; never more.
    d = _mk_driver()
    msgs = [{"role": "user", "content": f"m{i}"} for i in range(6)]
    body = d._build_body(msgs, tools=[_openai_tool("a")], system="sys")
    assert _count_cache_markers(body) <= 4
    assert body["system"][0]["cache_control"].get("ttl") == "1h"
    assert body["tools"][-1]["cache_control"].get("ttl") == "1h"


def test_single_message_history_still_valid():
    # A one-message history must not crash and must still cache that message.
    d = _mk_driver()
    body = d._build_body([{"role": "user", "content": "only"}], tools=None, system="sys")
    assert _count_cache_markers(body) <= 4
    assert body["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert _marker_on(body["messages"][-1]) == {"type": "ephemeral", "ttl": "1h"}
