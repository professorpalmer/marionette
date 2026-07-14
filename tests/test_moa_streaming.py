"""Hermetic tests for MoADriver.chat_stream (aggregator delegation)."""
from pmharness.drivers.base import DriverResponse
from pmharness.drivers.moa import MoADriver, AGGREGATOR_SYSTEM_PROMPT


class FakeProposer:
    def __init__(self, name, preset_response=None):
        self.name = name
        self.preset_response = preset_response or DriverResponse(
            text=f"Response from {name}",
            tokens_in=10,
            tokens_out=20,
            model=name,
        )
        self.chat_calls = []

    def complete(self, task_prompt, *, system=None):
        raise AssertionError("proposers should be invoked via chat() in chat_stream")

    def chat(self, messages, *, tools=None, system=None):
        self.chat_calls.append((messages, tools, system))
        return self.preset_response


class FakeStreamingAggregator:
    supports_streaming = True

    def __init__(self, name, preset_response=None):
        self.name = name
        self.preset_response = preset_response or DriverResponse(
            text="Synthesized answer",
            tokens_in=12,
            tokens_out=18,
            model=name,
            meta={"reasoning": "weighing proposals"},
        )
        self.complete_calls = []
        self.chat_stream_calls = []

    def complete(self, task_prompt, *, system=None):
        self.complete_calls.append((task_prompt, system))
        return self.preset_response

    def chat_stream(
        self,
        messages,
        *,
        tools=None,
        system=None,
        on_delta,
        on_reasoning_delta=None,
        on_tool_hint=None,
    ):
        self.chat_stream_calls.append((messages, tools, system))
        on_delta("Synth")
        on_delta("esized")
        if on_reasoning_delta:
            on_reasoning_delta("thinking")
        if on_tool_hint:
            on_tool_hint("read_file")
        return self.preset_response


class FakeNonStreamingAggregator:
    supports_streaming = False

    def __init__(self, name, preset_response=None):
        self.name = name
        self.preset_response = preset_response or DriverResponse(
            text="Complete-only synthesis",
            tokens_in=8,
            tokens_out=14,
            model=name,
        )
        self.complete_calls = []

    def complete(self, task_prompt, *, system=None):
        self.complete_calls.append((task_prompt, system))
        return self.preset_response


def _builder_with_streaming_agg(fakes):
    def fake_builder(name, reach="openrouter"):
        if name == "agg":
            fakes[name] = FakeStreamingAggregator(name)
        else:
            fakes[name] = FakeProposer(name)
        return fakes[name]

    return fake_builder


def test_supports_streaming_true_when_aggregator_streams():
    fakes = {}
    moa = MoADriver(
        name="test-moa",
        proposers=["p1"],
        aggregator="agg",
        builder=_builder_with_streaming_agg(fakes),
    )
    assert moa.supports_streaming is True
    assert callable(moa.chat_stream)


def test_supports_streaming_false_when_aggregator_lacks_stream():
    fakes = {}

    def fake_builder(name, reach="openrouter"):
        if name == "agg":
            fakes[name] = FakeNonStreamingAggregator(name)
        else:
            fakes[name] = FakeProposer(name)
        return fakes[name]

    moa = MoADriver(
        name="test-moa",
        proposers=["p1"],
        aggregator="agg",
        builder=fake_builder,
    )
    assert moa.supports_streaming is False
    assert callable(moa.chat_stream)


def test_chat_stream_delegates_to_aggregator_chat_stream():
    fakes = {}
    moa = MoADriver(
        name="test-moa",
        proposers=["p1", "p2"],
        aggregator="agg",
        builder=_builder_with_streaming_agg(fakes),
    )

    messages = [{"role": "user", "content": "Plan a feature."}]
    deltas = []
    reasoning = []
    tool_hints = []

    resp = moa.chat_stream(
        messages,
        system="Custom system",
        on_delta=lambda t: deltas.append(t),
        on_reasoning_delta=lambda t: reasoning.append(t),
        on_tool_hint=lambda n: tool_hints.append(n),
    )

    for name in ["p1", "p2"]:
        assert len(fakes[name].chat_calls) == 1
        assert fakes[name].chat_calls[0][0] == messages
        assert fakes[name].chat_calls[0][2] == "Custom system"

    assert len(fakes["agg"].chat_stream_calls) == 1
    agg_messages, agg_tools, agg_system = fakes["agg"].chat_stream_calls[0]
    assert agg_tools is None
    assert agg_system == AGGREGATOR_SYSTEM_PROMPT
    assert len(agg_messages) == 1
    assert agg_messages[0]["role"] == "user"
    assert "USER: Plan a feature." in agg_messages[0]["content"]
    assert "Response from p1" in agg_messages[0]["content"]
    assert "Response from p2" in agg_messages[0]["content"]
    assert not fakes["agg"].complete_calls

    assert deltas == ["Synth", "esized"]
    assert reasoning == ["thinking"]
    assert tool_hints == ["read_file"]
    assert resp.text == "Synthesized answer"
    assert resp.model == "test-moa"
    assert resp.tokens_in == 32  # 10+10 proposer + 12 agg
    assert resp.tokens_out == 58  # 20+20 proposer + 18 agg
    assert resp.meta["moa"]["n_proposers_ok"] == 2
    assert resp.meta["tool_calls"] == []


def test_chat_stream_tools_guard():
    moa = MoADriver(
        name="test-moa",
        proposers=["p1"],
        aggregator="agg",
        builder=_builder_with_streaming_agg({}),
    )

    resp = moa.chat_stream(
        [{"role": "user", "content": "hello"}],
        tools=[{"name": "write_file"}],
        on_delta=lambda t: None,
    )
    assert resp.error is not None
    assert "planner/review virtual-model and cannot be used" in resp.error
    assert resp.text == ""


def test_chat_stream_all_proposers_fail():
    fakes = {}

    def fake_builder(name, reach="openrouter"):
        if name == "agg":
            fakes[name] = FakeStreamingAggregator(name)
        else:
            fakes[name] = FakeProposer(
                name,
                preset_response=DriverResponse(text="", error="down", model=name),
            )
        return fakes[name]

    moa = MoADriver(
        name="test-moa",
        proposers=["p1", "p2"],
        aggregator="agg",
        builder=fake_builder,
    )

    deltas = []
    resp = moa.chat_stream(
        [{"role": "user", "content": "hi"}],
        on_delta=lambda t: deltas.append(t),
    )

    assert resp.error is not None
    assert "all MoA proposers failed" in resp.error
    assert resp.meta["moa"]["n_proposers_ok"] == 0
    assert not fakes["agg"].chat_stream_calls
    assert deltas == []


def test_chat_stream_fallback_emits_single_delta_when_no_stream_path():
    fakes = {}

    def fake_builder(name, reach="openrouter"):
        if name == "agg":
            fakes[name] = FakeNonStreamingAggregator(name)
        else:
            fakes[name] = FakeProposer(name)
        return fakes[name]

    moa = MoADriver(
        name="test-moa",
        proposers=["p1"],
        aggregator="agg",
        builder=fake_builder,
    )

    deltas = []
    resp = moa.chat_stream(
        [{"role": "user", "content": "hello"}],
        on_delta=lambda t: deltas.append(t),
    )

    assert len(fakes["agg"].complete_calls) == 1
    assert deltas == ["Complete-only synthesis"]
    assert resp.text == "Complete-only synthesis"
    assert resp.meta["moa"]["n_proposers_ok"] == 1
