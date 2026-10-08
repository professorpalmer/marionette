"""llama.cpp reasoning-budget report: captured per step, counted per session."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import urllib.request

import pytest

from harness.stream_performance_store import StreamPerformanceReceiptStore, copy_stream_performance
from pmharness.drivers.openai_compat import OpenAICompatDriver
from tests.test_openai_compat_stream_terminals import _OwnedReadSocket, _data

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _stream(monkeypatch, finish_chunk: dict, tail: dict | None = None):
    class _Sse:
        def __init__(self):
            self._sock = _OwnedReadSocket()
            body = b"\n" + _data(tail) if tail is not None else b""
            self._tail = io.BytesIO(body + b"\ndata: [DONE]\n\n")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def __iter__(self):
            yield _data({"choices": [{"delta": {"reasoning_content": "hmm"}}]})
            yield _data(finish_chunk)

        def settimeout(self, _seconds):
            return None

        def read1(self, size):
            return self._tail.read(size)

    monkeypatch.setenv("LLAMA_CPP_API_KEY", "local-test-key")
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _Sse())
    driver = OpenAICompatDriver(
        name="llama-cpp:bonsai", model="bonsai",
        base_url="http://127.0.0.1:8080/v1", api_key_env="LLAMA_CPP_API_KEY",
    )
    return driver.chat_stream([{"role": "user", "content": "go"}], on_delta=lambda _t: None)


def test_force_close_on_the_final_chunk_is_counted(monkeypatch):
    resp = _stream(monkeypatch, {
        "choices": [{"delta": {"content": "done"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20490,
                  "completion_tokens_details": {"reasoning_tokens": 20480}},
        "timings": {"reasoning_budget_exhausted": True, "reasoning_n": 20480},
    })
    assert resp.error is None
    assert resp.meta["stream_performance"] == {
        "reasoning_budget_reported_count": 1,
        "reasoning_budget_exhausted_count": 1,
        "reasoning_tokens": 20480,
    }


def test_report_in_the_trailing_usage_chunk_is_counted(monkeypatch):
    resp = _stream(
        monkeypatch,
        {"choices": [{"delta": {"content": "done"}, "finish_reason": "stop"}]},
        tail={"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 300},
              "timings": {"reasoning_budget_exhausted": False, "reasoning_n": 290}},
    )
    assert resp.tokens_out == 300
    assert resp.meta["stream_performance"] == {
        "reasoning_budget_reported_count": 1,
        "reasoning_budget_exhausted_count": 0,
        "reasoning_tokens": 290,
    }


def test_a_server_without_the_report_stays_unknown(monkeypatch):
    resp = _stream(monkeypatch, {
        "choices": [{"delta": {"content": "done"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        "timings": {"predicted_n": 5},
    })
    assert "stream_performance" not in resp.meta


@pytest.mark.parametrize("perf", [
    {"reasoning_budget_reported_count": 1, "reasoning_budget_exhausted_count": 1, "reasoning_tokens": 20480},
])
def test_receipts_keep_the_counts(perf):
    assert copy_stream_performance(perf) == perf


def test_report_script_divides_by_reported_steps(tmp_path):
    store = StreamPerformanceReceiptStore(str(tmp_path))
    base = {"session_id": "sess1", "driver": "llama-cpp:bonsai", "served_model": "bonsai"}
    for exhausted in (1, 0, None, 1):
        perf = {} if exhausted is None else {
            "reasoning_budget_reported_count": 1,
            "reasoning_budget_exhausted_count": exhausted,
            "reasoning_tokens": 100,
        }
        store.record("sess1", dict(base, stream_performance=perf))
    store.record("sess2", dict(base, session_id="sess2", stream_performance={}))
    out = subprocess.run(
        [sys.executable, os.path.join(ROOT, "scripts", "reasoning_budget_report.py"),
         str(tmp_path), "--json"],
        capture_output=True, text=True, check=True,
    )
    report = json.loads(out.stdout)
    assert report["reported_steps"] == 3
    assert report["exhausted_steps"] == 2
    [session] = report["sessions"]
    assert session["session_id"] == "sess1"
    assert session["steps"] == 4
    assert session["models"] == ["bonsai"]


def test_a_non_streamed_reply_is_counted_too(monkeypatch):
    payload = {
        "choices": [{"message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20490},
        "timings": {"reasoning_budget_exhausted": True, "reasoning_n": 20480},
    }

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setenv("LLAMA_CPP_API_KEY", "local-test-key")
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=None: _Resp(json.dumps(payload).encode("utf-8")))
    driver = OpenAICompatDriver(
        name="llama-cpp:bonsai", model="bonsai",
        base_url="http://127.0.0.1:8080/v1", api_key_env="LLAMA_CPP_API_KEY",
    )
    resp = driver.chat([{"role": "user", "content": "go"}])
    assert resp.error is None
    assert resp.meta["stream_performance"] == {
        "reasoning_budget_reported_count": 1,
        "reasoning_budget_exhausted_count": 1,
        "reasoning_tokens": 20480,
    }
