from __future__ import annotations

from pmharness.drivers.openai_compat import OpenAICompatDriver


def _driver(**kwargs) -> OpenAICompatDriver:
    return OpenAICompatDriver(
        name="local:endpoint/model",
        model="model",
        base_url="https://api.example.com/v1",
        api_key_env="EXAMPLE_API_KEY",
        **kwargs,
    )


def _chat_body(driver: OpenAICompatDriver) -> dict:
    return driver._build_chat_body([{"role": "user", "content": "hi"}], stream=True)


def test_unset_temperature_defers_to_the_server_default():
    # Greedy decoding (temperature 0) drives open reasoning models into
    # repetition loops; an unconfigured pilot must not force it.
    assert "temperature" not in _chat_body(_driver())


def test_explicit_temperature_is_sent():
    assert _chat_body(_driver(temperature=0.6))["temperature"] == 0.6


def test_explicit_zero_temperature_is_still_sent():
    assert _chat_body(_driver(temperature=0.0))["temperature"] == 0.0
