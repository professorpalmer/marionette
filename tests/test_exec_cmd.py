from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

from harness import exec_cmd

ROOT = Path(__file__).resolve().parents[1]


def _ev(kind, **data):
    return SimpleNamespace(kind=kind, data=data)


class _FakeSession:
    """Turn 1 starts a background job; the job finishes; the pilot resumes."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.sends = []
        self.polls = 0

    def send(self, message, resume=False):
        self.sends.append((message, resume))
        if not resume:
            yield _ev("message", text="dispatched")
            yield _ev("assistant_done")
        else:
            yield _ev("message", text="summary of worker results")
            yield _ev("assistant_done")

    def has_pending_swarms(self):
        return self.polls == 0

    def drain_swarm_results(self):
        self.polls += 1
        if self.polls == 1:
            yield _ev("swarm_result", job_id="local-1")
            yield _ev("pilot_resume", job_id="local-1")


def test_exec_resumes_the_pilot_after_background_work(monkeypatch, tmp_path, capsys):
    made = []

    def factory(cfg):
        made.append(_FakeSession(cfg))
        return made[-1]

    monkeypatch.setattr("harness.conversation.ConversationalSession", factory)
    monkeypatch.setattr(exec_cmd, "POLL_SECONDS", 0)
    events = tmp_path / "events.jsonl"

    code = exec_cmd.run_exec(["fix it", "--repo", str(tmp_path), "--events", str(events)])

    assert code == 0
    assert made[0].sends == [("fix it", False), ("", True)]
    out = capsys.readouterr().out
    assert "dispatched" in out and "summary of worker results" in out
    kinds = [json.loads(line)["kind"] for line in events.read_text().splitlines()]
    assert kinds.count("pilot_resume") == 1 and kinds[-1] == "assistant_done"


def test_exec_returns_1_on_an_error_event(monkeypatch, tmp_path):
    class Failing(_FakeSession):
        def send(self, message, resume=False):
            yield _ev("error", error="provider down")

        def has_pending_swarms(self):
            return False

        def drain_swarm_results(self):
            return iter(())

    monkeypatch.setattr("harness.conversation.ConversationalSession", Failing)
    assert exec_cmd.run_exec(["task", "--repo", str(tmp_path)]) == 1


def _meter():
    spec = importlib.util.spec_from_file_location("bench_meter", ROOT / "bench" / "harbor" / "meter.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_meter_sets_one_model_effort_and_provider():
    meter = _meter()
    cfg = SimpleNamespace(model="deepseek/deepseek-v4.1-flash", provider="deepinfra/fp8", effort="high", max_output=393216)
    out = meter.rewrite_body({"model": "z-ai/glm-5.3", "reasoning_effort": "low", "messages": [], "stream": True}, cfg)
    assert out["model"] == "deepseek/deepseek-v4.1-flash"
    assert out["reasoning"] == {"effort": "high"} and "reasoning_effort" not in out
    assert out["provider"] == {"only": ["deepinfra/fp8"], "allow_fallbacks": False}
    assert out["max_tokens"] == 393216
    assert out["stream_options"]["include_usage"] is True


def test_meter_keeps_a_reply_limit_the_harness_sent():
    meter = _meter()
    cfg = SimpleNamespace(model="m", provider="p", effort="low", max_output=393216)
    assert meter.rewrite_body({"max_completion_tokens": 4096}, cfg).get("max_tokens") is None


def test_exec_lifts_the_turn_caps_unless_the_caller_set_them(monkeypatch, tmp_path):
    seen = {}

    class Probe(_FakeSession):
        def send(self, message, resume=False):
            import os
            seen["budget"] = os.environ.get("HARNESS_PILOT_TOOL_BUDGET")
            seen["steps"] = os.environ.get("HARNESS_MAX_PILOT_STEPS")
            return iter(())

        def has_pending_swarms(self):
            return False

        def drain_swarm_results(self):
            return iter(())

    monkeypatch.setattr("harness.conversation.ConversationalSession", Probe)
    monkeypatch.delenv("HARNESS_PILOT_TOOL_BUDGET", raising=False)
    monkeypatch.setenv("HARNESS_MAX_PILOT_STEPS", "7")
    assert exec_cmd.run_exec(["task", "--repo", str(tmp_path)]) == 0
    assert seen == {"budget": "0", "steps": "7"}


def test_meter_reads_the_trial_tag_and_scalar_params():
    meter = _meter()
    assert meter.trial_tag("/t/fix-git__abc/v1/chat/completions") == "fix-git__abc"
    assert meter.trial_tag("/v1/chat/completions") == ""
    params = meter.request_params({"model": "m", "temperature": 0.2, "stream": True, "messages": [
        {"role": "assistant", "content": "", "reasoning_content": "x"}], "tools": []})
    assert params == {"temperature": 0.2, "stream": True, "reasoning_replayed": 1}
