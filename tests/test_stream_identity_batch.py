"""Stream identity batching protects the 512-frame SSE replay ring."""

from __future__ import annotations

import queue
import time

from harness.api.sse import SseEventRing, _SSE_RING_CAP
from harness.send_loop_phases import drain_stream_queue
from harness.stream_identity import StreamDeltaBatch, normalize_delta_payload


class _CountingGetQueue(queue.Queue):
    """Queue that records drain-loop ``get`` calls so hold vs first-frame is observable."""

    def __init__(self) -> None:
        super().__init__()
        self.get_calls = 0

    def get(self, block=True, timeout=None):
        self.get_calls += 1
        return super().get(block=block, timeout=timeout)


def _collect_drain(q):
    events = []
    gen = drain_stream_queue(q)
    try:
        while True:
            events.append(next(gen))
    except StopIteration as stop:
        return events, stop.value


def _finish_gen(gen):
    extras = []
    try:
        while True:
            extras.append(next(gen))
    except StopIteration as stop:
        return extras, stop.value


def _replay_joined(events, kind):
    ring = SseEventRing("sess-batch", 1)
    for ev in events:
        ring.append(ev.kind, ev.data)
    replay = ring.since(0)
    assert replay["gap"] is False
    return "".join(
        (e.get("data") or {}).get("text") or ""
        for e in replay["events"]
        if e.get("kind") == kind
    )


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


class _RaiseOnSecondGet:
    """Queue stand-in: first get returns ``item``; a second get fails the test."""

    def __init__(self, item):
        self.calls = 0
        self._item = item

    def get(self, timeout=None):
        self.calls += 1
        if self.calls == 1:
            return self._item
        raise AssertionError("second queue get before first yield")


def test_drain_yields_first_identity_answer_before_second_get():
    q = _RaiseOnSecondGet(
        ("delta", {"text": "Hello", "stream_id": "a1", "channel": "answer"}),
    )
    ev = next(drain_stream_queue(q))
    assert ev.kind == "message_delta"
    assert ev.data["text"] == "Hello"
    assert ev.data.get("stream_id") == "a1"
    assert ev.data.get("channel") == "answer"
    assert q.calls == 1


def test_drain_yields_first_identity_reasoning_before_second_get():
    q = _RaiseOnSecondGet(
        ("reasoning", {"text": "Think", "stream_id": "rs1", "channel": "reasoning"}),
    )
    ev = next(drain_stream_queue(q))
    assert ev.kind == "thinking"
    assert ev.data["text"] == "Think"
    assert ev.data.get("stream_id") == "rs1"
    assert ev.data.get("channel") == "reasoning"
    assert ev.data.get("delta") is True
    assert q.calls == 1


def test_drain_first_overdue_identity_answer_still_yields_before_second_get():
    """push() may flush on the char threshold; that is still the first frame."""
    text = "x" * 80
    q = _RaiseOnSecondGet(
        ("delta", {"text": text, "stream_id": "a1", "channel": "answer"}),
    )
    ev = next(drain_stream_queue(q))
    assert ev.kind == "message_delta"
    assert ev.data["text"] == text
    assert ev.data.get("stream_id") == "a1"
    assert q.calls == 1


def test_drain_still_holds_first_identity_progress_until_later_get():
    """Progress stays on the batch timeout path — not a first-frame yield."""
    q = _RaiseOnSecondGet(
        ("delta", {"text": "pre", "stream_id": "p1", "channel": "progress"}),
    )
    try:
        next(drain_stream_queue(q))
    except AssertionError as exc:
        assert "second queue get before first yield" in str(exc)
        assert q.calls == 2
    else:
        raise AssertionError("progress must not yield before a second queue get")


def test_drain_batches_second_same_identity_answer_until_done(monkeypatch):
    """Second same-identity answer stays batched until the terminal get.

    Final texts ``['Hello', ' world']`` are also what first-framing every
    delta would produce — ``get_calls`` is what proves the hold.
    """
    monkeypatch.setattr(time, "monotonic", lambda: 1000.0)
    q = _CountingGetQueue()
    resp = type("R", (), {"meta": {}})()
    q.put(("delta", {"text": "Hello", "stream_id": "a1", "channel": "answer"}))
    q.put(("delta", {"text": " world", "stream_id": "a1", "channel": "answer"}))
    q.put(("done", resp))

    gen = drain_stream_queue(q)
    first = next(gen)
    assert first.kind == "message_delta"
    assert first.data["text"] == "Hello"
    assert first.data.get("stream_id") == "a1"
    assert q.get_calls == 1

    second = next(gen)
    assert second.kind == "message_delta"
    assert second.data["text"] == " world"
    assert second.data.get("stream_id") == "a1"
    # Held until the generator reads ``done`` (3rd get). First-framing the
    # second delta would yield it on get 2.
    assert q.get_calls == 3

    extras, (streamed, got) = _finish_gen(gen)
    assert extras == []
    assert streamed == "Hello world"
    assert got is resp
    assert q.get_calls == 3


def test_drain_batches_second_same_identity_reasoning_until_done(monkeypatch):
    """Second same-identity reasoning stays batched until the terminal get."""
    monkeypatch.setattr(time, "monotonic", lambda: 1000.0)
    q = _CountingGetQueue()
    resp = type("R", (), {"meta": {}})()
    q.put(("reasoning", {"text": "Why ", "stream_id": "rs1", "channel": "reasoning"}))
    q.put(("reasoning", {"text": "not", "stream_id": "rs1", "channel": "reasoning"}))
    q.put(("done", resp))

    gen = drain_stream_queue(q)
    first = next(gen)
    assert first.kind == "thinking"
    assert first.data["text"] == "Why "
    assert first.data.get("stream_id") == "rs1"
    assert first.data.get("delta") is True
    assert q.get_calls == 1

    second = next(gen)
    assert second.kind == "thinking"
    assert second.data["text"] == "not"
    assert second.data.get("stream_id") == "rs1"
    assert second.data.get("delta") is True
    assert q.get_calls == 3

    extras, (streamed, got) = _finish_gen(gen)
    assert extras == []
    assert streamed == ""
    assert got.meta["streamed_reasoning"] == "Why not"
    assert q.get_calls == 3


def test_drain_first_identity_answer_uses_say_extractor():
    q: queue.Queue = queue.Queue()
    resp = type("R", (), {"meta": {}})()
    q.put((
        "delta",
        {"text": '{"say": "Hi"}', "stream_id": "a1", "channel": "answer"},
    ))
    q.put(("done", resp))

    events = []
    gen = drain_stream_queue(q)
    try:
        while True:
            events.append(next(gen))
    except StopIteration as stop:
        streamed, _got = stop.value

    deltas = [e for e in events if e.kind == "message_delta"]
    assert [d.data["text"] for d in deltas] == ["Hi"]
    assert streamed == "Hi"


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


def test_drain_batches_answer_word_deltas_without_exhausting_sse_ring(monkeypatch):
    """520 same-identity answer words: first frame immediate, rest batched."""
    monkeypatch.setattr(time, "monotonic", lambda: 1000.0)
    q: queue.Queue = queue.Queue()
    words = [f"w{i} " for i in range(520)]
    for w in words:
        q.put((
            "delta",
            {
                "text": w,
                "stream_id": "msg_answer",
                "channel": "answer",
                "output_index": 1,
            },
        ))
    q.put(("done", type("R", (), {"meta": {}})()))

    events, (streamed, _resp) = _collect_drain(q)
    deltas = [e for e in events if e.kind == "message_delta"]
    assert deltas[0].data["text"] == words[0]
    # Immediate first frame + char-threshold batches — never one frame per word.
    assert 2 <= len(deltas) < 80
    assert len(deltas) < _SSE_RING_CAP // 2
    joined = "".join(d.data["text"] for d in deltas)
    assert joined == "".join(words)
    assert all(d.data.get("stream_id") == "msg_answer" for d in deltas)
    assert all(d.data.get("channel") == "answer" for d in deltas)
    assert _replay_joined(deltas, "message_delta") == joined
    assert streamed == joined


def test_drain_batches_reasoning_word_deltas_without_exhausting_sse_ring(monkeypatch):
    """520 same-identity reasoning words: first frame immediate, rest batched."""
    monkeypatch.setattr(time, "monotonic", lambda: 1000.0)
    q: queue.Queue = queue.Queue()
    words = [f"w{i} " for i in range(520)]
    for w in words:
        q.put((
            "reasoning",
            {
                "text": w,
                "stream_id": "msg_reasoning",
                "channel": "reasoning",
                "output_index": 1,
            },
        ))
    q.put(("done", type("R", (), {"meta": {}})()))

    events, (streamed, resp) = _collect_drain(q)
    thinking = [e for e in events if e.kind == "thinking"]
    assert thinking[0].data["text"] == words[0]
    assert 2 <= len(thinking) < 80
    assert len(thinking) < _SSE_RING_CAP // 2
    joined = "".join(t.data["text"] for t in thinking)
    assert joined == "".join(words)
    assert all(t.data.get("stream_id") == "msg_reasoning" for t in thinking)
    assert all(t.data.get("channel") == "reasoning" for t in thinking)
    assert all(t.data.get("delta") is True for t in thinking)
    assert _replay_joined(thinking, "thinking") == joined
    assert streamed == ""
    assert resp.meta["streamed_reasoning"] == joined


def test_drain_identity_change_after_first_frame_flushes_held_answer(monkeypatch):
    """After the first-frame emit, a new stream_id flushes the held tail first."""
    monkeypatch.setattr(time, "monotonic", lambda: 1000.0)
    q = _CountingGetQueue()
    resp = type("R", (), {"meta": {}})()
    q.put(("delta", {"text": "Hello", "stream_id": "a1", "channel": "answer"}))
    q.put(("delta", {"text": " world", "stream_id": "a1", "channel": "answer"}))
    q.put(("delta", {"text": "Other", "stream_id": "a2", "channel": "answer"}))
    q.put(("done", resp))

    gen = drain_stream_queue(q)
    first = next(gen)
    assert first.kind == "message_delta"
    assert first.data["text"] == "Hello"
    assert first.data.get("stream_id") == "a1"
    assert q.get_calls == 1

    held = next(gen)
    assert held.kind == "message_delta"
    assert held.data["text"] == " world"
    assert held.data.get("stream_id") == "a1"
    # Identity-change get (3) flushes the held same-identity tail.
    assert q.get_calls == 3

    changed = next(gen)
    assert changed.kind == "message_delta"
    assert changed.data["text"] == "Other"
    assert changed.data.get("stream_id") == "a2"
    # New identity first-frames on the same identity-change get.
    assert q.get_calls == 3

    extras, (streamed, got) = _finish_gen(gen)
    assert extras == []
    assert streamed == "Hello worldOther"
    assert got is resp


def test_drain_answer_reasoning_interleave_preserves_order(monkeypatch):
    """Interleaved answer/reasoning first-frames then holds with no drops."""
    monkeypatch.setattr(time, "monotonic", lambda: 1000.0)
    q = _CountingGetQueue()
    resp = type("R", (), {"meta": {}})()
    q.put(("delta", {"text": "A1", "stream_id": "a1", "channel": "answer"}))
    q.put(("reasoning", {"text": "R1", "stream_id": "rs1", "channel": "reasoning"}))
    q.put(("delta", {"text": "A2", "stream_id": "a1", "channel": "answer"}))
    q.put(("reasoning", {"text": "R2", "stream_id": "rs1", "channel": "reasoning"}))
    q.put(("done", resp))

    gen = drain_stream_queue(q)
    a1 = next(gen)
    assert a1.kind == "message_delta"
    assert a1.data["text"] == "A1"
    assert q.get_calls == 1

    r1 = next(gen)
    assert r1.kind == "thinking"
    assert r1.data["text"] == "R1"
    assert r1.data.get("delta") is True
    assert q.get_calls == 2

    a2 = next(gen)
    assert a2.kind == "message_delta"
    assert a2.data["text"] == "A2"
    # Later same-identity tails stay batched until ``done`` (5th get).
    assert q.get_calls == 5

    r2 = next(gen)
    assert r2.kind == "thinking"
    assert r2.data["text"] == "R2"
    assert r2.data.get("delta") is True
    assert q.get_calls == 5

    extras, (streamed, got) = _finish_gen(gen)
    assert extras == []
    assert streamed == "A1A2"
    assert got.meta["streamed_reasoning"] == "R1R2"
    assert q.get_calls == 5


def test_drain_first_frame_answer_then_tool_hint_keeps_answer_before_prep(monkeypatch):
    """First-frame answer yields before the tool_hint get; prep follows it."""
    monkeypatch.setattr(time, "monotonic", lambda: 1000.0)
    q = _CountingGetQueue()
    resp = type("R", (), {"meta": {}})()
    q.put(("delta", {"text": "Hello", "stream_id": "a1", "channel": "answer"}))
    q.put(("tool_hint", "read_file"))
    q.put(("done", resp))

    gen = drain_stream_queue(q)
    first = next(gen)
    assert first.kind == "message_delta"
    assert first.data["text"] == "Hello"
    # Yielded on the first get — not because tool_hint flushed a pending batch.
    assert q.get_calls == 1

    prep = next(gen)
    assert prep.kind == "tool_prep"
    assert prep.data["name"] == "read_file"
    assert q.get_calls == 2

    extras, (streamed, got) = _finish_gen(gen)
    assert extras == []
    assert streamed == "Hello"
    assert got is resp
    assert q.get_calls == 3


def test_drain_new_stream_id_after_tool_first_frames(monkeypatch):
    """Post-tool narration on a new stream_id gets its own immediate first frame."""
    monkeypatch.setattr(time, "monotonic", lambda: 1000.0)
    q = _CountingGetQueue()
    resp = type("R", (), {"meta": {}})()
    q.put(("delta", {"text": "Hello", "stream_id": "a1", "channel": "answer"}))
    q.put(("tool_hint", "read_file"))
    q.put(("delta", {"text": "After", "stream_id": "a2", "channel": "answer"}))
    q.put(("delta", {"text": " tool", "stream_id": "a2", "channel": "answer"}))
    q.put(("done", resp))

    gen = drain_stream_queue(q)
    first = next(gen)
    assert first.kind == "message_delta"
    assert first.data["text"] == "Hello"
    assert first.data.get("stream_id") == "a1"
    assert q.get_calls == 1

    prep = next(gen)
    assert prep.kind == "tool_prep"
    assert q.get_calls == 2

    after = next(gen)
    assert after.kind == "message_delta"
    assert after.data["text"] == "After"
    assert after.data.get("stream_id") == "a2"
    # New identity first-frames on its own get — not held until done.
    assert q.get_calls == 3

    tail = next(gen)
    assert tail.kind == "message_delta"
    assert tail.data["text"] == " tool"
    assert tail.data.get("stream_id") == "a2"
    # Later same-identity a2 stays batched until the terminal get.
    assert q.get_calls == 5

    extras, (streamed, got) = _finish_gen(gen)
    assert extras == []
    assert streamed == "HelloAfter tool"
    assert got is resp


def test_drain_dual_output_identities_each_first_frame(monkeypatch):
    """Two answer stream_ids each get an immediate first frame."""
    monkeypatch.setattr(time, "monotonic", lambda: 1000.0)
    q = _CountingGetQueue()
    resp = type("R", (), {"meta": {}})()
    q.put((
        "delta",
        {"text": "One", "stream_id": "out_1", "channel": "answer", "output_index": 0},
    ))
    q.put((
        "delta",
        {"text": "Two", "stream_id": "out_2", "channel": "answer", "output_index": 1},
    ))
    q.put((
        "delta",
        {"text": " more", "stream_id": "out_1", "channel": "answer", "output_index": 0},
    ))
    q.put(("done", resp))

    gen = drain_stream_queue(q)
    one = next(gen)
    assert one.kind == "message_delta"
    assert one.data["text"] == "One"
    assert one.data.get("stream_id") == "out_1"
    assert one.data.get("output_index") == 0
    assert q.get_calls == 1

    two = next(gen)
    assert two.kind == "message_delta"
    assert two.data["text"] == "Two"
    assert two.data.get("stream_id") == "out_2"
    assert two.data.get("output_index") == 1
    assert q.get_calls == 2

    more = next(gen)
    assert more.kind == "message_delta"
    assert more.data["text"] == " more"
    assert more.data.get("stream_id") == "out_1"
    # Later same-identity out_1 stays batched until done.
    assert q.get_calls == 4

    extras, (streamed, got) = _finish_gen(gen)
    assert extras == []
    assert streamed == "OneTwo more"
    assert got is resp
