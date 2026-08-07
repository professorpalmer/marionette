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

    def fake_execute_intent(intent, *, state_dir=None, worker_mode="subprocess", **_kwargs):
        # Accept cwd/repo/session_id/on_delta so per-runner workspace pinning
        # (and future kwargs) do not break this vision-only stub.
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


def test_conversational_send_all_transcriptions_failed_stops_loudly(monkeypatch):
    """Parity with Session.run: ConversationalSession.send must fail loudly when
    every sidecar transcription errors (never continue as bare text-only)."""
    import harness.vision as v
    from harness.conversation import ConversationalSession

    monkeypatch.setattr(
        v,
        "transcribe_images",
        lambda paths, sidecar=None: [
            VisionResult(text="", error="vlm unavailable", model="fake") for _ in paths
        ],
    )
    monkeypatch.setattr(v, "pilot_supports_native_images", lambda *a, **k: False)

    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp(prefix="vh-"))
    session = ConversationalSession(cfg)
    events = list(session.send("What is in this screenshot?", images=["/fake/a.png"]))
    kinds = [e.kind for e in events]

    assert "error" in kinds
    assert any(
        e.kind == "error" and "transcription" in (e.data.get("error") or "").lower()
        for e in events
    )
    assert "assistant_done" not in kinds
    assert not any(
        m.get("role") == "user" and "screenshot" in str(m.get("content") or "").lower()
        for m in session._history[1:]
    )


def test_provider_vision_sidecar_skips_codex_responses(monkeypatch):
    """openai-codex (api_mode=codex_responses) must not become a chat/completions
    vision sidecar — that endpoint shape is wrong and transcription fails."""
    from dataclasses import dataclass
    import harness.vision as v

    @dataclass
    class _FakeProvider:
        name: str
        api_mode: str
        vision_model: str
        base_url: str = "https://example.invalid"

        def key_env(self):
            return "FAKE_KEY"

    monkeypatch.setattr(
        "harness.providers.available_providers",
        lambda: [
            _FakeProvider(
                name="openai-codex",
                api_mode="codex_responses",
                vision_model="gpt-5.4",
                base_url="https://chatgpt.com/backend-api/codex",
            ),
            _FakeProvider(
                name="openai",
                api_mode="chat_completions",
                vision_model="gpt-5.6-luna",
                base_url="https://api.openai.com/v1",
            ),
        ],
    )
    sc = v.provider_vision_sidecar()
    assert sc is not None
    assert isinstance(sc, v.OpenAICompatVisionSidecar)
    assert sc.base_url == "https://api.openai.com/v1"
    assert sc.model == "gpt-5.6-luna"


def test_provider_vision_sidecar_none_when_only_codex(monkeypatch):
    from dataclasses import dataclass
    import harness.vision as v

    @dataclass
    class _FakeProvider:
        name: str
        api_mode: str
        vision_model: str
        base_url: str = "https://chatgpt.com/backend-api/codex"

        def key_env(self):
            return "OPENAI_CODEX_TOKEN"

    monkeypatch.setattr(
        "harness.providers.available_providers",
        lambda: [
            _FakeProvider(
                name="openai-codex",
                api_mode="codex_responses",
                vision_model="gpt-5.4",
            ),
        ],
    )
    assert v.provider_vision_sidecar() is None


def test_native_multimodal_user_content_builds_image_url(tmp_path):
    from harness.vision import native_multimodal_user_content

    img = tmp_path / "shot.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")
    parts = native_multimodal_user_content("look", [str(img)])
    assert parts[0] == {"type": "text", "text": "look"}
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_pilot_supports_native_images_codex_and_stub():
    from harness.providers import get_provider
    from harness.vision import pilot_supports_native_images
    from pmharness.drivers.stub import StubDriver

    codex = get_provider("openai-codex")
    assert pilot_supports_native_images(codex, model="gpt-5.6-luna") is True
    stub = StubDriver()
    assert pilot_supports_native_images(None, model="stub-oracle-v2", pilot=stub) is False


def test_conversational_send_native_multimodal_skips_sidecar(tmp_path, monkeypatch):
    """Vision-capable pilots receive pixels in history, not transcription preamble."""
    import harness.vision as v
    from harness.conversation import ConversationalSession
    from pmharness.drivers.base import DriverResponse

    img = tmp_path / "shot.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")

    monkeypatch.setattr(v, "pilot_supports_native_images", lambda *a, **k: True)
    called = {"transcribe": 0}

    def _boom(*_a, **_k):
        called["transcribe"] += 1
        raise AssertionError("sidecar must not run on native path")

    monkeypatch.setattr(v, "transcribe_images", _boom)

    class _Pilot:
        model = "gpt-5.6-luna"
        supports_streaming = False

        def chat(self, messages, *, tools=None, system=None, **_k):
            return DriverResponse(
                text='{"say":"ok","actions":[]}',
                tokens_in=1, tokens_out=1, latency_ms=1.0,
            )

        def complete(self, prompt, *, system=None, **_k):
            return self.chat([{"role": "user", "content": prompt}], system=system)

    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp(prefix="vh-"))
    session = ConversationalSession(cfg)
    session.pilot = _Pilot()
    session._build_visible_tools_schema = lambda: []
    session._maybe_compact_history = lambda *a, **k: iter(())
    session._submit_housekeeping = lambda *a, **k: None

    events = list(session.send("describe", images=[str(img)]))
    assert called["transcribe"] == 0
    assert any(e.kind == "vision" and e.data.get("status") == "native" for e in events)
    user_msgs = [m for m in session._history if m.get("role") == "user"]
    assert user_msgs
    content = user_msgs[0]["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert "cannot see the image" not in content[0]["text"]
    assert any(p.get("type") == "image_url" for p in content)


def test_anthropic_driver_maps_image_url_parts():
    from pmharness.drivers.anthropic import AnthropicDriver

    d = AnthropicDriver(name="t", model="claude-haiku-4-5", api_key_env="ANTHROPIC_API_KEY")
    body = d._build_body(
        [{
            "role": "user",
            "content": [
                {"type": "text", "text": "see"},
                {"type": "image_url", "image_url": {
                    "url": "data:image/jpeg;base64,xyz",
                }},
            ],
        }],
        tools=None,
        system=None,
    )
    blocks = body["messages"][0]["content"]
    assert blocks[0] == {"type": "text", "text": "see"}
    assert blocks[1]["type"] == "image"
    assert blocks[1]["source"]["media_type"] == "image/jpeg"
    assert blocks[1]["source"]["data"] == "xyz"
