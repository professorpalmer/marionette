"""Prompt-cache affinity: stable harness_session_id → pilot session_id kwarg."""
from __future__ import annotations

import queue
from types import SimpleNamespace
from typing import Dict, List, Optional

from harness.send_loop_phases import (
    maybe_attach_pilot_session_id,
    pilot_accepts_session_id,
    run_stream,
)
from pmharness.drivers.codex_responses import CodexResponsesDriver


def _stream_session(pilot, harness_session_id: str, *, messages=None):
    history = messages or [{"role": "user", "content": "hi"}]
    return SimpleNamespace(
        pilot=pilot,
        harness_session_id=harness_session_id,
        _messages_for_provider=lambda: history,
    )


def test_pilot_accepts_session_id_explicit_param():
    def chat_stream(messages, *, session_id=None, on_delta):
        pass

    assert pilot_accepts_session_id(chat_stream) is True


def test_pilot_accepts_session_id_kwargs_driver():
    def chat_stream(messages, **kwargs):
        pass

    assert pilot_accepts_session_id(chat_stream) is True


def test_pilot_accepts_session_id_legacy_driver():
    def chat_stream(
        messages,
        *,
        tools=None,
        system=None,
        on_delta,
        on_reasoning_delta=None,
        on_tool_hint=None,
    ):
        pass

    assert pilot_accepts_session_id(chat_stream) is False


def test_maybe_attach_skips_empty_session_id():
    kwargs: dict = {}
    maybe_attach_pilot_session_id(
        kwargs,
        lambda messages, *, session_id=None: None,
        "",
    )
    assert "session_id" not in kwargs


def test_maybe_attach_skips_legacy_driver():
    kwargs: dict = {}

    def legacy(messages, *, tools=None, system=None):
        pass

    maybe_attach_pilot_session_id(kwargs, legacy, "sess-a")
    assert "session_id" not in kwargs


def test_run_stream_passes_stable_session_id_across_turns():
    seen: List[Optional[str]] = []

    def chat_stream(messages, **kwargs):
        seen.append(kwargs.get("session_id"))
        return SimpleNamespace(text="ok")

    pilot = SimpleNamespace(chat_stream=chat_stream, supports_streaming=True)
    session = _stream_session(pilot, "marionette-chat-7")

    for _ in range(2):
        q: queue.Queue = queue.Queue()
        run_stream(session, q, [], "sys")
        while not q.empty():
            q.get_nowait()

    assert seen == ["marionette-chat-7", "marionette-chat-7"]


def test_run_stream_resume_turn_reuses_same_session_id():
    """Resume keeps harness_session_id on the session object — same cache key."""
    seen: List[Optional[str]] = []

    def chat_stream(messages, **kwargs):
        seen.append(kwargs.get("session_id"))
        return SimpleNamespace(text="ok")

    pilot = SimpleNamespace(chat_stream=chat_stream, supports_streaming=True)
    session = _stream_session(pilot, "resume-sess-1")

    q1: queue.Queue = queue.Queue()
    run_stream(session, q1, [], "sys")
    while not q1.empty():
        q1.get_nowait()

    # Resume path reuses the same session runner; harness_session_id unchanged.
    q2: queue.Queue = queue.Queue()
    run_stream(session, q2, [], "sys")
    while not q2.empty():
        q2.get_nowait()

    assert seen == ["resume-sess-1", "resume-sess-1"]


def test_run_stream_different_chat_sessions_get_different_ids():
    captured: Dict[str, Optional[str]] = {}

    def make_chat_stream(label):
        def chat_stream(messages, **kwargs):
            captured[label] = kwargs.get("session_id")
            return SimpleNamespace(text="ok")
        return chat_stream

    for label, sid in (("a", "chat-alpha"), ("b", "chat-beta")):
        pilot = SimpleNamespace(
            chat_stream=make_chat_stream(label),
            supports_streaming=True,
        )
        session = _stream_session(pilot, sid)
        q: queue.Queue = queue.Queue()
        run_stream(session, q, [], "sys")
        while not q.empty():
            q.get_nowait()

    assert captured == {"a": "chat-alpha", "b": "chat-beta"}
    assert captured["a"] != captured["b"]


def test_run_stream_legacy_driver_receives_no_session_id_kwarg():
    """Anthropic-style signature must not get an unexpected session_id kwarg."""
    q: queue.Queue = queue.Queue()

    def chat_stream(
        messages,
        *,
        tools=None,
        system=None,
        on_delta,
        on_reasoning_delta=None,
        on_tool_hint=None,
    ):
        return SimpleNamespace(text="ok")

    session = _stream_session(
        SimpleNamespace(chat_stream=chat_stream),
        "sess-legacy",
    )
    run_stream(session, q, [], "sys")
    kind, val = q.get_nowait()
    assert kind == "done"
    assert val.text == "ok"


def test_run_stream_kwargs_driver_receives_session_id():
    captured: dict = {}

    def chat_stream(messages, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(text="ok")

    session = _stream_session(
        SimpleNamespace(chat_stream=chat_stream),
        "kwargs-sess",
    )
    q: queue.Queue = queue.Queue()
    run_stream(session, q, [], "sys")
    while not q.empty():
        q.get_nowait()
    assert captured["session_id"] == "kwargs-sess"


def test_sync_chat_path_attaches_session_id():
    captured: dict = {}

    def chat(messages, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(text="ok", error=None)

    kwargs: dict = {"tools": [], "system": "sys"}
    maybe_attach_pilot_session_id(kwargs, chat, "sync-sess-9")
    chat([{"role": "user", "content": "hi"}], **kwargs)
    assert captured["session_id"] == "sync-sess-9"


def test_sync_complete_path_attaches_session_id():
    captured: dict = {}

    def complete(prompt, *, system=None, session_id=None):
        captured["session_id"] = session_id
        captured["system"] = system
        return SimpleNamespace(text="ok", error=None)

    kwargs: dict = {"system": "sys"}
    maybe_attach_pilot_session_id(kwargs, complete, "complete-sess-3")
    complete("ping", **kwargs)
    assert captured["session_id"] == "complete-sess-3"


def test_compaction_complete_path_excludes_session_id():
    """Summarizers call complete() directly — must not inherit chat affinity."""
    captured: dict = {}

    def complete(prompt, *, system=None, session_id=None):
        captured["session_id"] = session_id
        return SimpleNamespace(text="summary", error=None)

    # Mirror compaction_mixin: no maybe_attach_pilot_session_id.
    complete("fold history", system="compaction summarizer")
    assert captured["session_id"] is None


def test_codex_build_body_sets_prompt_cache_key_from_session_id():
    from pmharness.drivers.codex_responses import _codex_logical_thread_id
    from pmharness.drivers.prompt_cache import durable_codex_prompt_cache_key

    driver = CodexResponsesDriver(name="openai-codex/test", model="gpt-5")
    body = driver._build_body(
        [{"role": "user", "content": "hello"}],
        session_id="codex-stable-sess",
    )
    expected_key = durable_codex_prompt_cache_key("codex-stable-sess")
    expected_thread = _codex_logical_thread_id(expected_key)
    assert body["prompt_cache_key"] == expected_key
    assert expected_thread and expected_thread != expected_key
    assert body["client_metadata"] == {
        "session_id": expected_key,
        "thread_id": expected_thread,
    }


def test_codex_one_chat_maps_to_one_logical_thread():
    """One Marionette chat → durable session identity + distinct logical thread.

    Native Codex splits session_id (prompt cache) from thread_id
    (x-client-request-id). Marionette keeps prompt_cache_key / session_id on
    the durable cache key and derives a stable UUID5 thread id from it.
    """
    from pmharness.drivers.codex_responses import (
        _codex_client_metadata,
        _codex_logical_thread_id,
        _codex_session_affinity_headers,
    )
    from pmharness.drivers.prompt_cache import durable_codex_prompt_cache_key

    session_a = "chat-logical-thread-a"
    session_b = "chat-logical-thread-b"
    key_a = durable_codex_prompt_cache_key(session_a)
    key_b = durable_codex_prompt_cache_key(session_b)
    assert key_a and key_b and key_a != key_b

    thread_a = _codex_logical_thread_id(key_a)
    thread_b = _codex_logical_thread_id(key_b)
    assert thread_a and thread_b
    assert thread_a != key_a
    assert thread_b != key_b
    assert thread_a != thread_b
    # Deterministic / stable across repeated derivation.
    assert _codex_logical_thread_id(key_a) == thread_a

    headers_a = _codex_session_affinity_headers(key_a)
    assert headers_a == {
        "session-id": key_a,
        "thread-id": thread_a,
        "x-client-request-id": thread_a,
    }
    # Stable across repeated header builds (not a fresh UUID per POST).
    assert _codex_session_affinity_headers(key_a) == headers_a
    assert _codex_client_metadata(key_a) == {
        "session_id": key_a,
        "thread_id": thread_a,
    }

    headers_b = _codex_session_affinity_headers(key_b)
    assert headers_b == {
        "session-id": key_b,
        "thread-id": thread_b,
        "x-client-request-id": thread_b,
    }
    assert headers_b["x-client-request-id"] != headers_a["x-client-request-id"]
    assert headers_b["session-id"] != headers_a["session-id"]

    driver = CodexResponsesDriver(name="openai-codex/test", model="gpt-5")
    body_a = driver._build_body(
        [{"role": "user", "content": "hello"}],
        session_id=session_a,
    )
    body_a2 = driver._build_body(
        [{"role": "user", "content": "again"}],
        session_id=session_a,
    )
    body_b = driver._build_body(
        [{"role": "user", "content": "other chat"}],
        session_id=session_b,
    )
    assert body_a["prompt_cache_key"] == key_a
    assert body_a2["prompt_cache_key"] == key_a
    assert body_a["client_metadata"]["session_id"] == key_a
    assert body_a["client_metadata"]["thread_id"] == thread_a
    assert body_a2["client_metadata"]["thread_id"] == thread_a
    assert body_b["client_metadata"]["session_id"] == key_b
    assert body_b["client_metadata"]["thread_id"] == thread_b
    assert body_b["client_metadata"]["thread_id"] != body_a["client_metadata"]["thread_id"]


def test_codex_build_body_omits_prompt_cache_key_without_session_id():
    driver = CodexResponsesDriver(name="openai-codex/test", model="gpt-5")
    body = driver._build_body(
        [{"role": "user", "content": "hello"}],
        session_id=None,
    )
    assert "prompt_cache_key" not in body
    assert "client_metadata" not in body


def test_codex_build_body_cache_kill_switch_omits_identity_markers(monkeypatch):
    monkeypatch.setenv("HARNESS_PROMPT_CACHE", "0")
    monkeypatch.setenv("HARNESS_CODEX_PROMPT_CACHE_BREAKPOINT", "on")

    from pmharness.drivers.codex_responses import (
        _codex_client_metadata,
        _codex_logical_thread_id,
        _codex_session_affinity_headers,
    )

    driver = CodexResponsesDriver(name="openai-codex/test", model="gpt-5.6-luna")
    body = driver._build_body(
        [{"role": "user", "content": "hello"}],
        session_id="codex-disabled-sess",
    )

    assert "prompt_cache_key" not in body
    assert "prompt_cache_options" not in body
    assert "client_metadata" not in body
    # Fail-closed: no affinity headers / metadata when kill switch removed the key.
    assert _codex_session_affinity_headers(body.get("prompt_cache_key")) == {}
    assert _codex_client_metadata(body.get("prompt_cache_key")) == {}
    assert _codex_logical_thread_id(body.get("prompt_cache_key")) is None
    assert not any(
        isinstance(item, dict)
        and any(
            isinstance(part, dict)
            and part.get("prompt_cache_breakpoint") is not None
            for part in item.get("content") or []
        )
        for item in body.get("input") or []
    )


def test_codex_chat_stream_wires_session_id_to_body(monkeypatch):
    """End-to-end: run_stream session_id → CodexResponsesDriver._build_body."""
    from pmharness.drivers.prompt_cache import durable_codex_prompt_cache_key

    posted: list[dict] = []

    class _CaptureDriver(CodexResponsesDriver):
        def _post_stream(self, body, **kwargs):
            posted.append(body)
            return SimpleNamespace(
                text="ok",
                tokens_in=0,
                tokens_out=0,
                latency_ms=0,
                model=self.name,
                meta={},
            )

    driver = _CaptureDriver(name="openai-codex/test", model="gpt-5")
    session = _stream_session(
        SimpleNamespace(chat_stream=driver.chat_stream, supports_streaming=True),
        "codex-wire-sess",
    )
    q: queue.Queue = queue.Queue()
    run_stream(session, q, [], "sys")
    while not q.empty():
        q.get_nowait()

    assert posted, "expected Codex driver to post a body"
    assert posted[0]["prompt_cache_key"] == durable_codex_prompt_cache_key(
        "codex-wire-sess"
    )
