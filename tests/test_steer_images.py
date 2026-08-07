"""Steering with an attached image must reach the model without dropping pixels.

Mid-turn steers inject as TEXT, so text-only pilots get sidecar transcription.
Vision-capable pilots must NEVER use a weaker sidecar VLM — they queue a
follow-up turn with native multimodal images instead.

Hermetic: monkeypatches vision helpers; no real model/vision call.
"""
import tempfile

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession


class _FakeResult:
    def __init__(self, text="", error=None):
        self.text = text
        self.error = error


def _session():
    return ConversationalSession(HarnessConfig(state_dir=tempfile.mkdtemp()))


def test_steer_with_image_transcribes_into_text(monkeypatch):
    s = _session()
    monkeypatch.setattr("harness.vision.session_supports_native_images", lambda _s: False)
    monkeypatch.setattr("harness.vision.transcribe_images",
                        lambda paths, sidecar=None: [_FakeResult(text="a red login button")])
    s.steer_with_images("look at this", ["/tmp/shot.png"])
    drained = s.drain_steer()
    assert len(drained) == 1
    assert "look at this" in drained[0]
    assert "a red login button" in drained[0]  # the transcription, not a bare id


def test_steer_image_error_is_surfaced_not_dropped(monkeypatch):
    s = _session()
    monkeypatch.setattr("harness.vision.session_supports_native_images", lambda _s: False)
    monkeypatch.setattr("harness.vision.transcribe_images",
                        lambda paths, sidecar=None: [_FakeResult(error="unreadable")])
    s.steer_with_images("check", ["/tmp/x.png"])
    drained = s.drain_steer()
    assert drained and "could not be read" in drained[0]


def test_text_only_steer_still_works(monkeypatch):
    s = _session()
    s.steer_with_images("just text", [])
    assert s.drain_steer() == ["just text"]


def test_steer_native_vision_queues_prompt_skips_sidecar(monkeypatch):
    """gpt-5.6-luna-class pilots must not get a weaker sidecar paraphrase."""
    s = _session()
    called = {"transcribe": 0}
    queued = {}

    def _boom(*_a, **_k):
        called["transcribe"] += 1
        raise AssertionError("sidecar must not run for native vision steer")

    def _queue(text, images=None, **_k):
        queued["text"] = text
        queued["images"] = list(images or [])
        return {"id": "q1", "text": text}

    monkeypatch.setattr("harness.vision.session_supports_native_images", lambda _s: True)
    monkeypatch.setattr("harness.vision.transcribe_images", _boom)
    s.enqueue_prompt = _queue
    s.steer_with_images("look at this", ["/tmp/shot.png"])
    assert called["transcribe"] == 0
    assert queued.get("text") == "look at this"
    assert queued.get("images") == ["/tmp/shot.png"]
    nudge = s.drain_steer()
    assert nudge and "native vision" in nudge[0].lower()
