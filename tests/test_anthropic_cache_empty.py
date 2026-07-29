"""Prompt-cache markers must never land on an empty text block.

Real report: after enabling history prefix caching, Anthropic returned
HTTP 400 'cache_control cannot be set for empty text blocks' -- the marker was
placed on a message whose text was empty. This guards that an empty / whitespace
last message does not get a cache_control marker (which 400s the whole request).

Also: native AnthropicDriver must walk history markers back to cache carriers
(shared ``history_cache_carriers``) so empty/whitespace tails do not waste the
≤2 history breakpoint slots.

Hermetic: builds the request body directly, no network.
"""
from pmharness.drivers.anthropic import AnthropicDriver
from pmharness.drivers.prompt_cache import history_cache_carriers


def _driver():
    return AnthropicDriver("claude", "claude-opus-4-8", enable_prompt_cache=True)


def _has_empty_marked_block(body) -> bool:
    for m in body.get("messages", []):
        content = m.get("content")
        if isinstance(content, list):
            for blk in content:
                if (isinstance(blk, dict) and blk.get("cache_control")
                        and blk.get("type") == "text"
                        and not str(blk.get("text") or "").strip()):
                    return True
    return False


def _marker_on(msg):
    content = msg.get("content")
    if isinstance(content, list):
        for blk in content:
            if isinstance(blk, dict) and blk.get("cache_control"):
                return blk["cache_control"]
    return None


def _count_message_cache_markers(body) -> int:
    n = 0
    for m in body.get("messages", []):
        if _marker_on(m) is not None:
            n += 1
    return n


def test_empty_last_message_is_not_cache_marked():
    d = _driver()
    msgs = [
        {"role": "user", "content": "do a thing"},
        {"role": "assistant", "content": "working"},
        {"role": "user", "content": ""},  # empty -> must NOT be marked
    ]
    body = d._build_body(msgs, tools=None, system="SYS")
    assert not _has_empty_marked_block(body), "cache_control set on an empty text block"
    # Walk-back still spends both history slots on earlier carriers.
    assert _marker_on(body["messages"][0]) == {"type": "ephemeral", "ttl": "1h"}
    assert _marker_on(body["messages"][1]) == {"type": "ephemeral", "ttl": "1h"}
    assert _marker_on(body["messages"][2]) is None


def test_whitespace_only_message_is_not_marked():
    d = _driver()
    msgs = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "   "}]
    body = d._build_body(msgs, tools=None, system="SYS")
    assert not _has_empty_marked_block(body)
    # Sole carrier is the non-empty user turn; whitespace tail wastes no slot.
    assert _marker_on(body["messages"][0]) == {"type": "ephemeral", "ttl": "1h"}
    assert _marker_on(body["messages"][1]) is None


def test_normal_message_still_gets_cached():
    d = _driver()
    msgs = [{"role": "user", "content": "a real prompt with content"}]
    body = d._build_body(msgs, tools=None, system="SYS")
    # The non-empty last message SHOULD carry a marker (caching still works).
    last = body["messages"][-1]["content"]
    assert isinstance(last, list) and last[-1].get("cache_control")
    # AGNT-style all-1h: history and stable system both get ttl:1h.
    assert last[-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert body["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


def test_native_history_walks_back_past_empty_and_whitespace_tail():
    """Empty/whitespace tail envelopes must not steal the two history breakpoints."""
    d = _driver()
    msgs = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
        {"role": "user", "content": "third"},
        {"role": "assistant", "content": ""},
        {"role": "user", "content": "   "},
    ]
    body = d._build_body(msgs, tools=None, system="SYS")
    out = body["messages"]
    assert not _has_empty_marked_block(body)
    # Last two carriers are "second" and "third"; empty/whitespace tails unmarked.
    assert _marker_on(out[0]) is None
    assert _marker_on(out[1]) == {"type": "ephemeral", "ttl": "1h"}
    assert _marker_on(out[2]) == {"type": "ephemeral", "ttl": "1h"}
    assert _marker_on(out[3]) is None
    assert _marker_on(out[4]) is None
    assert _count_message_cache_markers(body) == 2
    # Shared helper agrees with what the driver stamped.
    carriers = history_cache_carriers(out, limit=2)
    assert carriers == [out[1], out[2]]


def test_native_empty_tail_preserves_four_breakpoint_budget():
    """system + last-tool + two walked-back history carriers ≤ 4."""
    d = _driver()
    tools = [{
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "desc",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }]
    msgs = [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
        {"role": "user", "content": "c"},
        {"role": "assistant", "content": ""},
        {"role": "user", "content": "\t"},
    ]
    body = d._build_body(msgs, tools=tools, system="SYS")
    n = 0
    if isinstance(body.get("system"), list):
        n += sum(1 for b in body["system"] if isinstance(b, dict) and b.get("cache_control"))
    n += _count_message_cache_markers(body)
    for t in body.get("tools") or []:
        if isinstance(t, dict) and t.get("cache_control"):
            n += 1
    assert n <= 4
    assert body["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert body["tools"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert _marker_on(body["messages"][1]) is not None  # assistant b
    assert _marker_on(body["messages"][2]) is not None  # user c
    assert _marker_on(body["messages"][3]) is None
    assert _marker_on(body["messages"][4]) is None


def test_openai_compat_empty_system_and_tool_envelopes_never_marked():
    """Shared stamper: empty system/tool envelopes must not consume breakpoints."""
    from pmharness.drivers.prompt_cache import apply_openai_compat_cache_control

    body = {
        "model": "anthropic/claude-sonnet-4",
        "messages": [
            {"role": "system", "content": ""},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "   "},
            {"role": "tool", "content": ""},
            {"role": "user", "content": "again"},
        ],
        "tools": [{}, {"type": "function", "function": {"name": "ok", "parameters": {}}}],
    }
    apply_openai_compat_cache_control(body, model="anthropic/claude-sonnet-4")
    assert not any(
        isinstance(b, dict) and b.get("cache_control")
        and b.get("type") == "text"
        and not str(b.get("text") or "").strip()
        for m in body["messages"]
        for b in (m.get("content") if isinstance(m.get("content"), list) else [])
    )
    assert "cache_control" not in body["tools"][0]
    assert body["tools"][1].get("cache_control")
    assert _has_empty_marked_block(body) is False


def _count_all_cache_markers(body) -> int:
    n = 0
    system = body.get("system")
    if isinstance(system, list):
        n += sum(1 for b in system if isinstance(b, dict) and b.get("cache_control"))
    elif isinstance(system, dict) and system.get("cache_control"):
        n += 1
    n += _count_message_cache_markers(body)
    for t in body.get("tools") or []:
        if isinstance(t, dict) and t.get("cache_control"):
            n += 1
    return n


def test_native_whitespace_system_not_cache_marked():
    """Whitespace-only system must not receive a stable cache_control breakpoint."""
    d = _driver()
    msgs = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
        {"role": "user", "content": "third"},
    ]
    body = d._build_body(msgs, tools=None, system="  \t\n  ")
    system = body.get("system")
    if isinstance(system, list):
        assert not any(
            isinstance(b, dict) and b.get("cache_control") for b in system
        )
    else:
        assert not (isinstance(system, dict) and system.get("cache_control"))
    # History markers still land on the two carriers (system slot unused).
    assert _marker_on(body["messages"][-1]) == {"type": "ephemeral", "ttl": "1h"}
    assert _marker_on(body["messages"][-2]) == {"type": "ephemeral", "ttl": "1h"}
    assert _count_all_cache_markers(body) == 2


def test_native_empty_name_trailing_tool_walks_back():
    """Empty/invalid trailing tool schemas must not be marked; walk back to last eligible."""
    d = _driver()
    tools = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "desc",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "",
                "description": "empty name",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "   ",
                "description": "whitespace name",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
    ]
    body = d._build_body(
        [{"role": "user", "content": "hi"}],
        tools=tools,
        system="SYS",
    )
    out_tools = body["tools"]
    assert out_tools[0].get("cache_control") == {"type": "ephemeral", "ttl": "1h"}
    assert "cache_control" not in out_tools[1]
    assert "cache_control" not in out_tools[2]
    assert body["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


def test_native_stable_markers_respect_four_breakpoint_budget():
    """Whitespace system + empty trailing tool must not burn the ≤4 budget."""
    d = _driver()
    tools = [
        {
            "type": "function",
            "function": {
                "name": "ok_tool",
                "description": "desc",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "",
                "description": "bad",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
    ]
    msgs = [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
        {"role": "user", "content": "c"},
    ]
    body = d._build_body(msgs, tools=tools, system="\n\t ")
    n = _count_all_cache_markers(body)
    assert n <= 4
    # System skipped; last eligible tool + two history carriers = 3.
    assert n == 3
    system = body.get("system")
    if isinstance(system, list):
        assert not any(isinstance(b, dict) and b.get("cache_control") for b in system)
    assert body["tools"][0].get("cache_control") == {"type": "ephemeral", "ttl": "1h"}
    assert "cache_control" not in body["tools"][1]
    assert _marker_on(body["messages"][1]) is not None
    assert _marker_on(body["messages"][2]) is not None
