"""Prompt queue persistence across backend restart.

The server-side _prompt_queue is mirrored to {state_dir}/prompt_queue.json so a
fresh ConversationalSession over the SAME state_dir reloads queued prompts in
order. pop/remove/clear are reflected after reload, and a corrupt file yields an
empty queue rather than a crash. No model, no network.
"""
import json
import os
import tempfile

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession


def _session(state_dir):
    return ConversationalSession(HarnessConfig(state_dir=state_dir))


def test_enqueue_survives_restart_in_order():
    d = tempfile.mkdtemp()
    s = _session(d)
    s.enqueue_prompt("first")
    s.enqueue_prompt("second", images=["/tmp/a.png", "/tmp/b.png"])

    s2 = _session(d)
    items = s2.list_prompts()
    assert [i["text"] for i in items] == ["first", "second"]
    assert items[0]["images"] == []
    assert items[1]["images"] == ["/tmp/a.png", "/tmp/b.png"]


def test_pop_reflected_after_reload():
    d = tempfile.mkdtemp()
    s = _session(d)
    s.enqueue_prompt("one")
    s.enqueue_prompt("two")
    popped = s._pop_next_prompt()
    assert popped["text"] == "one"

    s2 = _session(d)
    assert [i["text"] for i in s2.list_prompts()] == ["two"]


def test_remove_reflected_after_reload():
    d = tempfile.mkdtemp()
    s = _session(d)
    a = s.enqueue_prompt("keep")
    b = s.enqueue_prompt("drop")
    assert s.remove_prompt(b["id"]) is True

    s2 = _session(d)
    texts = [i["text"] for i in s2.list_prompts()]
    assert texts == ["keep"]
    assert a["text"] == "keep"


def test_clear_reflected_after_reload():
    d = tempfile.mkdtemp()
    s = _session(d)
    s.enqueue_prompt("a")
    s.enqueue_prompt("b")
    assert s.clear_prompts() == 2

    s2 = _session(d)
    assert s2.list_prompts() == []


def test_corrupt_file_yields_empty_queue():
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "prompt_queue.json"), "w", encoding="utf-8") as f:
        f.write("{ this is not valid json ]]")

    s = _session(d)
    assert s.list_prompts() == []
    # queue still usable after tolerating the corrupt file
    s.enqueue_prompt("fresh")
    assert [i["text"] for i in s.list_prompts()] == ["fresh"]


def test_non_dict_items_skipped_on_load():
    d = tempfile.mkdtemp()
    payload = {"queue": [{"id": "x", "text": "good", "images": []},
                         "not-a-dict",
                         {"id": "y"}]}  # missing text key
    with open(os.path.join(d, "prompt_queue.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f)

    s = _session(d)
    assert [i["text"] for i in s.list_prompts()] == ["good"]
