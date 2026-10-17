"""Steering with an attached image must reach the model without dropping pixels.

Text-only pilots get sidecar transcription in the steer text. Vision-capable
pilots must NEVER use a weaker sidecar VLM. They steer mid-turn with native
image parts.

Hermetic: monkeypatches vision helpers; no real model/vision call.
"""
import base64
import hashlib
from pathlib import Path

import pytest

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.input_receipts import session_input_store


class _FakeResult:
    def __init__(self, text="", error=None):
        self.text = text
        self.error = error


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEElEQVR4nGNocDgARAwQCgApDgYBH5bqCgAAAABJRU5ErkJggg=="
)


@pytest.fixture
def image_session(tmp_path):
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    image = uploads / "shot.png"
    image.write_bytes(PNG)
    session = ConversationalSession(HarnessConfig(
        driver="stub-oracle-v2", state_dir=str(tmp_path / "state"),
    ))
    session._input_upload_root = str(uploads)
    return session, image


def _retained(session, original):
    rows = session.input_receipts()
    assert len(rows) == 1
    row = rows[0]
    assert row["original_text"] == original
    attachment, = row["attachments"]
    assert attachment["sha256"] == hashlib.sha256(PNG).hexdigest()
    assert session_input_store(session).attachment(attachment["ref"]) == PNG
    return row


def _assert_materialized(paths, image):
    assert len(paths) == 1
    assert paths[0] != str(image)
    assert Path(paths[0]).read_bytes() == PNG


def test_steer_with_image_transcribes_into_text(monkeypatch, image_session):
    s, image = image_session
    monkeypatch.setattr("harness.vision.session_supports_native_images", lambda _s: False)
    def transcribe(paths, sidecar=None):
        _assert_materialized(paths, image)
        return [_FakeResult(text="a red login button")]
    monkeypatch.setattr("harness.vision.transcribe_images", transcribe)
    assert s.steer_with_images("look at this", [str(image)]) == "enqueue_steer"
    receipt = _retained(s, "look at this")
    assert list(s._session_actions)[0].id == receipt["id"]
    drained = s.drain_steer()
    assert len(drained) == 1
    assert "look at this" in drained[0]
    assert "a red login button" in drained[0]  # the transcription, not a bare id


def test_steer_image_error_is_surfaced_not_dropped(monkeypatch, image_session):
    s, image = image_session
    monkeypatch.setattr("harness.vision.session_supports_native_images", lambda _s: False)
    def transcribe(paths, sidecar=None):
        _assert_materialized(paths, image)
        return [_FakeResult(error="unreadable")]
    monkeypatch.setattr("harness.vision.transcribe_images", transcribe)
    assert s.steer_with_images("check", [str(image)]) == "enqueue_steer"
    receipt = _retained(s, "check")
    assert list(s._session_actions)[0].id == receipt["id"]
    drained = s.drain_steer()
    assert drained and "could not be read" in drained[0]


def test_text_only_steer_still_works(image_session):
    s, _ = image_session
    assert s.steer_with_images("just text", []) == "enqueue_steer"
    assert s.drain_steer() == ["just text"]
    literal = "  keep me\n\t "
    assert s.steer_with_images(literal, []) == "enqueue_steer"
    action, = list(s._session_actions)
    assert action.text == literal
    events = list(s._check_and_inject_steer())
    assert any(e.kind == "steer" and e.data["text"] == literal for e in events)
    receipt = s.input_receipts()[-1]
    assert receipt["original_text"] == literal
    assert receipt["id"] == action.id
    assert receipt["status"] == "injected"
    assert s._history[-1]["content"] == literal
    assert s._history[-1]["input_id"] == receipt["id"]


def _inject(s):
    list(s._check_and_inject_steer())
    return s._history[-1]


def test_steer_native_vision_steers_with_pixels(monkeypatch, image_session):
    """A vision pilot steers mid-turn with the real pixels, not a queued turn
    and not a weaker sidecar paraphrase."""
    s, image = image_session

    def _boom(*_a, **_k):
        raise AssertionError("sidecar must not run for native vision steer")

    monkeypatch.setattr("harness.vision.session_supports_native_images", lambda _s: True)
    monkeypatch.setattr("harness.vision.transcribe_images", _boom)
    action = s.steer_with_images("look at this", [str(image)])
    assert action == "enqueue_steer"
    assert s.list_prompts() == []
    receipt = _retained(s, "look at this")
    row = _inject(s)
    assert row["role"] == "user" and row["input_id"] == receipt["id"]
    text, image_part = row["content"]
    assert text == {"type": "text", "text": "look at this"}
    assert image_part["type"] == "image_url"
    assert image_part["image_url"]["url"].startswith("data:image/png;base64,")


def test_steer_native_vision_images_only_steers_placeholder(monkeypatch, image_session):
    """Images-only on a vision pilot steers the placeholder text plus pixels."""
    s, image = image_session

    monkeypatch.setattr("harness.vision.session_supports_native_images", lambda _s: True)
    action = s.steer_with_images("", [str(image)])
    assert action == "enqueue_steer"
    assert s.list_prompts() == []
    receipt = _retained(s, "")
    _, paths = session_input_store(s).delivery_content(receipt["id"])
    _assert_materialized(paths, image)
    row = _inject(s)
    assert row["content"][0] == {"type": "text", "text": "(see attached image)"}
    assert row["content"][1]["type"] == "image_url"


def test_text_only_steer_keeps_transcribed_text(monkeypatch, image_session):
    """A text-only pilot steer stays a string: the sidecar text carries the image."""
    s, image = image_session
    monkeypatch.setattr("harness.vision.session_supports_native_images", lambda _s: False)
    monkeypatch.setattr("harness.vision.transcribe_images", lambda _p: [_FakeResult("a red square")])
    assert s.steer_with_images("look", [str(image)]) == "enqueue_steer"
    row = _inject(s)
    assert isinstance(row["content"], str) and "a red square" in row["content"]


def test_anthropic_image_steer_is_its_own_user_turn():
    """The Qwen relay on Go drops an image that shares a user turn with
    tool_result blocks. Text steers still merge, as before."""
    from pmharness.drivers.anthropic import AnthropicDriver

    d = AnthropicDriver(name="opencode-go:qwen3.8-max", model="qwen3.8-max",
                        base_url="https://opencode.ai/zen/go/v1", api_key_env="X", max_tokens=100)
    tc = [{"id": "toolu_1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]
    head = [{"role": "user", "content": "go"}, {"role": "assistant", "content": "", "tool_calls": tc},
            {"role": "tool", "tool_call_id": "toolu_1", "content": "done"}]
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    msgs = d._build_body(head + [{"role": "user", "content": [{"type": "text", "text": "look"}, image]}],
                         None, None)["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "user"]
    assert [b["type"] for b in msgs[2]["content"]] == ["tool_result"]
    assert [b["type"] for b in msgs[3]["content"]] == ["text", "image"]
    msgs = d._build_body(head + [{"role": "user", "content": "stop"}], None, None)["messages"]
    assert [b["type"] for b in msgs[2]["content"]] == ["tool_result", "text"]
