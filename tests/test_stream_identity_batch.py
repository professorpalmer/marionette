"""Stream identity batching protects the 512-frame SSE replay ring."""

from __future__ import annotations

import queue
import time

from harness.api.sse import SseEventRing, _SSE_RING_CAP
from harness.send_loop_phases import drain_stream_queue
from harness.stream_identity import StreamDeltaBatch, normalize_delta_payload


def test_normalize_delta_payload_accepts_str_and_dict():
    assert normalize_delta_payload("hi") == ("hi", {})
    text, meta = normalize_delta_payload({
        "text": "x",
        "stream_id": "msg_1",
        "output_index": 2,
        "channel": "progress",
    })
    assert text == "x"
    assert meta == {
        "stream_id": "msg_1",
        "output_index": 2,
        "channel": "progress",
    }


def test_stream_delta_batch_merges_same_identity():
    bat = StreamDeltaBatch(max_ms=10_000, max_chars=10_000)
    assert bat.push("Hello", {"stream_id": "a", "channel": "progress"}, default_channel="progress") is None
    assert bat.push(" world", {"stream_id": "a", "channel": "progress"}, default_channel="progress") is None
    # Identity change flushes prior buffer.
    flushed = bat.push("Other", {"stream_id": "b", "channel": "progress"}, default_channel="progress")
    assert flushed is not None
    assert flushed["text"] == "Hello world"
    assert flushed["stream_id"] == "a"
    final = bat.flush()
    assert final["text"] == "Other"
    assert final["stream_id"] == "b"


def test_stream_delta_batch_flushes_when_max_chars_overdue():
    """max_chars overdue must flush from push without an identity change."""
    bat = StreamDeltaBatch(max_ms=10_000, max_chars=10)
    assert bat.push("12345", {"stream_id": "a", "channel": "progress"}, default_channel="progress") is None
    flushed = bat.push("67890X", {"stream_id": "a", "channel": "progress"}, default_channel="progress")
    assert flushed is not None
    assert flushed["text"] == "1234567890X"
    assert flushed["stream_id"] == "a"
    assert bat.pending is False


def test_stream_delta_batch_identity_change_returns_old_when_new_also_overdue():
    """Identity change must return the prior flush even if the new buffer is overdue."""
    bat = StreamDeltaBatch(max_ms=10_000, max_chars=5)
    assert bat.push("ab", {"stream_id": "a", "channel": "progress"}, default_channel="progress") is None
    # New identity is longer than max_chars — must still surface old "ab" first.
    flushed = bat.push(
        "overdue-new",
        {"stream_id": "b", "channel": "progress"},
        default_channel="progress",
    )
    assert flushed is not None
    assert flushed["text"] == "ab"
    assert flushed["stream_id"] == "a"
    # New overdue text may remain pending for a later flush mechanism.
    if bat.pending:
        final = bat.flush()
        assert final is not None
        assert final["text"] == "overdue-new"
        assert final["stream_id"] == "b"


def test_stream_delta_batch_flushes_when_max_ms_overdue(monkeypatch):
    """max_ms overdue must flush from push once the batch window elapses."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["t"])
    bat = StreamDeltaBatch(max_ms=40, max_chars=10_000)
    assert bat.push("Hi", {"stream_id": "a", "channel": "progress"}, default_channel="progress") is None
    clock["t"] = 1000.05  # 50ms later — past max_ms
    flushed = bat.push(" there", {"stream_id": "a", "channel": "progress"}, default_channel="progress")
    assert flushed is not None
    assert flushed["text"] == "Hi there"
    assert flushed["stream_id"] == "a"
    assert bat.pending is False


def test_drain_batches_word_deltas_without_exhausting_sse_ring():
    """500+ same-stream word deltas must not emit 500+ SSE frames."""
    q: queue.Queue = queue.Queue()
    words = [f"w{i} " for i in range(520)]
    for w in words:
        q.put((
            "delta",
            {
                "text": w,
                "stream_id": "msg_progress",
                "channel": "progress",
                "output_index": 1,
            },
        ))
    q.put(("done", type("R", (), {"meta": {}})()))

    events = []
    gen = drain_stream_queue(q)
    try:
        while True:
            events.append(next(gen))
    except StopIteration as stop:
        streamed, _resp = stop.value

    deltas = [e for e in events if e.kind == "message_delta"]
    # Batched well under the ring cap — never one frame per word.
    assert len(deltas) < 80
    assert len(deltas) < _SSE_RING_CAP // 2
    joined = "".join(d.data["text"] for d in deltas)
    assert joined == "".join(words)
    assert all(d.data.get("stream_id") == "msg_progress" for d in deltas)

    # Replay through the SSE ring reconstructs without duplicates.
    ring = SseEventRing("sess-batch", 1)
    for ev in deltas:
        ring.append(ev.kind, ev.data)
    replay = ring.since(0)
    assert replay["gap"] is False
    replayed = "".join(
        (e.get("data") or {}).get("text") or ""
        for e in replay["events"]
        if e.get("kind") == "message_delta"
    )
    assert replayed == joined
    assert streamed == ""  # progress bypasses say extractor / streamed_prose


def test_drain_flushes_before_tool_hint_barrier():
    q: queue.Queue = queue.Queue()
    q.put(("delta", {"text": "pre ", "stream_id": "p1", "channel": "progress"}))
    q.put(("tool_hint", "read_file"))
    q.put(("delta", {"text": "post", "stream_id": "a1", "channel": "answer"}))
    q.put(("done", type("R", (), {"meta": {}})()))

    events = []
    gen = drain_stream_queue(q)
    try:
        while True:
            events.append(next(gen))
    except StopIteration:
        pass

    kinds = [e.kind for e in events]
    assert "tool_prep" in kinds
    # Progress text must appear before tool_prep (barrier flush).
    tool_at = kinds.index("tool_prep")
    assert any(
        e.kind == "message_delta" and "pre" in e.data.get("text", "")
        for e in events[:tool_at]
    )
