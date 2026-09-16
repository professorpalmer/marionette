from __future__ import annotations

import json
import os
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from harness import advisor_consult
from harness.advisor_consult import (
    AdviceConflict,
    AdviceStore,
    AdvisorService,
    resolve_advisor,
    usage_receipt,
)


def test_advice_store_is_session_owned_and_idempotent(tmp_path):
    a = AdviceStore(str(tmp_path), "session-A")
    b = AdviceStore(str(tmp_path), "session-B")
    request_id = str(uuid4())
    first = {
        "request_id": request_id,
        "question": "Is this right?",
        "status": "pending",
        "session_id": "session-A",
    }
    stored, created = a.create(first)
    assert created is True
    again, created = a.create(first)
    assert created is False
    assert again["question"] == "Is this right?"
    with pytest.raises(AdviceConflict):
        a.create({**first, "question": "different"})
    b.create({**first, "session_id": "session-B"})
    assert [row["session_id"] for row in a.history()] == ["session-A"]
    assert [row["session_id"] for row in b.history()] == ["session-B"]


def test_usage_unknown_is_not_zero():
    response = SimpleNamespace(tokens_in=0, tokens_out=0, model="x", meta={})
    receipt = usage_receipt(response, "openai/gpt-5-nano")
    assert receipt["cost_usd"] is None
    assert receipt["cost_source"] == "unknown"
    assert receipt["usage_source"] == "unknown"


def test_advisor_service_one_tool_free_call_and_cancel(tmp_path, monkeypatch):
    calls = []

    class FakeDriver:
        timeout = 45

        def chat(self, messages, **kwargs):
            calls.append(kwargs)
            observer = getattr(self, "_request_body_observer", None)
            if observer is not None:
                observer(json.dumps({"tools": [], "tool_choice": None}).encode())
            assert kwargs.get("tools") == []
            return SimpleNamespace(
                text="Look at the failing test first.",
                error="",
                tokens_in=11,
                tokens_out=7,
                model="cheap",
                meta={"raw_usage": {"input": 11}, "provider_cost_usd": 0.002},
            )

    service = AdvisorService(str(tmp_path), resolver=lambda spec: ("cheap/mini", FakeDriver()))
    request_id = str(uuid4())
    receipt = service.start("session-A", request_id, "Did I miss a test?", [{"role": "user", "content": "hi"}], "openai/gpt-5.6")
    assert receipt["status"] in {"pending", "running", "succeeded"}
    for _ in range(50):
        history = service.history("session-A")
        if history and history[0]["status"] == "succeeded":
            break
        time.sleep(0.05)
    done = service.history("session-A")[0]
    assert done["status"] == "succeeded"
    assert done["tools_enabled"] is False
    assert done["wire_calls"] == 1
    assert done["usage"]["cost_usd"] == 0.002
    assert calls[0]["tools"] == []
    assert done["answer"]

    gate = threading.Event()

    class BlockingDriver:
        timeout = 45

        def chat(self, messages, **kwargs):
            observer = getattr(self, "_request_body_observer", None)
            if observer is not None:
                observer(json.dumps({"tools": [], "tool_choice": None}).encode())
            gate.wait(2)
            return SimpleNamespace(
                text="late",
                error="",
                tokens_in=1,
                tokens_out=1,
                model="cheap",
                meta={"raw_usage": {"input": 1}},
            )

    blocker = AdvisorService(str(tmp_path), resolver=lambda spec: ("cheap/mini", BlockingDriver()))
    other = str(uuid4())
    blocker.start("session-A", other, "stop me", [], "openai/gpt-5.6")
    cancelled = blocker.cancel("session-A", other)
    gate.set()
    assert cancelled["status"] in {"cancelling", "cancelled"}
    for _ in range(50):
        row = blocker.history("session-A")[0]
        if row["request_id"] == other and row["status"] in {"cancelled", "cancelling"}:
            break
        time.sleep(0.05)
    else:
        raise AssertionError(blocker.history("session-A")[0]["status"])


def test_history_liveness_does_not_os_kill_on_windows(tmp_path, monkeypatch):
    killed = []

    def fake_kill(pid, sig):
        killed.append((pid, sig))
        raise AssertionError("Windows liveness must not call os.kill")

    class _NtOs:
        name = "nt"

        def kill(self, pid, sig):
            return fake_kill(pid, sig)

        def __getattr__(self, name):
            return getattr(os, name)

    monkeypatch.setattr(advisor_consult, "os", _NtOs())
    monkeypatch.setattr(advisor_consult, "_windows_owner_alive", lambda pid: True)
    request_id = str(uuid4())
    AdviceStore(str(tmp_path), "session-A").create({
        "request_id": request_id,
        "question": "still running?",
        "status": "running",
        "session_id": "session-A",
        "owner_pid": os.getpid(),
    })
    rows = AdvisorService(str(tmp_path)).history("session-A")
    assert killed == []
    assert rows[0]["status"] == "running"


def test_history_marks_dead_owner_interrupted_without_signal(tmp_path, monkeypatch):
    monkeypatch.setattr(advisor_consult, "_owner_pid_alive", lambda pid: False)
    request_id = str(uuid4())
    AdviceStore(str(tmp_path), "session-A").create({
        "request_id": request_id,
        "question": "gone?",
        "status": "running",
        "session_id": "session-A",
        "owner_pid": 1,
    })
    rows = AdvisorService(str(tmp_path)).history("session-A")
    assert rows[0]["status"] == "interrupted"


def test_resolve_advisor_skips_current_pilot_and_needs_an_enabled_cheap_model(monkeypatch):
    monkeypatch.setattr(
        "harness.providers.available_pilots",
        lambda: ["openai/gpt-5.6", "openai/gpt-5-nano"],
    )

    class Provider:
        api_mode = "chat_completions"

    monkeypatch.setattr("harness.providers.get_provider", lambda name: Provider())
    monkeypatch.setattr(
        "pmharness.registry.price_with_source",
        lambda spec: (5.0, 15.0, "list") if "5.6" in spec else (0.05, 0.4, "list"),
    )
    built = []
    monkeypatch.setattr("harness.providers.build_pilot", lambda spec, max_tokens=2048: built.append(spec) or SimpleNamespace(timeout=90))
    spec, driver = resolve_advisor("openai/gpt-5.6")
    assert spec == "openai/gpt-5-nano"
    assert driver.timeout == 45
    assert built == ["openai/gpt-5-nano"]
