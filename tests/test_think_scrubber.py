"""StreamingThinkScrubber — Hermes lift for split-delta think tags."""
from __future__ import annotations

from pmharness.think_scrubber import StreamingThinkScrubber


def test_split_think_tags_do_not_leak():
    s = StreamingThinkScrubber()
    assert s.feed("<think>") == ""
    assert s.feed("secret plan") == ""
    assert s.feed("</think>") == ""
    assert s.feed("Visible answer") == "Visible answer"
    assert s.flush() == ""


def test_closed_pair_in_one_delta():
    s = StreamingThinkScrubber()
    assert s.feed("<think>hidden</think>Hello") == "Hello"
    assert s.flush() == ""


def test_partial_open_tag_held_across_deltas():
    s = StreamingThinkScrubber()
    assert s.feed("<thi") == ""
    assert s.feed("nk>x</think>ok") == "ok"
    assert s.flush() == ""


def test_flush_rearms_boundary_for_retry():
    """After flush, a new stream's opening <think> is a boundary again."""
    s = StreamingThinkScrubber()
    assert s.feed("partial") == "partial"
    assert s.flush() == ""
    assert s.feed("<think>") == ""
    assert s.feed("more</think>done") == "done"


def test_prose_mention_not_stripped():
    s = StreamingThinkScrubber()
    text = "Please use <think> tags here carefully."
    assert s.feed(text) == text
