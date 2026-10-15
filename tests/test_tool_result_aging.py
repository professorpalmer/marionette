from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.tool_pairing import in_progress_count
from harness.tool_result_aging import (
    AGED_PREFIX,
    age_tool_results,
    cache_is_cold,
    maybe_age_session_tool_results,
)

BIG = "x" * 3000


def _round(i: int, content: str = BIG, name: str = "read_file") -> list:
    call_id = f"call_{i}"
    return [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": call_id, "content": content},
    ]


def _history(rounds: int, **kw) -> list:
    out = [{"role": "user", "content": "start"}]
    for i in range(rounds):
        out += _round(i, **kw)
    out.append({"role": "assistant", "content": "done"})
    return out


def _tool_contents(history: list) -> dict:
    return {m["tool_call_id"]: m["content"] for m in history if m.get("role") == "tool"}


def test_keeps_recent_rounds_whole():
    history = _history(8)
    new, aged = age_tool_results(history, keep_recent_rounds=6)
    assert aged == frozenset({"call_0", "call_1"})
    contents = _tool_contents(new)
    for i in range(2, 8):
        assert contents[f"call_{i}"] == BIG
    assert contents["call_0"].startswith(AGED_PREFIX)
    assert history[2]["content"] == BIG  # input not mutated


def test_small_results_untouched():
    history = _history(8, content="short")
    new, aged = age_tool_results(history, keep_recent_rounds=2, min_chars=1500)
    assert aged == frozenset()
    assert new == history


def test_stub_names_tool_size_and_recovery():
    history = _history(3, name="run_command")
    new, _ = age_tool_results(history, keep_recent_rounds=1)
    stub = _tool_contents(new)["call_0"]
    assert stub == (
        "[aged tool result: run_command returned 3000 chars. "
        "Not stored; run run_command again if you need it.]"
    )
    spilled = "<persisted-output> preview spill://sess/call_0 " + BIG
    new, _ = age_tool_results(_history(3, content=spilled), keep_recent_rounds=1)
    assert "Full text: read_file spill://sess/call_0]" in _tool_contents(new)["call_0"]
    new, _ = age_tool_results(
        history, keep_recent_rounds=1, recover=lambda body, cid: f"spill://s/{cid}",
    )
    assert _tool_contents(new)["call_1"].endswith("read_file spill://s/call_1]")


def test_pairing_intact():
    history = _history(8)
    new, _ = age_tool_results(history, keep_recent_rounds=2)
    assert [m.get("tool_call_id") for m in new] == [m.get("tool_call_id") for m in history]
    assert [m.get("tool_calls") for m in new] == [m.get("tool_calls") for m in history]
    assert in_progress_count(new) == 0


def test_idempotent():
    history = _history(8)
    once, aged = age_tool_results(history, keep_recent_rounds=2)
    twice, aged_again = age_tool_results(once, keep_recent_rounds=2)
    assert twice == once
    assert aged_again == frozenset()
    thrice, _ = age_tool_results(once, keep_recent_rounds=2, already_aged=aged)
    assert thrice == once


class _Driver:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def cache_snapshot(self):
        return self.snapshot


def _snap(started, ttl=300, reason=""):
    return SimpleNamespace(started=started, ttl_seconds=ttl, reason=reason, cached_tokens=10)


def test_cache_gate():
    session = SimpleNamespace(pilot=_Driver(_snap(100.0)))
    assert not cache_is_cold(session, 1000.0)  # no request yet: unknown
    session._last_provider_request_at = 100.0
    assert not cache_is_cold(session, 399.0)  # warm
    assert cache_is_cold(session, 400.0)
    session.pilot = _Driver(None)
    assert cache_is_cold(session, 101.0)
    session.pilot = _Driver(_snap(100.0, reason="unsupported"))
    assert not cache_is_cold(session, 399.0)
    assert cache_is_cold(session, 400.0)
    session.pilot = object()
    assert not cache_is_cold(session, 399.0)
    assert cache_is_cold(session, 400.0)


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.delenv("HARNESS_TOOL_RESULT_AGING", raising=False)
    s = ConversationalSession(HarnessConfig(
        repo=str(tmp_path), driver="stub-oracle-v2", state_dir=str(tmp_path / "state"),
    ))
    s._history = s._history[:1] + _history(8)
    return s


def test_session_ages_only_when_cold(session):
    session.pilot = _Driver(_snap(100.0))
    session._last_provider_request_at = 100.0
    assert maybe_age_session_tool_results(session, clock=lambda: 200.0) is None
    assert _tool_contents(session._history)["call_0"] == BIG
    payload = maybe_age_session_tool_results(session, clock=lambda: 500.0)
    assert payload["aged_results"] == 2
    assert payload["chars_reclaimed"] > 5000
    assert payload["after_tokens"] < payload["before_tokens"]
    assert session._tool_results_aged == {"call_0", "call_1"}


def test_aged_set_is_monotonic_and_persisted(session):
    session.pilot = _Driver(None)
    session._last_provider_request_at = 0.0
    maybe_age_session_tool_results(session, clock=lambda: 1.0)
    first = _tool_contents(session._history)
    session._history.append({"role": "user", "content": "more"})
    for i in range(10, 13):
        session._history += _round(i)
    maybe_age_session_tool_results(session, clock=lambda: 2.0)
    assert session._tool_results_aged == {"call_0", "call_1", "call_2", "call_3", "call_4"}
    later = _tool_contents(session._history)
    assert later["call_0"] == first["call_0"] and later["call_1"] == first["call_1"]
    data = json.loads(json.dumps(session.export_transcript_data()))
    assert set(data["tool_results_aged"]) == session._tool_results_aged
    other = ConversationalSession(HarnessConfig(
        repo=session.config.repo, driver="stub-oracle-v2", state_dir=session.state_dir,
    ))
    other.load_history(data)
    assert other._tool_results_aged == session._tool_results_aged
    assert other.export_history() == session.export_history()


@pytest.mark.parametrize("value", ["0", "off"])
def test_env_off_switch(session, monkeypatch, value):
    monkeypatch.setenv("HARNESS_TOOL_RESULT_AGING", value)
    session.pilot = _Driver(None)
    session._last_provider_request_at = 0.0
    assert maybe_age_session_tool_results(session, clock=lambda: 9999.0) is None
    assert _tool_contents(session._history)["call_0"] == BIG


def test_send_ages_before_first_provider_call(session, monkeypatch):
    monkeypatch.setattr("harness.conversation._prewarm_worker_imports", lambda: None)
    session._last_provider_request_at = -10_000.0
    events = list(session.send("next question"))
    aged = [e for e in events if e.kind == "compaction" and e.data.get("mode") == "tool_result_aging"]
    assert len(aged) == 1
    assert _tool_contents(session._history)["call_0"].startswith(AGED_PREFIX)
