import io
import json
import urllib.request
import pytest

from pmharness.drivers.gemini import GeminiDriver
import pmharness.drivers.retry
from pmharness import registry
from harness.pilot import parse_tool_calls


@pytest.fixture(autouse=True)
def mock_retry_sleep(monkeypatch):
    orig_with_retry = pmharness.drivers.retry.with_retry
    def mock_with_retry(fn, **kwargs):
        kwargs["sleep"] = lambda x: None
        return orig_with_retry(fn, **kwargs)
    monkeypatch.setattr(pmharness.drivers.retry, "with_retry", mock_with_retry)


def test_gemini_supports_streaming():
    driver = GeminiDriver(
        name="gemini-3.5-flash",
        model="gemini-3.5-flash",
        api_key_env="GEMINI_API_KEY"
    )
    assert driver.supports_streaming is False


def test_gemini_sanitize_schema():
    driver = GeminiDriver(
        name="gemini-3.5-flash",
        model="gemini-3.5-flash",
        api_key_env="GEMINI_API_KEY"
    )
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "path": {
                "type": "string",
                "additionalProperties": False
            }
        },
        "required": ["path"]
    }
    sanitized = driver._sanitize_schema(schema)
    assert "$schema" not in sanitized
    assert "additionalProperties" not in sanitized
    assert "additionalProperties" not in sanitized["properties"]["path"]
    assert sanitized["type"] == "object"
    assert sanitized["properties"]["path"]["type"] == "string"


def test_gemini_chat_tools_and_system(monkeypatch):
    driver = GeminiDriver(
        name="gemini-3.5-flash",
        model="gemini-3.5-flash",
        api_key_env="GEMINI_API_KEY"
    )
    driver._key = lambda: "fake-gemini-key"

    captured_reqs = []

    def mock_urlopen(req, timeout=None):
        captured_reqs.append(req)
        resp_data = {
            "candidates": [
                {
                    "content": {
                        "parts": [{"text": "Hello, here to assist."}],
                        "role": "model"
                    },
                    "finishReason": "STOP"
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 150,
                "candidatesTokenCount": 50
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

    tools_schema = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read file contents",
                "parameters": {
                    "$schema": "some-schema",
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "path": {"type": "string"}
                    },
                    "required": ["path"]
                }
            }
        }
    ]

    messages = [{"role": "user", "content": "hello"}]
    resp = driver.chat(messages, tools=tools_schema, system="my gemini system prompt")

    assert len(captured_reqs) == 1
    req = captured_reqs[0]

    # Verify url has :generateContent and the API key
    assert ":generateContent" in req.full_url
    assert "key=fake-gemini-key" in req.full_url

    # Verify body
    body_data = json.loads(req.data.decode("utf-8"))
    
    # Assert systemInstruction shape
    assert body_data["systemInstruction"] == {
        "parts": [{"text": "my gemini system prompt"}]
    }

    # Assert tools conversion schema shape {name, description, parameters}
    assert len(body_data["tools"]) == 1
    t = body_data["tools"][0]
    assert len(t["function_declarations"]) == 1
    fd = t["function_declarations"][0]
    assert fd["name"] == "read_file"
    assert fd["description"] == "Read file contents"
    assert "$schema" not in fd["parameters"]
    assert "additionalProperties" not in fd["parameters"]
    assert fd["parameters"]["type"] == "object"

    # Assert tool_config mode AUTO
    assert body_data["tool_config"] == {
        "function_calling_config": {"mode": "AUTO"}
    }

    # Assert usage metadata
    assert resp.tokens_in == 150
    assert resp.tokens_out == 50


def test_gemini_chat_message_translation(monkeypatch):
    driver = GeminiDriver(
        name="gemini-3.5-flash",
        model="gemini-3.5-flash",
        api_key_env="GEMINI_API_KEY"
    )
    driver._key = lambda: "fake-gemini-key"

    captured_reqs = []

    def mock_urlopen(req, timeout=None):
        captured_reqs.append(req)
        resp_data = {
            "candidates": [
                {
                    "content": {
                        "parts": [{"text": "Done"}],
                        "role": "model"
                    },
                    "finishReason": "STOP"
                }
            ]
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

    # Assert conversion to Gemini content shape and role/parts structure
    gemini_contents = body_data["contents"]
    assert len(gemini_contents) == 3

    # First user message
    assert gemini_contents[0]["role"] == "user"
    assert gemini_contents[0]["parts"] == [{"text": "please read foo.txt"}]

    # Second assistant message with text + tool_use
    assert gemini_contents[1]["role"] == "model"
    assert len(gemini_contents[1]["parts"]) == 2
    assert gemini_contents[1]["parts"][0] == {"text": "Sure, let me read it."}
    assert gemini_contents[1]["parts"][1] == {
        "functionCall": {
            "name": "read_file",
            "args": {"path": "foo.txt"}
        }
    }

    # Third user tool result message (functionResponse)
    assert gemini_contents[2]["role"] == "user"
    assert gemini_contents[2]["parts"] == [
        {
            "functionResponse": {
                "name": "read_file",
                "response": {
                    "content": "file contents of foo"
                }
            }
        }
    ]


def test_gemini_canned_response_parsing(monkeypatch):
    driver = GeminiDriver(
        name="gemini-3.5-flash",
        model="gemini-3.5-flash",
        api_key_env="GEMINI_API_KEY"
    )
    driver._key = lambda: "fake-gemini-key"

    def mock_urlopen(req, timeout=None):
        resp_data = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": "Writing a file now..."},
                            {
                                "functionCall": {
                                    "name": "mcp_server_write_file",
                                    "args": {
                                        "path": "test.txt",
                                        "content": "hello text"
                                    }
                                }
                            }
                        ],
                        "role": "model"
                    },
                    "finishReason": "STOP"
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 150,
                "candidatesTokenCount": 80
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

    resp = driver.chat([{"role": "user", "content": "write a file"}])

    # Assert response text, reasoning is empty, and tool call adaptation
    assert resp.text == "Writing a file now..."
    assert resp.meta["reasoning"] == ""
    assert resp.meta["finish_reason"] == "STOP"

    tool_calls = resp.meta["tool_calls"]
    assert len(tool_calls) == 1
    tc = tool_calls[0]
    assert tc["id"] == "call_mcp_server_write_file_1"
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
    assert act.tool_call_id == "call_mcp_server_write_file_1"


def test_registry_build_gemini():
    # Build using reach='native' from catalog entry
    driver = registry.build("gemini-3.5-flash", reach="native")
    assert isinstance(driver, GeminiDriver)
    assert driver.name == "gemini-3.5-flash"
    assert driver.model == "gemini-3.5-flash"
    assert driver.base_url == "https://generativelanguage.googleapis.com/v1beta"
    assert driver.api_key_env == "GEMINI_API_KEY"
