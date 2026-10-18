"""A streamed block that loops on itself is cut at a fixed size."""

from __future__ import annotations

import queue
from types import SimpleNamespace

import pytest

from harness.repetition_guard import (
    CHECK_EVERY,
    MIN_CHARS,
    RepetitionWatch,
    is_runaway_repetition,
)
from harness.send_loop_phases import (
    STREAM_REPETITION_MESSAGE,
    StreamRepetitionError,
    run_stream,
)

PHRASE = "I will now check the file again to be sure. "


def _loop(n: int, unit: str = PHRASE) -> str:
    return (unit * (n // len(unit) + 1))[:n]


def _varied(n: int) -> str:
    rows = []
    i = 0
    while sum(len(r) for r in rows) < n:
        rows.append("Row %05d: value %d is %s\n" % (i, i * 7919 % 10007, "odd" if i % 2 else "even"))
        i += 1
    return "".join(rows)[:n]


def test_trips_at_exactly_min_chars_and_latches():
    assert not is_runaway_repetition(_loop(MIN_CHARS - 1))
    assert is_runaway_repetition(_loop(MIN_CHARS))
    text = _loop(MIN_CHARS)
    watch = RepetitionWatch()
    assert watch.feed(text[:-1]) is False
    assert watch.feed(text[-1]) is True
    assert watch.feed("totally new text") is True


@pytest.mark.parametrize("width", [1, 7, 32, 4096, 100_000])
def test_same_result_for_every_chunk_width(width):
    text = _varied(5_000) + _loop(30_000)
    watch = RepetitionWatch()
    tripped_at = None
    for i in range(0, len(text), width):
        if watch.feed(text[i:i + width]):
            tripped_at = i
            break
    assert tripped_at is not None
    # The trip check position does not depend on the width.
    pos = MIN_CHARS
    while not is_runaway_repetition(text[:pos]):
        pos += CHECK_EVERY
    assert tripped_at < pos <= tripped_at + width


def test_long_periods_and_varied_lines_in_a_loop_trip():
    paragraph = "".join("line %d of a long step that the model repeats\n" % i for i in range(40))
    assert len(paragraph) * 5 < 16_000
    assert is_runaway_repetition(_loop(40_000, paragraph))


def test_loop_after_a_long_varied_prefix_trips():
    watch = RepetitionWatch()
    assert watch.feed(_varied(60_000)) is False
    tripped = False
    for i in range(0, 20_000, 50):
        tripped = watch.feed(_loop(20_000)[i:i + 50])
        if tripped:
            break
    assert tripped


def test_numbered_distinct_rows_are_not_a_loop():
    watch = RepetitionWatch()
    text = _varied(80_000)
    for i in range(0, len(text), 64):
        assert watch.feed(text[i:i + 64]) is False


def test_identical_lines_with_one_varied_field_are_not_a_loop():
    text = "".join("| step %d | ok | 0.00 ms |\n" % i for i in range(3_000))
    assert len(text) > MIN_CHARS
    assert not is_runaway_repetition(text)


def _session(chat_stream, cancel=None):
    history = [{"role": "system"}, {"role": "user", "content": "hi"}]
    return SimpleNamespace(
        pilot=SimpleNamespace(chat_stream=chat_stream, supports_streaming=True),
        _history=history,
        _cancel=cancel,
        _elide_stale_reads=lambda msgs: msgs,
        _messages_for_provider=lambda: history[1:],
    )


def test_run_stream_cuts_a_looped_reasoning_block():
    q: queue.Queue = queue.Queue()
    calls = {"after_trip": 0, "cancelled_seen": False}

    def chat_stream(messages, *, on_delta, on_reasoning_delta, is_cancelled=None, **_k):
        on_delta("Let me think. ")
        for i in range(0, 40_000, 100):
            if is_cancelled():
                calls["cancelled_seen"] = True
                break
            on_reasoning_delta(_loop(40_000)[i:i + 100])
        return SimpleNamespace(text="Let me think. ", meta={})

    run_stream(_session(chat_stream), q, [], "sys")
    items = []
    while not q.empty():
        items.append(q.get_nowait())
    kinds = [k for k, _ in items]
    assert kinds[0] == "delta"
    assert "done" not in kinds
    assert kinds[-1] == "error"
    assert isinstance(items[-1][1], StreamRepetitionError)
    assert STREAM_REPETITION_MESSAGE in str(items[-1][1])
    reasoning = "".join(v for k, v in items if k == "reasoning")
    assert len(reasoning) < MIN_CHARS
    assert calls["cancelled_seen"] is True


def test_run_stream_leaves_a_normal_long_answer_alone():
    q: queue.Queue = queue.Queue()
    text = _varied(50_000)

    def chat_stream(messages, *, on_delta, **_k):
        for i in range(0, len(text), 80):
            on_delta(text[i:i + 80])
        return SimpleNamespace(text=text)

    run_stream(_session(chat_stream), q, [], "sys")
    kinds = []
    while not q.empty():
        kinds.append(q.get_nowait()[0])
    assert kinds[-1] == "done" and "error" not in kinds

