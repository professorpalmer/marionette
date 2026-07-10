import io
import json
import urllib.request
import pytest

from pmharness.drivers.anthropic import AnthropicDriver
import pmharness.drivers.retry
from harness.pilot import parse_tool_calls

@pytest.fixture(autouse=True)
def mock_retry_sleep(monkeypatch):
    orig_with_retry = pmharness.drivers.retry.with_retry
    def mock_with_retry(fn, **kwargs):
        kwargs["sleep"] = lambda x: None
        return orig_with_retry(fn, **kwargs)
    monkeypatch.setattr(pmharness.drivers.retry, "with_retry", mock_with_retry)

def test_anthropic_supports_streaming():
    driver = AnthropicDriver(
        name="claude-frontier",
        model="claude-3-5-sonnet",
        api_key_env="ANTHROPIC_API_KEY"
    )
    # AnthropicDriver now streams via SSE (chat_stream); prose renders token-by-token.
    assert driver.supports_streaming is True
    assert callable(driver.chat_stream)

def test_anthropic_chat_tools_and_system(monkeypatch):
    driver = AnthropicDriver(
        name="claude-frontier",
        model="claude-3-5-sonnet",
        api_key_env="ANTHROPIC_API_KEY",
        enable_prompt_cache=True
    )
    driver._key = lambda: "fake-key"

    captured_reqs = []

    def mock_urlopen(req, timeout=None):
        captured_reqs.append(req)
        resp_data = {
            "content": [{"type": "text", "text": "Hello world"}],
            "usage": {
                "input_tokens": 120,
                "output_tokens": 40,
                "cache_creation_input_tokens": 50,
                "cache_read_input_tokens": 20
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

    tools_schema = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read file contents",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"}
                    },
                    "required": ["path"]
                }
            }
        }
    ]

    messages = [{"role": "user", "content": "hello"}]
    resp = driver.chat(messages, tools=tools_schema, system="my system prompt")

    assert len(captured_reqs) == 1
    req = captured_reqs[0]

    # Verify body
    body_data = json.loads(req.data.decode("utf-8"))
    
    # Assert system cached ephemeral block shape (stable = 1h TTL by default)
    assert body_data["system"] == [
        {
            "type": "text",
            "text": "my system prompt",
            "cache_control": {"type": "ephemeral", "ttl": "1h"}
        }
    ]
    # Last tool is the other stable breakpoint; history (single user msg) stays 5m
    assert body_data["tools"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    hist = body_data["messages"][-1]["content"][-1]["cache_control"]
    assert hist == {"type": "ephemeral"}
    assert "ttl" not in hist

    # Assert tools conversion schema shape {name, description, input_schema}
    assert len(body_data["tools"]) == 1
    t = body_data["tools"][0]
    assert t["name"] == "read_file"
    assert t["description"] == "Read file contents"
    assert t["input_schema"] == {
        "type": "object",
        "properties": {
            "path": {"type": "string"}
        },
        "required": ["path"]
    }

    # Assert tool_choice auto
    assert body_data["tool_choice"] == {"type": "auto"}

    # Assert usage metadata
    assert resp.meta.get("cache_write_tokens") == 50
    assert resp.meta.get("cache_read_tokens") == 20
    assert resp.tokens_in == 120
    assert resp.tokens_out == 40

def test_anthropic_chat_message_translation(monkeypatch):
    driver = AnthropicDriver(
        name="claude-frontier",
        model="claude-3-5-sonnet",
        api_key_env="ANTHROPIC_API_KEY",
        enable_prompt_cache=False
    )
    driver._key = lambda: "fake-key"

    captured_reqs = []

    def mock_urlopen(req, timeout=None):
        captured_reqs.append(req)
        resp_data = {
            "content": [{"type": "text", "text": "Done"}],
            "usage": {},
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

    # OpenAI-style history with assistant tool_calls message and a role: tool message
    messages = [
        {"role": "user", "content": "please read foo.txt"},
        {
            "role": "assistant",
            "content": "Sure, let me read it.",
            "tool_calls": [
                {
                    "id": "tc-123",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path": "foo.txt"}'
                    }
                }
            ]
        },
        {
            "role": "tool",
            "tool_call_id": "tc-123",
            "content": "file contents of foo"
        }
    ]

    driver.chat(messages)

    assert len(captured_reqs) == 1
    req = captured_reqs[0]
    body_data = json.loads(req.data.decode("utf-8"))

    # Assert conversion to Anthropic message shape and role/block structure
    anthropic_msgs = body_data["messages"]
    assert len(anthropic_msgs) == 3

    # First user message
    assert anthropic_msgs[0]["role"] == "user"
    assert anthropic_msgs[0]["content"] == [{"type": "text", "text": "please read foo.txt"}]

    # Second assistant message with text + tool_use
    assert anthropic_msgs[1]["role"] == "assistant"
    assert len(anthropic_msgs[1]["content"]) == 2
    assert anthropic_msgs[1]["content"][0] == {"type": "text", "text": "Sure, let me read it."}
    assert anthropic_msgs[1]["content"][1] == {
        "type": "tool_use",
        "id": "tc-123",
        "name": "read_file",
        "input": {"path": "foo.txt"}
    }

    # Third user tool result message
    assert anthropic_msgs[2]["role"] == "user"
    assert anthropic_msgs[2]["content"] == [
        {
            "type": "tool_result",
            "tool_use_id": "tc-123",
            "content": "file contents of foo"
        }
    ]

def test_anthropic_canned_response_parsing(monkeypatch):
    driver = AnthropicDriver(
        name="claude-frontier",
        model="claude-3-5-sonnet",
        api_key_env="ANTHROPIC_API_KEY",
        enable_prompt_cache=False
    )
    driver._key = lambda: "fake-key"

    def mock_urlopen(req, timeout=None):
        resp_data = {
            "content": [
                {"type": "text", "text": "Thinking in prose. <thinking>Let me think</thinking> Here is my action:"},
                {
                    "type": "tool_use",
                    "id": "tc-abc",
                    "name": "mcp_server_write_file",
                    "input": {
                        "path": "test.txt",
                        "content": "hello text"
                    }
                }
            ],
            "usage": {
                "input_tokens": 150,
                "output_tokens": 80
            },
            "stop_reason": "tool_use"
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

    resp = driver.chat([{"role": "user", "content": "write a file"}])

    # Assert response text, reasoning extraction, and tool call adaptation
    assert resp.text == "Thinking in prose.  Here is my action:"
    assert resp.meta["reasoning"] == "Let me think"
    assert resp.meta["finish_reason"] == "tool_use"

    tool_calls = resp.meta["tool_calls"]
    assert len(tool_calls) == 1
    tc = tool_calls[0]
    assert tc["id"] == "tc-abc"
    assert tc["type"] == "function"
    assert tc["function"]["name"] == "mcp_server_write_file"
    
    # Assert arguments are JSON string
    args_str = tc["function"]["arguments"]
    assert isinstance(args_str, str)
    args_dict = json.loads(args_str)
    assert args_dict == {"path": "test.txt", "content": "hello text"}

    # Assert parse_tool_calls maps it to PilotAction correctly
    actions = parse_tool_calls(tool_calls)
    assert len(actions) == 1
    act = actions[0]
    assert act.kind == "call_mcp"
    assert act.tool == "server.write_file"
    assert act.arguments == {"path": "test.txt", "content": "hello text"}
    assert act.tool_call_id == "tc-abc"
