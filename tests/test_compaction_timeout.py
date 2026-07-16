"""Compaction summarizer timeout + cooldown."""
from __future__ import annotations

import json
import shutil
import tempfile
import time

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession


class _HangPilot:
    name = "hang-pilot"
    base_url = "http://localhost:11434/v1"

    def complete(self, prompt, *, system=None):
        time.sleep(30)
        from pmharness.drivers.openai_compat import DriverResponse
        return DriverResponse(text="never", tokens_out=1, latency_ms=1.0)


class _OkPilot:
    name = "ok-pilot"
    base_url = "http://localhost:11434/v1"

    def __init__(self):
        self.calls = 0

    def complete(self, prompt, *, system=None):
        self.calls += 1
        from pmharness.drivers.openai_compat import DriverResponse
        return DriverResponse(text="## Historical Task Snapshot\nok", tokens_out=5, latency_ms=1.0)


def _fat_session(monkeypatch, pilot, *, timeout_s="1", cooldown_s="60"):
    monkeypatch.setenv("HARNESS_COMPACTION_TIMEOUT_S", timeout_s)
    monkeypatch.setenv("HARNESS_COMPACTION_COOLDOWN_S", cooldown_s)
    monkeypatch.setenv("HARNESS_APPEND_ONLY_CONTEXT", "off")
    temp_dir = tempfile.mkdtemp()
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=temp_dir)
    cfg.repo = temp_dir
    cfg.max_context_tokens = 800
    session = ConversationalSession(cfg)
    session.pilot = pilot
    # Inflate history past the 0.75 trigger.
    session._history = [{"role": "system", "content": "sys"}]
    for i in range(20):
        session._history.append({
            "role": "user",
            "content": f"msg {i} " + ("x" * 400),
        })
        session._history.append({
            "role": "assistant",
            "content": json.dumps({"say": f"a{i}", "actions": []}),
        })
    return session, temp_dir


def test_compaction_timeout_uses_fallback_and_sets_cooldown(monkeypatch):
    session, temp_dir = _fat_session(monkeypatch, _HangPilot(), timeout_s="1", cooldown_s="90")
    try:
        events = list(session._maybe_compact_history(force=True))
        assert any(e.kind == "compaction" for e in events)
        assert session._compaction_fail_until > time.time()
        # Summary message present
        assert any(m.get("_compressed_summary") for m in session._history)
    finally:
        shutil.rmtree(temp_dir)


def test_compaction_cooldown_skips_llm(monkeypatch):
    pilot = _OkPilot()
    session, temp_dir = _fat_session(monkeypatch, pilot, timeout_s="30", cooldown_s="90")
    try:
        session._compaction_fail_until = time.time() + 60
        list(session._maybe_compact_history(force=True))
        assert pilot.calls == 0
    finally:
        shutil.rmtree(temp_dir)
