"""llama-server prompt-read progress keeps a local stream alive."""

from __future__ import annotations

import inspect
import json
import time
import urllib.request
from types import SimpleNamespace
from unittest.mock import MagicMock

from harness.send_loop_phases import (
    STREAM_IDLE_NOTICE_SEC,
    STREAM_IDLE_STUCK_SEC,
    drain_stream_queue,
)
from pmharness.drivers import openai_compat
from pmharness.drivers.openai_compat import (
    OpenAICompatDriver,
    _OpenAIChatSseAccumulator,
    prompt_progress_notice,
)


def _line(payload: dict) -> bytes:
    return ("data: " + json.dumps(payload) + "\n").encode("utf-8")


def _progress(done: int, total: int = 40960) -> dict:
    return {
        "choices": [],
        "prompt_progress": {"total": total, "cache": 0, "processed": done, "time_ms": 10},
    }


def _llama_driver() -> OpenAICompatDriver:
    return OpenAICompatDriver(
        name="llama-cpp:qwen",
        model="qwen",
        base_url="http://127.0.0.1:8080/v1",
        api_key_env="LLAMA_CPP_API_KEY",
    )


def test_notice_text_and_bad_payloads():
    assert prompt_progress_notice({"total": 40960, "processed": 10240}) == (
        "Reading prompt: 10240 of 40960 tokens (25%)"
    )
    assert prompt_progress_notice({"total": 100, "processed": 500}).endswith("(100%)")
    assert prompt_progress_notice({"total": 0, "processed": 0}) is None
    assert prompt_progress_notice({"total": "x"}) is None


def test_accumulator_reports_progress_without_starting_the_stream():
    seen = []
    acc = _OpenAIChatSseAccumulator(on_prompt_progress=seen.append)
    acc.feed(_line(_progress(2048)))
    assert seen == [{"total": 40960, "cache": 0, "processed": 2048, "time_ms": 10}]
    assert acc.stream_started is False
    assert acc.full_text == ""


def test_return_progress_only_on_llama_cpp_streams(monkeypatch):
    monkeypatch.setenv("LLAMA_CPP_API_KEY", "local")
    llama = _llama_driver()
    assert llama._build_chat_body([{"role": "user", "content": "hi"}], stream=True)["return_progress"] is True
    assert "return_progress" not in llama._build_chat_body([{"role": "user", "content": "hi"}])
    remote = OpenAICompatDriver(
        name="openrouter:x", model="x", base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
    )
    assert "return_progress" not in remote._build_chat_body([{"role": "user", "content": "hi"}], stream=True)


def test_drain_wires_the_wait_notice_into_chat_stream():
    assert "on_wait_notice" in inspect.signature(OpenAICompatDriver.chat_stream).parameters


def test_chat_stream_turns_progress_into_rate_limited_notices(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(openai_compat.time, "monotonic", lambda: clock["t"])

    class _Stream:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def __iter__(self):
            for done, step in ((0, 0.0), (4096, 0.5), (8192, 1.6), (16384, 1.0), (40960, 3.0)):
                clock["t"] += step
                yield _line(_progress(done))
            yield _line({"choices": [{"delta": {"content": "pong"}, "finish_reason": "stop"}]})
            yield b"data: [DONE]\n"

    sent = {}

    def fake_urlopen(req, timeout=None):
        sent["body"] = json.loads(req.data.decode("utf-8"))
        return _Stream()

    monkeypatch.setenv("LLAMA_CPP_API_KEY", "local")
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    notices = []
    resp = _llama_driver().chat_stream(
        [{"role": "user", "content": "ping"}],
        on_delta=lambda _t: None,
        on_wait_notice=notices.append,
    )
    assert sent["body"]["return_progress"] is True
    assert resp.error is None and resp.text == "pong"
    # t=1000 (0%), 1000.5 dropped, 1002.1 (20%), 1003.1 dropped, 1006.1 (100%).
    assert notices == [
        "Reading prompt: 0 of 40960 tokens (0%)",
        "Reading prompt: 8192 of 40960 tokens (20%)",
        "Reading prompt: 40960 of 40960 tokens (100%)",
    ]


def test_progress_notices_keep_the_drain_past_the_stuck_limit(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr("harness.send_loop_phases.STREAM_IDLE_POLL_SEC", 0)
    step = STREAM_IDLE_NOTICE_SEC - 1
    rounds = int(STREAM_IDLE_STUCK_SEC // step) + 3
    script = []
    for i in range(rounds):
        script.append(("advance", step))
        script.append(("wait", "Reading prompt: %d of 100 tokens (%d%%)" % (i, i)))
    resp = SimpleNamespace(text="ok")
    script.append(("done", resp))
    it = iter(script)

    def fake_get(timeout=None):
        while True:
            item = next(it)
            if item[0] == "advance":
                clock["t"] += item[1]
                continue
            return item

    q = MagicMock()
    q.get = fake_get
    events = list(drain_stream_queue(q))
    assert clock["t"] > STREAM_IDLE_STUCK_SEC
    waits = [e.data.get("message") for e in events if e.kind == "notice"]
    assert waits and all(m.startswith("Reading prompt") for m in waits)
    assert not any(e.data.get("stream_idle_stuck") for e in events if e.kind == "notice")
