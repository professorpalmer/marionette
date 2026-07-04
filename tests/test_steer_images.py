"""Steering with an attached image must transcribe it into the steer text.

Real report: steering with a screenshot resolved to just a 'screenshot id' --
the image was dropped. A steer injects as TEXT into the running turn, so it can't
carry raw image blocks; steer_with_images() runs the same vision transcription as
view_image and appends it to the steer content.

Hermetic: monkeypatches the vision transcriber; no real model/vision call.
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
    monkeypatch.setattr("harness.vision.transcribe_images",
                        lambda paths, sidecar=None: [_FakeResult(text="a red login button")])
    s.steer_with_images("look at this", ["/tmp/shot.png"])
    drained = s.drain_steer()
    assert len(drained) == 1
    assert "look at this" in drained[0]
    assert "a red login button" in drained[0]  # the transcription, not a bare id


def test_steer_image_error_is_surfaced_not_dropped(monkeypatch):
    s = _session()
    monkeypatch.setattr("harness.vision.transcribe_images",
                        lambda paths, sidecar=None: [_FakeResult(error="unreadable")])
    s.steer_with_images("check", ["/tmp/x.png"])
    drained = s.drain_steer()
    assert drained and "could not be read" in drained[0]


def test_text_only_steer_still_works(monkeypatch):
    s = _session()
    s.steer_with_images("just text", [])
    assert s.drain_steer() == ["just text"]
