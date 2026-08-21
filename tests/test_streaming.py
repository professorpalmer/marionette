"""Unit and integration tests for OpenAI streaming feature."""
import tempfile
import json
import pytest

from pmharness.drivers.openai_compat import OpenAICompatDriver, DriverResponse
from harness.config import HarnessConfig
from harness.conversation import ConversationalSession


class FakeStreamingDriver:
    supports_streaming = True

    name = "fake-streaming"

    def __init__(self, use_native_tool_calls=True):
        self.use_native_tool_calls = use_native_tool_calls
        self.chat_called = False
        self.chat_stream_called = False
        self.calls = 0

    def chat(self, messages, *, tools=None, system=None):
        self.chat_called = True
        self.calls += 1
        if self.calls == 1:
            if self.use_native_tool_calls:
                meta = {
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": '{"path": "test.txt"}'
                            }
                        }
                    ],
                    "reasoning": "Let me read the file first."
                }
                return DriverResponse(text="Reading...", meta=meta)
            else:
                text = '{"say":"Reading...","actions":[{"kind":"read_file","path":"test.txt"}]}'
                return DriverResponse(text=text)
        else:
            return DriverResponse(text="Done.", meta={"tool_calls": [], "reasoning": ""})

    def chat_stream(self, messages, *, tools=None, system=None, on_delta,
                    on_reasoning_delta=None, on_tool_hint=None):
        self.chat_stream_called = True
        self.calls += 1

        # Fire off some deltas
        on_delta("Read")
        on_delta("ing")
        on_delta("...")

        if self.calls == 1:
            if self.use_native_tool_calls:
                meta = {
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": '{"path": "test.txt"}'
                            }
                        }
                    ],
                    "reasoning": "Let me read the file first."
                }
                return DriverResponse(text="Reading...", meta=meta)
            else:
                text = '{"say":"Reading...","actions":[{"kind":"read_file","path":"test.txt"}]}'
                return DriverResponse(text=text)
        else:
            return DriverResponse(text="Done.", meta={"tool_calls": [], "reasoning": ""})


def test_driver_chat_stream_assembly():
    driver = OpenAICompatDriver(
        name="test-driver",
        model="gpt-4o",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
    )

    # Mock self._key to avoid env var requirement
    driver._key = lambda: "fake-key"

    import urllib.request

    # Fake SSE chunk lines
    sse_lines = [
        b"data: " + json.dumps({
            "choices": [{
                "delta": {
                    "reasoning_content": "Thinking...",
                    "content": "Hello",
                }
            }]
        }).encode("utf-8") + b"\n",
        b"data: " + json.dumps({
            "choices": [{
                "delta": {
                    "content": " world",
                    "tool_calls": [{
                        "index": 0,
                        "id": "call_99",
                        "type": "function",
                        "function": {
                            "name": "read_",
                            "arguments": '{"pa'
                        }
                    }]
                }
            }]
        }).encode("utf-8") + b"\n",
        b"data: " + json.dumps({
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "function": {
                            "name": "file",
                            "arguments": 'th": "foo"}'
                        }
                    }]
                }
            }]
        }).encode("utf-8") + b"\n",
        b"data: " + json.dumps({
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 20,
            }
        }).encode("utf-8") + b"\n",
        b"data: [DONE]\n"
    ]

    class FakeResponse:
        def __init__(self, lines):
            self.lines = lines
            self.idx = 0
        def __iter__(self):
            return self
        def __next__(self):
            if self.idx < len(self.lines):
                val = self.lines[self.idx]
                self.idx += 1
                return val
            raise StopIteration
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc_val, exc_tb):
            pass

    fake_resp = FakeResponse(sse_lines)

    original_urlopen = urllib.request.urlopen
    try:
        urllib.request.urlopen = lambda req, timeout=None: fake_resp

        deltas = []
        reasoning = []
        tools = []
        def on_delta(d):
            deltas.append(d)
        def on_reasoning(d):
            reasoning.append(d)
        def on_tool(name):
            tools.append(name)

        resp = driver.chat_stream(
            messages=[{"role": "user", "content": "hi"}],
            on_delta=on_delta,
            on_reasoning_delta=on_reasoning,
            on_tool_hint=on_tool,
        )

        assert deltas == ["Hello", " world"]
        assert reasoning == ["Thinking..."]
        assert tools == ["read_", "read_file"]
        assert resp.text == "Hello world"
        assert resp.tokens_in == 10
        assert resp.tokens_out == 20
        assert resp.meta["reasoning"] == "Thinking..."

        # Verify tool calls assembly
        tool_calls = resp.meta["tool_calls"]
        assert len(tool_calls) == 1
        assert tool_calls[0]["id"] == "call_99"
        assert tool_calls[0]["function"]["name"] == "read_file"
        assert tool_calls[0]["function"]["arguments"] == '{"path": "foo"}'

    finally:
        urllib.request.urlopen = original_urlopen


def _sse_stream_driver(monkeypatch, sse_payloads):
    driver = OpenAICompatDriver(
        name="test-driver",
        model="gpt-4o",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
    )
    driver._key = lambda: "fake-key"

    lines = [
        b"data: " + json.dumps(payload).encode("utf-8") + b"\n"
        for payload in sse_payloads
    ]
    lines.append(b"data: [DONE]\n")

    class FakeResponse:
        def __init__(self, chunks):
            self.chunks = chunks

        def __iter__(self):
            return iter(self.chunks)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            return False

    import urllib.request
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda req, timeout=None: FakeResponse(lines),
    )
    return driver


def test_indexless_tool_call_assembles_across_chunks(monkeypatch):
    driver = _sse_stream_driver(monkeypatch, [
        {"choices": [{"delta": {"tool_calls": [
            {"id": "call_a", "type": "function",
             "function": {"name": "run_"}},
        ]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"function": {"name": "command"}},
        ]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"function": {"arguments": '{"x":'}},
        ]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"id": "call_a", "function": {"arguments": "1}"}},
        ]}}]},
    ])
    resp = driver.chat_stream(
        [{"role": "user", "content": "hi"}],
        on_delta=lambda _t: None,
    )
    tool_calls = resp.meta["tool_calls"]
    assert len(tool_calls) == 1
    assert tool_calls[0]["id"] == "call_a"
    assert tool_calls[0]["function"]["name"] == "run_command"
    assert tool_calls[0]["function"]["arguments"] == '{"x":1}'


def test_two_indexless_tool_calls_in_one_chunk_stay_distinct(monkeypatch):
    driver = _sse_stream_driver(monkeypatch, [
        {"choices": [{"delta": {"tool_calls": [
            {"id": "c1", "function": {"name": "alpha"}},
            {"id": "c2", "function": {"name": "beta"}},
        ]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"function": {"arguments": '{"b":2}'}},
        ]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"id": "c1", "function": {"arguments": '{"a":1}'}},
        ]}}]},
    ])
    resp = driver.chat_stream(
        [{"role": "user", "content": "hi"}],
        on_delta=lambda _t: None,
    )
    tool_calls = resp.meta["tool_calls"]
    assert [tc["id"] for tc in tool_calls] == ["c1", "c2"]
    assert tool_calls[0]["function"]["name"] == "alpha"
    assert tool_calls[0]["function"]["arguments"] == '{"a":1}'
    assert tool_calls[1]["function"]["name"] == "beta"
    assert tool_calls[1]["function"]["arguments"] == '{"b":2}'


def test_two_indexless_nameless_calls_attach_later_fragment_to_most_recent(monkeypatch):
    driver = _sse_stream_driver(monkeypatch, [
        {"choices": [{"delta": {"tool_calls": [
            {"function": {"name": "alpha"}},
            {"function": {"name": "beta"}},
        ]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"function": {"arguments": '{"n":1}'}},
        ]}}]},
    ])
    resp = driver.chat_stream(
        [{"role": "user", "content": "hi"}],
        on_delta=lambda _t: None,
    )
    tool_calls = resp.meta["tool_calls"]
    assert len(tool_calls) == 2
    assert tool_calls[0]["function"]["name"] == "alpha"
    assert tool_calls[0]["function"]["arguments"] == ""
    assert tool_calls[1]["function"]["name"] == "beta"
    assert tool_calls[1]["function"]["arguments"] == '{"n":1}'


def test_conversational_loop_streaming():
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    s = ConversationalSession(cfg)

    # Mock actual pilot object with our FakeStreamingDriver
    s.pilot = FakeStreamingDriver(use_native_tool_calls=True)

    events = list(s.send("Hello there"))

    kinds = [e.kind for e in events]
    # Check that message_delta events are yielded in order with the right pieces
    delta_events = [e for e in events if e.kind == "message_delta"]
    assert len(delta_events) == 6
    assert delta_events[0].data["text"] == "Read"
    assert delta_events[1].data["text"] == "ing"
    assert delta_events[2].data["text"] == "..."
    assert delta_events[3].data["text"] == "Read"
    assert delta_events[4].data["text"] == "ing"
    assert delta_events[5].data["text"] == "..."

    # final 'message' event still arrives with the cleaned text
    msg_events = [e for e in events if e.kind == "message"]
    assert len(msg_events) == 2
    assert msg_events[0].data["text"] == "Reading..."
    assert msg_events[1].data["text"] == "Done."

    # tool_calls assembled from the stream still execute
    assert "action_start" in kinds
    assert "action_result" in kinds

    # Assert chat_stream was called and NOT chat
    assert s.pilot.chat_stream_called
    assert not s.pilot.chat_called


def test_conversational_loop_worker_no_streaming():
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp(), no_delegation=True)
    s = ConversationalSession(cfg)

    s.pilot = FakeStreamingDriver(use_native_tool_calls=True)

    events = list(s.send("Hello there"))

    kinds = [e.kind for e in events]
    assert "message_delta" not in kinds
    assert "message" in kinds

    assert s.pilot.chat_called
    assert not s.pilot.chat_stream_called


class _MissingIdStreamingPilot:
    """Streaming native calls with empty/missing ids — two steps."""

    supports_streaming = True
    name = "missing-id-stream"

    def __init__(self):
        self.calls = 0
        self.provider_payloads = []

    def chat(self, messages, *, tools=None, system=None):
        raise AssertionError("chat() must not be used when streaming is available")

    def chat_stream(self, messages, *, tools=None, system=None, on_delta,
                    on_reasoning_delta=None, on_tool_hint=None):
        self.calls += 1
        on_delta("step")
        if self.calls == 1:
            payload = [
                {
                    "id": "",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path": "a.txt"}',
                    },
                },
                {
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path": "b.txt"}',
                    },
                },
            ]
            self.provider_payloads.append(payload)
            return DriverResponse(
                text="",
                tokens_out=4,
                meta={"tool_calls": payload, "reasoning": "read both"},
            )
        if self.calls == 2:
            payload = [
                {
                    "id": "",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path": "c.txt"}',
                    },
                },
            ]
            self.provider_payloads.append(payload)
            return DriverResponse(
                text="",
                tokens_out=3,
                meta={"tool_calls": payload, "reasoning": "read third"},
            )
        return DriverResponse(
            text="Done.",
            tokens_out=2,
            meta={"tool_calls": [], "reasoning": ""},
        )


def test_streaming_missing_tool_call_ids_persist_unique_and_paired(tmp_path):
    cfg = HarnessConfig(
        driver="stub-oracle-v2",
        state_dir=str(tmp_path / "state"),
        repo=str(tmp_path / "repo"),
    )
    (tmp_path / "repo").mkdir()
    (tmp_path / "state").mkdir()
    (tmp_path / "repo" / "a.txt").write_text("A", encoding="utf-8")
    (tmp_path / "repo" / "b.txt").write_text("B", encoding="utf-8")
    (tmp_path / "repo" / "c.txt").write_text("C", encoding="utf-8")
    session = ConversationalSession(cfg)
    session.pilot = _MissingIdStreamingPilot()

    list(session.send("read the notes"))

    originals = session.pilot.provider_payloads
    assert originals[0][0]["id"] == ""
    assert "id" not in originals[0][1]
    assert originals[1][0]["id"] == ""

    assistants = [
        m for m in session._history
        if m.get("role") == "assistant" and m.get("tool_calls")
    ]
    assert len(assistants) == 2
    ids1 = [tc["id"] for tc in assistants[0]["tool_calls"]]
    ids2 = [tc["id"] for tc in assistants[1]["tool_calls"]]
    all_ids = ids1 + ids2
    assert len(all_ids) == 3
    assert len(set(all_ids)) == 3
    assert all(i and i.strip() for i in all_ids)
    tools = [m for m in session._history if m.get("role") == "tool"]
    assert [m.get("tool_call_id") for m in tools] == all_ids


class _LiveReasoningPilot:
    """Streams reasoning + tool hint before prose -- the opaque-spinner case."""

    supports_streaming = True
    name = "live-reasoning"

    def __init__(self):
        self.calls = 0

    def chat(self, messages, *, tools=None, system=None):
        raise AssertionError("chat() must not be used when streaming is available")

    def chat_stream(self, messages, *, tools=None, system=None, on_delta,
                    on_reasoning_delta=None, on_tool_hint=None):
        self.calls += 1
        if on_reasoning_delta:
            on_reasoning_delta("Let me pull the latest from GitHub")
            on_reasoning_delta(" and check the wiki.")
        if on_tool_hint:
            on_tool_hint("read_file")
        on_delta("Here is the summary.")
        return DriverResponse(
            text="Here is the summary.",
            tokens_out=12,
            meta={"tool_calls": [], "reasoning": "Let me pull the latest from GitHub and check the wiki."},
        )


def test_live_reasoning_and_tool_prep_events():
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    s = ConversationalSession(cfg)
    s.pilot = _LiveReasoningPilot()

    events = list(s.send("Summarize Kotoba"))
    thinking = [e for e in events if e.kind == "thinking"]
    assert len(thinking) == 2
    assert thinking[0].data.get("delta") is True
    assert "GitHub" in thinking[0].data["text"]
    assert thinking[1].data.get("delta") is True

    tool_prep = [e for e in events if e.kind == "tool_prep"]
    assert len(tool_prep) == 1
    assert tool_prep[0].data["name"] == "read_file"

    deltas = [e for e in events if e.kind == "message_delta"]
    assert deltas and deltas[0].data["text"] == "Here is the summary."
