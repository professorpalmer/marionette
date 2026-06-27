import pytest
from pmharness.drivers.base import DriverResponse
from pmharness.drivers.moa import MoADriver
from pmharness import registry as reg


class FakeDriver:
    def __init__(self, name, preset_response=None):
        self.name = name
        self.preset_response = preset_response or DriverResponse(
            text=f"Response from {name}",
            tokens_in=10,
            tokens_out=20,
            model=name,
        )
        self.complete_calls = []
        self.chat_calls = []

    def complete(self, task_prompt, *, system=None):
        self.complete_calls.append((task_prompt, system))
        return self.preset_response

    def chat(self, messages, *, tools=None, system=None):
        self.chat_calls.append((messages, tools, system))
        return self.preset_response


def test_moa_complete_success():
    # Setup fakes
    fakes = {}

    def fake_builder(name, reach="openrouter"):
        fakes[name] = FakeDriver(name)
        return fakes[name]

    moa = MoADriver(
        name="test-moa",
        proposers=["p1", "p2", "p3"],
        aggregator="agg",
        builder=fake_builder,
    )

    # Execute
    res = moa.complete("Hello world", system="My custom system")

    # Verify each proposer was called
    for name in ["p1", "p2", "p3"]:
        assert name in fakes
        assert len(fakes[name].complete_calls) == 1
        assert fakes[name].complete_calls[0][0] == "Hello world"
        assert fakes[name].complete_calls[0][1] == "My custom system"

    # Verify aggregator received candidates in task prompt
    assert "agg" in fakes
    agg_calls = fakes["agg"].complete_calls
    assert len(agg_calls) == 1
    agg_prompt = agg_calls[0][0]
    assert "Hello world" in agg_prompt
    assert "Response from p1" in agg_prompt
    assert "Response from p2" in agg_prompt
    assert "Response from p3" in agg_prompt

    # Verify result
    assert res.text == "Response from agg"
    assert res.model == "test-moa"
    assert res.tokens_in == 40
    assert res.tokens_out == 80  # 20 * 3 + 20 = 80
    assert res.meta["moa"]["n_proposers_ok"] == 3
    assert res.meta["moa"]["proposer_tokens_in"] == 30
    assert res.meta["moa"]["proposer_tokens_out"] == 60


def test_moa_complete_partial_errors():
    # One proposer fails, one returns normal error, one succeeds
    fakes = {}

    def fake_builder(name, reach="openrouter"):
        if name == "p1":
            resp = DriverResponse(text="", error="Rate limited", model=name)
        elif name == "p2":
            resp = DriverResponse(text="Good candidate", tokens_in=5, tokens_out=8, model=name)
        else:
            resp = DriverResponse(text="Synthesizer output", tokens_in=12, tokens_out=15, model=name)
        fakes[name] = FakeDriver(name, preset_response=resp)
        return fakes[name]

    moa = MoADriver(
        name="test-moa",
        proposers=["p1", "p2"],
        aggregator="agg",
        builder=fake_builder,
    )

    res = moa.complete("Hello")

    # p1 errored, p2 was ok, agg should synthesize using only p2
    assert "p1" in fakes
    assert "p2" in fakes
    assert "agg" in fakes

    agg_prompt = fakes["agg"].complete_calls[0][0]
    assert "Good candidate" in agg_prompt
    assert "p1" not in agg_prompt or "Rate limited" not in agg_prompt  # Or at least not as a proposal candidate

    assert res.text == "Synthesizer output"
    assert res.meta["moa"]["n_proposers_ok"] == 1
    assert res.meta["moa"]["proposer_tokens_in"] == 5
    assert res.meta["moa"]["proposer_tokens_out"] == 8
    assert res.tokens_in == 17  # 5 + 12
    assert res.tokens_out == 23  # 8 + 15


def test_moa_complete_all_errors():
    fakes = {}

    def fake_builder(name, reach="openrouter"):
        resp = DriverResponse(text="", error="Fatal", model=name)
        fakes[name] = FakeDriver(name, preset_response=resp)
        return fakes[name]

    moa = MoADriver(
        name="test-moa",
        proposers=["p1", "p2"],
        aggregator="agg",
        builder=fake_builder,
    )

    res = moa.complete("Hello")
    assert res.error is not None
    assert "all MoA proposers failed" in res.error
    assert res.meta["moa"]["n_proposers_ok"] == 0


def test_moa_chat_guard_with_tools():
    moa = MoADriver(
        name="test-moa",
        proposers=["p1"],
        aggregator="agg",
        builder=lambda name, reach: FakeDriver(name),
    )

    # Non-empty tools list should fail immediately
    res = moa.chat([{"role": "user", "content": "hello"}], tools=[{"name": "write_file"}])
    assert res.error is not None
    assert "planner/review virtual-model and cannot be used" in res.error
    assert res.text == ""


def test_moa_chat_success_no_tools():
    fakes = {}

    def fake_builder(name, reach="openrouter"):
        fakes[name] = FakeDriver(name)
        return fakes[name]

    moa = MoADriver(
        name="test-moa",
        proposers=["p1", "p2"],
        aggregator="agg",
        builder=fake_builder,
    )

    messages = [{"role": "user", "content": "Design a system."}]
    res = moa.chat(messages, tools=None, system="System level instructions")

    # Verify proposers were called with chat
    for name in ["p1", "p2"]:
        assert name in fakes
        assert len(fakes[name].chat_calls) == 1
        assert fakes[name].chat_calls[0][0] == messages
        assert fakes[name].chat_calls[0][1] is None
        assert fakes[name].chat_calls[0][2] == "System level instructions"

    # Aggregator complete call should contain flat history
    assert "agg" in fakes
    assert len(fakes["agg"].complete_calls) == 1
    agg_prompt = fakes["agg"].complete_calls[0][0]
    assert "USER: Design a system." in agg_prompt
    assert "Response from p1" in agg_prompt
    assert "Response from p2" in agg_prompt

    assert res.text == "Response from agg"
    assert res.meta["tool_calls"] == []
    assert res.meta["moa"]["n_proposers_ok"] == 2


def test_registry_build_moa_preset(monkeypatch):
    # Verify that registry builds moa-planner and it maps to our preset models which exist in the catalog
    cat = reg.load_catalog()
    moa_presets = cat.get("moa_presets", {})
    assert "moa-planner" in moa_presets
    preset = moa_presets["moa-planner"]

    # Verify that preset model names actually exist in the catalog models list
    existing_model_names = {m["name"] for m in cat["models"]}
    for p in preset["proposers"]:
        assert p in existing_model_names, f"Preset proposer {p} must exist in the catalog"
    assert preset["aggregator"] in existing_model_names, f"Preset aggregator {preset['aggregator']} must exist in the catalog"

    # Verify build('moa-planner') construction
    # We monkeypatch the builder inside registry to avoid actually calling the real API / endpoints during build if we don't want to
    # Actually, registry build() only instantiates the class (e.g. it might instantiate OpenAICompatDriver which doesn't hit network on init)
    moa_driver = reg.build("moa-planner")
    assert isinstance(moa_driver, MoADriver)
    assert moa_driver.name == "moa-planner"
    assert moa_driver.proposer_names == preset["proposers"]
    assert moa_driver.aggregator_name == preset["aggregator"]
