"""Prompt-cache markers must never land on an empty text block.

Real report: after enabling history prefix caching, Anthropic returned
HTTP 400 'cache_control cannot be set for empty text blocks' -- the marker was
placed on a message whose text was empty. This guards that an empty / whitespace
last message does not get a cache_control marker (which 400s the whole request).

Hermetic: builds the request body directly, no network.
"""
from pmharness.drivers.anthropic import AnthropicDriver


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


def test_empty_last_message_is_not_cache_marked():
    d = _driver()
    msgs = [
        {"role": "user", "content": "do a thing"},
        {"role": "assistant", "content": "working"},
        {"role": "user", "content": ""},  # empty -> must NOT be marked
    ]
    body = d._build_body(msgs, tools=None, system="SYS")
    assert not _has_empty_marked_block(body), "cache_control set on an empty text block"


def test_whitespace_only_message_is_not_marked():
    d = _driver()
    msgs = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "   "}]
    body = d._build_body(msgs, tools=None, system="SYS")
    assert not _has_empty_marked_block(body)


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
