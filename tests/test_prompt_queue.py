"""Prompt queue: a sequential 'playlist' distinct from steer.

Steer interrupts the CURRENT turn; the prompt queue schedules FUTURE turns that
each run as their own user message after the previous finishes, and can be
reordered/removed before they run. These tests exercise the pure queue ops
(enqueue/list/remove/reorder/clear) on a real ConversationalSession with a temp
state_dir -- no model, no network.
"""
import tempfile

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession


def _session():
    return ConversationalSession(HarnessConfig(state_dir=tempfile.mkdtemp()))


def test_enqueue_and_list():
    s = _session()
    a = s.enqueue_prompt("first")
    b = s.enqueue_prompt("second")
    assert a["id"] != b["id"]
    items = s.list_prompts()
    assert [i["text"] for i in items] == ["first", "second"]


def test_remove_present_and_missing():
    s = _session()
    a = s.enqueue_prompt("x")
    assert s.remove_prompt(a["id"]) is True
    assert s.list_prompts() == []
    # removing an unknown id is a no-op that reports False
    assert s.remove_prompt("does-not-exist") is False


def test_reorder_full():
    s = _session()
    a = s.enqueue_prompt("a")
    b = s.enqueue_prompt("b")
    c = s.enqueue_prompt("c")
    s.reorder_prompts([c["id"], a["id"], b["id"]])
    assert [i["text"] for i in s.list_prompts()] == ["c", "a", "b"]


def test_reorder_ignores_unknown_and_keeps_omitted_at_end():
    s = _session()
    a = s.enqueue_prompt("a")
    b = s.enqueue_prompt("b")
    c = s.enqueue_prompt("c")
    # mention only b (and a bogus id); a and c must stay, in their prior order.
    s.reorder_prompts([b["id"], "bogus"])
    texts = [i["text"] for i in s.list_prompts()]
    assert texts[0] == "b"
    assert set(texts) == {"a", "b", "c"}
    assert texts.index("a") < texts.index("c")  # omitted keep relative order


def test_clear():
    s = _session()
    s.enqueue_prompt("a")
    s.enqueue_prompt("b")
    assert s.clear_prompts() == 2
    assert s.list_prompts() == []


def test_queue_is_independent_of_steer():
    s = _session()
    s.enqueue_prompt("queued")
    s.enqueue_steer("steered")
    # A queued prompt must not appear in the steer drain and vice versa.
    assert s.drain_steer() == ["steered"]
    assert [i["text"] for i in s.list_prompts()] == ["queued"]
