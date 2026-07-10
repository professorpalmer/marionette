"""OpenAI-compat / OpenRouter explicit prompt-cache stamping.

OpenRouter requires EXPLICIT cache_control for Anthropic Claude and Alibaba
Qwen. Automatic-cache models (gpt, gemini, …) must NOT get invented markers.
Hermetic: builds request bodies via OpenAICompatDriver._prepare_body, no network.
"""
from __future__ import annotations

import copy

from pmharness.drivers.openai_compat import OpenAICompatDriver
from pmharness.drivers.prompt_cache import (
    apply_openai_compat_cache_control,
    explicit_cache_family,
)


def _driver(
    model: str,
    *,
    base_url: str = "https://openrouter.ai/api/v1",
    session_id: str | None = None,
) -> OpenAICompatDriver:
    return OpenAICompatDriver(
        name="test",
        model=model,
        base_url=base_url,
        api_key_env="OPENROUTER_API_KEY",
        session_id=session_id,
    )


def _openai_tool(name: str = "read_file") -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "desc",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }


def _sample_body(*, tools: bool = True) -> dict:
    body = {
        "model": "placeholder",
        "messages": [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "second"},
            {"role": "user", "content": "third"},
        ],
        "temperature": 0.0,
        "max_tokens": 100,
    }
    if tools:
        body["tools"] = [_openai_tool("a"), _openai_tool("b")]
        body["tool_choice"] = "auto"
    return body


def _count_cache_markers(body: dict) -> int:
    n = 0
    for m in body.get("messages") or []:
        c = m.get("content")
        if isinstance(c, list):
            n += sum(1 for b in c if isinstance(b, dict) and b.get("cache_control"))
        elif isinstance(c, dict) and c.get("cache_control"):
            n += 1
    for t in body.get("tools") or []:
        if isinstance(t, dict) and t.get("cache_control"):
            n += 1
    return n


def _marker_on_msg(msg: dict):
    c = msg.get("content")
    if isinstance(c, list):
        for b in c:
            if isinstance(b, dict) and b.get("cache_control"):
                return b["cache_control"]
    return None


def test_explicit_cache_family_detection():
    assert explicit_cache_family("anthropic/claude-sonnet-4") == "claude"
    assert explicit_cache_family("claude-opus-4-8") == "claude"
    assert explicit_cache_family("qwen/qwen3-coder-plus") == "qwen"
    assert explicit_cache_family("qwen3-coder-flash") == "qwen"
    assert explicit_cache_family("openai/gpt-4o") is None
    assert explicit_cache_family("google/gemini-2.5-pro") is None
    assert explicit_cache_family("deepseek/deepseek-chat") is None


def test_claude_via_openrouter_stamps_all_1h_including_history():
    d = _driver("anthropic/claude-sonnet-4")
    body = _sample_body()
    d._prepare_body(body, messages=body["messages"], system="You are helpful.")

    sys_msg = body["messages"][0]
    assert _marker_on_msg(sys_msg) == {"type": "ephemeral", "ttl": "1h"}
    assert body["tools"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}

    # History: last + second-to-last non-system messages also get ttl:1h
    assert _marker_on_msg(body["messages"][-1]) == {"type": "ephemeral", "ttl": "1h"}
    assert _marker_on_msg(body["messages"][-2]) == {"type": "ephemeral", "ttl": "1h"}
    assert _count_cache_markers(body) <= 4


def test_qwen_stamps_ephemeral_without_ttl():
    d = _driver("qwen/qwen3-coder-plus")
    body = _sample_body()
    d._prepare_body(body, messages=body["messages"], system="You are helpful.")

    assert _marker_on_msg(body["messages"][0]) == {"type": "ephemeral"}
    assert "ttl" not in _marker_on_msg(body["messages"][0])
    assert body["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "ttl" not in body["tools"][-1]["cache_control"]
    # Qwen: stable markers only — no history breakpoints
    assert _marker_on_msg(body["messages"][-1]) is None
    assert _marker_on_msg(body["messages"][-2]) is None


def test_gpt_gemini_grok_get_no_cache_control():
    for model in (
        "openai/gpt-4o",
        "google/gemini-2.5-pro",
        "gpt-4o",
        "x-ai/grok-3",
        "grok-3",
    ):
        d = _driver(model)
        body = _sample_body()
        d._prepare_body(body, messages=body["messages"], system="You are helpful.")
        assert _count_cache_markers(body) == 0, f"{model} must not invent cache_control"


def test_kill_switch_disables_all_stamping(monkeypatch):
    monkeypatch.setenv("HARNESS_PROMPT_CACHE", "0")
    d = _driver("anthropic/claude-sonnet-4")
    body = _sample_body()
    d._prepare_body(body, messages=body["messages"], system="sys")
    assert _count_cache_markers(body) == 0


def test_anthropic_cache_ttl_5m_drops_ttl_on_stable_and_history(monkeypatch):
    monkeypatch.setenv("HARNESS_ANTHROPIC_CACHE_TTL", "5m")
    d = _driver("anthropic/claude-3.5-sonnet")
    body = _sample_body()
    d._prepare_body(body, messages=body["messages"], system="sys")
    assert _marker_on_msg(body["messages"][0]) == {"type": "ephemeral"}
    assert "ttl" not in _marker_on_msg(body["messages"][0])
    assert body["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "ttl" not in body["tools"][-1]["cache_control"]
    assert _marker_on_msg(body["messages"][-1]) == {"type": "ephemeral"}
    assert "ttl" not in _marker_on_msg(body["messages"][-1])
    assert _marker_on_msg(body["messages"][-2]) == {"type": "ephemeral"}
    assert "ttl" not in _marker_on_msg(body["messages"][-2])


def test_openrouter_session_id_from_env(monkeypatch):
    monkeypatch.setenv("HARNESS_SESSION_ID", "sess-sticky-42")
    d = _driver("anthropic/claude-sonnet-4")
    body = _sample_body(tools=False)
    d._prepare_body(body, messages=body["messages"], system="sys")
    assert body.get("session_id") == "sess-sticky-42"


def test_openrouter_session_id_from_kwarg(monkeypatch):
    monkeypatch.delenv("HARNESS_SESSION_ID", raising=False)
    d = _driver("qwen/qwen3-coder-plus", session_id=None)
    body = _sample_body(tools=False)
    d._prepare_body(
        body,
        messages=body["messages"],
        system="sys",
        session_id="explicit-sid",
    )
    assert body.get("session_id") == "explicit-sid"


def test_non_openrouter_skips_session_id(monkeypatch):
    monkeypatch.setenv("HARNESS_SESSION_ID", "should-not-appear")
    d = _driver("anthropic/claude-sonnet-4", base_url="https://api.openai.com/v1")
    body = _sample_body(tools=False)
    d._prepare_body(body, messages=body["messages"], system="sys")
    assert "session_id" not in body


def test_empty_system_text_not_marked():
    body = {
        "model": "anthropic/claude-sonnet-4",
        "messages": [
            {"role": "system", "content": "   "},
            {"role": "user", "content": "hi"},
        ],
    }
    apply_openai_compat_cache_control(body, model="anthropic/claude-sonnet-4")
    assert _marker_on_msg(body["messages"][0]) is None
    assert _marker_on_msg(body["messages"][1]) == {"type": "ephemeral", "ttl": "1h"}


def test_prepare_body_is_idempotent_safe():
    """Calling apply twice should not explode; second pass re-stamps same markers."""
    body = _sample_body()
    body["model"] = "anthropic/claude-sonnet-4"
    apply_openai_compat_cache_control(body, model="anthropic/claude-sonnet-4")
    first = copy.deepcopy(body)
    apply_openai_compat_cache_control(body, model="anthropic/claude-sonnet-4")
    assert _count_cache_markers(body) == _count_cache_markers(first)
    assert _count_cache_markers(body) <= 4
