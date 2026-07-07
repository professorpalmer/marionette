"""Tests for append-only context mode resolution."""
from __future__ import annotations

import pytest

from harness.append_only_context import (
    append_only_setting,
    should_enable_append_only,
)


@pytest.mark.parametrize(
    "driver_name",
    [
        "ollama",
        "Ollama-local",
        "my-lm-studio",
        "lmstudio-runner",
        "llama.cpp-server",
        "llamacpp",
        "vllm-worker",
        "sglang-node",
        "deepseek-chat",
    ],
)
def test_provider_name_match(driver_name):
    assert should_enable_append_only("auto", "https://api.openai.com/v1", driver_name)


@pytest.mark.parametrize(
    "base_url",
    [
        "http://localhost:11434/v1",
        "http://127.0.0.1:8080",
        "http://0.0.0.0:8000",
        "http://[::1]:1234",
        "http://10.0.0.5/v1",
        "http://192.168.1.42:8080",
        "http://172.16.0.1/v1",
        "http://172.31.255.1/v1",
        "http://llama-box.local:8080",
    ],
)
def test_local_base_urls(base_url):
    assert should_enable_append_only("auto", base_url, "custom-provider")


@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.openai.com/v1",
        "https://openrouter.ai/api/v1",
        "not-a-url",
        "",
    ],
)
def test_public_or_garbage_urls(base_url):
    assert not should_enable_append_only("auto", base_url, "gpt-4")


def test_settings_on_off():
    assert should_enable_append_only("on", "https://api.openai.com/v1", "gpt-4")
    assert not should_enable_append_only("off", "http://localhost:11434", "ollama")


def test_env_normalization(monkeypatch):
    monkeypatch.setenv("HARNESS_APPEND_ONLY_CONTEXT", "ON")
    assert append_only_setting() == "on"
    monkeypatch.setenv("HARNESS_APPEND_ONLY_CONTEXT", "0")
    assert append_only_setting() == "off"
    monkeypatch.setenv("HARNESS_APPEND_ONLY_CONTEXT", "true")
    assert append_only_setting() == "on"
    monkeypatch.setenv("HARNESS_APPEND_ONLY_CONTEXT", "false")
    assert append_only_setting() == "off"
    monkeypatch.delenv("HARNESS_APPEND_ONLY_CONTEXT", raising=False)
    assert append_only_setting() == "auto"
    monkeypatch.setenv("HARNESS_APPEND_ONLY_CONTEXT", "garbage")
    assert append_only_setting() == "auto"


def test_never_raises():
    should_enable_append_only(None, None, None)  # type: ignore[arg-type]


# --- Send-loop integration (Task C) ---

import json
import shutil
import tempfile

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession


class _RecordingPilot:
    name = "recording-pilot"
    base_url = "http://localhost:11434/v1"

    def __init__(self, turns):
        self.turns = turns
        self.step = 0
        self.prompts: list[str] = []
        self.system_prompts: list[str] = []

    def complete(self, prompt, *, system=None):
        from pmharness.drivers.openai_compat import DriverResponse

        self.prompts.append(prompt)
        self.system_prompts.append(system or "")
        txt = self.turns[min(self.step, len(self.turns) - 1)]
        self.step += 1
        return DriverResponse(text=txt, tokens_out=5, latency_ms=1.0)


def _make_session(monkeypatch, *, append_only: str = "on"):
    monkeypatch.setenv("HARNESS_APPEND_ONLY_CONTEXT", append_only)
    temp_dir = tempfile.mkdtemp()
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=temp_dir)
    cfg.repo = temp_dir
    session = ConversationalSession(cfg)
    return session, temp_dir


def test_append_only_prefix_stable_across_turns(monkeypatch):
    cg_sections = iter(["CG-SECTION-ALPHA", "CG-SECTION-BETA"])

    def fake_codegraph_context(task, cwd):
        return f"slice-for-{task}"

    def fake_codegraph_prompt_section(cg_slice):
        return next(cg_sections)

    monkeypatch.setattr(
        "puppetmaster.codegraph.codegraph_context",
        fake_codegraph_context,
    )
    monkeypatch.setattr(
        "puppetmaster.codegraph.codegraph_prompt_section",
        fake_codegraph_prompt_section,
    )

    session, temp_dir = _make_session(monkeypatch, append_only="on")
    try:
        turns = [
            json.dumps({"say": "turn one", "actions": []}),
            json.dumps({"say": "turn two", "actions": []}),
        ]
        pilot = _RecordingPilot(turns)
        session.pilot = pilot
        list(session.send("first task"))
        sys_after_turn1 = session._history[0]["content"]
        list(session.send("second task"))
        sys_after_turn2 = session._history[0]["content"]
        assert sys_after_turn1 == sys_after_turn2
        assert len(pilot.prompts) == 2
        assert pilot.prompts[1].startswith(pilot.prompts[0])
        user_contents = [
            m["content"]
            for m in session._history
            if m.get("role") == "user" and not m.get("_compressed_summary")
        ]
        assert any("CG-SECTION-ALPHA" in c for c in user_contents)
        assert any("CG-SECTION-BETA" in c for c in user_contents)
        assert not any("CG-SECTION-ALPHA" in s for s in pilot.system_prompts)
        assert not any("CG-SECTION-BETA" in s for s in pilot.system_prompts)
    finally:
        shutil.rmtree(temp_dir)


def test_append_only_off_keeps_turn_note_in_system(monkeypatch):
    monkeypatch.setenv("HARNESS_TURN_BUDGET", "on")
    session, temp_dir = _make_session(monkeypatch, append_only="off")
    try:
        pilot = _RecordingPilot([json.dumps({"say": "ok", "actions": []})])
        session.pilot = pilot
        list(session.send("Summarize +50k"))
        assert any(
            "output budget for this turn: 50000 tokens" in system
            for system in pilot.system_prompts
        )
    finally:
        shutil.rmtree(temp_dir)
