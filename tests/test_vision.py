"""Vision sidecar wiring: image -> transcription -> prepended to driver context.
Offline-safe with a fake sidecar; the live VLM call is exercised separately."""
import tempfile
from harness.config import HarnessConfig
from harness.session import Session
from harness.vision import VisionResult


class _FakeSidecar:
    name = "fake-vlm"
    def transcribe(self, path):
        return VisionResult(text="SCREENSHOT shows: AUTH_TOKEN field and a verify(jwt) function.",
                            tokens_out=12, model=self.name)


def test_session_transcribes_and_prepends(monkeypatch):
    # patch transcribe_images to use the fake sidecar
    import harness.vision as v
    monkeypatch.setattr(v, "transcribe_images",
                        lambda paths, sidecar=None: [_FakeSidecar().transcribe(p) for p in paths])

    cfg = HarnessConfig(driver="stub-oracle-v2", reach="openrouter",
                        budget=3, state_dir=tempfile.mkdtemp(prefix="vh-"))
    s = Session(cfg)
    events = list(s.run("What secret is in this screenshot?", images=["/fake/path.png"]))
    kinds = [e.kind for e in events]
    # a vision event was emitted
    assert "vision" in kinds
    vis = [e for e in events if e.kind == "vision" and "chars" in e.data]
    assert vis and vis[0].data["chars"] > 0
    # the loop still terminated
    assert kinds[-1] == "final"


def test_no_images_no_vision_event():
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp(prefix="vh-"))
    s = Session(cfg)
    events = list(s.run("What is JSON?"))
    assert not any(e.kind == "vision" for e in events)
