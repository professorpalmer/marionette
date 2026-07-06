"""Tests for provider cassette record/replay (pmharness/drivers/cassette.py)."""
import json
import os
import tempfile

import pytest

from pmharness.drivers.base import DriverResponse
from pmharness.drivers.cassette import CassetteDriver, maybe_wrap_cassette, request_hash


class _CountingStub:
    name = "count-stub"
    model = "count-stub"

    def __init__(self):
        self.calls = 0

    def complete(self, task_prompt: str, *, system: str = "") -> DriverResponse:
        self.calls += 1
        text = f"echo:{task_prompt}"
        return DriverResponse(
            text=text,
            tokens_in=10,
            tokens_out=5,
            latency_ms=1.0,
            model=self.model,
        )

    def chat(self, messages, *, tools=None, system=None) -> DriverResponse:
        self.calls += 1
        text = json.dumps(messages)
        return DriverResponse(
            text=text,
            tokens_in=12,
            tokens_out=6,
            latency_ms=1.0,
            model=self.model,
        )


def test_record_then_replay_is_identical_and_inner_called_once(monkeypatch, tmp_path):
    cassette_dir = str(tmp_path)
    inner = _CountingStub()

    monkeypatch.setenv("HARNESS_CASSETTE_MODE", "record")
    monkeypatch.setenv("HARNESS_CASSETTE_DIR", cassette_dir)
    recorder = CassetteDriver(inner, mode="record", cassette_dir=cassette_dir)
    first = recorder.complete("hello")
    assert inner.calls == 1

    monkeypatch.setenv("HARNESS_CASSETTE_MODE", "replay")
    replayer = CassetteDriver(inner, mode="replay", cassette_dir=cassette_dir)
    second = replayer.complete("hello")
    third = replayer.complete("hello")
    assert inner.calls == 1
    assert second.text == first.text
    assert third.text == first.text
    assert second.tokens_in == first.tokens_in
    assert second.tokens_out == first.tokens_out
    assert second.latency_ms == 0.0


def test_replay_miss_raises_with_hash(monkeypatch, tmp_path):
    cassette_dir = str(tmp_path)
    inner = _CountingStub()
    monkeypatch.setenv("HARNESS_CASSETTE_MODE", "replay")
    monkeypatch.setenv("HARNESS_CASSETTE_DIR", cassette_dir)
    driver = CassetteDriver(inner, mode="replay", cassette_dir=cassette_dir)
    with pytest.raises(KeyError) as exc:
        driver.complete("missing")
    msg = str(exc.value)
    assert "cassette miss for hash" in msg
    assert "count-stub.json" in msg


def test_scrubbing_redacts_sk_pattern(monkeypatch, tmp_path):
    cassette_dir = str(tmp_path)

    class _LeakyStub(_CountingStub):
        def complete(self, task_prompt: str, *, system: str = "") -> DriverResponse:
            self.calls += 1
            return DriverResponse(
                text="secret sk-abcdefghijklmnopqrstuvwxyz here",
                tokens_in=1,
                tokens_out=1,
                model=self.model,
            )

    inner = _LeakyStub()
    monkeypatch.setenv("HARNESS_CASSETTE_MODE", "record")
    monkeypatch.setenv("HARNESS_CASSETTE_DIR", cassette_dir)
    driver = CassetteDriver(inner, mode="record", cassette_dir=cassette_dir)
    driver.complete("scrub-me")

    raw = (tmp_path / "count-stub.json").read_text(encoding="utf-8")
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in raw
    assert "[REDACTED]" in raw


def test_passthrough_calls_inner_and_writes_nothing(monkeypatch, tmp_path):
    inner = _CountingStub()
    monkeypatch.delenv("HARNESS_CASSETTE_MODE", raising=False)
    monkeypatch.setenv("HARNESS_CASSETTE_DIR", str(tmp_path))
    wrapped = maybe_wrap_cassette(inner)
    assert wrapped is inner
    wrapped.complete("plain")
    assert inner.calls == 1
    assert list(tmp_path.iterdir()) == []


def test_maybe_wrap_cassette_requires_dir(monkeypatch):
    inner = _CountingStub()
    monkeypatch.setenv("HARNESS_CASSETTE_MODE", "record")
    monkeypatch.delenv("HARNESS_CASSETTE_DIR", raising=False)
    with pytest.raises(RuntimeError, match="HARNESS_CASSETTE_DIR"):
        maybe_wrap_cassette(inner)
