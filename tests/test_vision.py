"""Vision sidecar wiring: image -> transcription -> prepended to driver context.
Offline-safe with a fake sidecar; the live VLM call is exercised separately.

Swarm execution is stubbed (execute_intent monkeypatched) so these tests verify
ONLY the vision-transcription wiring deterministically -- they must never spawn a
real in-process Puppetmaster worker (that blocks in _wait_for_worker on the demo
adapter and hangs the suite). The real swarm path is covered by the E2E tests.
"""
import tempfile
from harness.config import HarnessConfig
from harness.session import Session
from harness.vision import VisionResult


class _FakeSidecar:
    name = "fake-vlm"
    def transcribe(self, path):
        return VisionResult(text="SCREENSHOT shows: AUTH_TOKEN field and a verify(jwt) function.",
                            tokens_out=12, model=self.name)


def _stub_execute_intent(monkeypatch):
    """Replace harness.session.execute_intent with a fake that returns a
    deterministic BridgeResult instead of driving real Puppetmaster."""
    from pmharness.bridge import BridgeResult
    import harness.session as sess

    def fake_execute_intent(intent, *, state_dir=None, worker_mode="subprocess"):
        return BridgeResult(
            job_id="job_fake", status="done", mode="analyze",
            num_artifacts=1, artifact_types=["finding"],
            summary="stub swarm result",
            artifacts=[{"type": "finding", "headline": "stub finding"}],
            adapter="demo",
        )
    monkeypatch.setattr(sess, "execute_intent", fake_execute_intent)


def test_session_transcribes_and_prepends(monkeypatch):
    # patch transcribe_images to use the fake sidecar
    import harness.vision as v
    monkeypatch.setattr(v, "transcribe_images",
                        lambda paths, sidecar=None: [_FakeSidecar().transcribe(p) for p in paths])
    # stub swarm execution so we never spawn a real Puppetmaster worker
    _stub_execute_intent(monkeypatch)

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


def test_no_images_no_vision_event(monkeypatch):
    _stub_execute_intent(monkeypatch)
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp(prefix="vh-"))
    s = Session(cfg)
    events = list(s.run("What is JSON?"))
    assert not any(e.kind == "vision" for e in events)


def test_all_transcriptions_failed_stops_loudly(monkeypatch):
    """Regression: if images were attached but EVERY transcription errors, the
    driver (text-only) must not silently answer as though no image was sent --
    that is a wrong answer dressed as a normal turn. The run must fail loudly and
    never reach the drive/swarm loop."""
    import harness.vision as v
    monkeypatch.setattr(v, "transcribe_images",
                        lambda paths, sidecar=None: [VisionResult(text="", error="vlm unavailable", model="fake") for _ in paths])
    # If we regressed and proceeded, this stub would let the swarm "succeed" --
    # so its absence from the event stream is what proves we bailed early.
    _stub_execute_intent(monkeypatch)

    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp(prefix="vh-"))
    s = Session(cfg)
    events = list(s.run("What secret is in this screenshot?", images=["/fake/a.png"]))
    kinds = [e.kind for e in events]

    assert "executing" not in kinds, "must not drive/swarm when all images failed"
    finals = [e for e in events if e.kind == "final"]
    assert finals and finals[-1].data.get("action") == "error"
    # the per-image error was still surfaced
    assert any(e.kind == "vision" and e.data.get("error") for e in events)
