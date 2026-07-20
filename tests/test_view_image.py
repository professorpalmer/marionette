import json
import tempfile
import os
import subprocess
import pytest
from dataclasses import dataclass
from harness.pilot import build_tools_schema
from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.vision import VisionResult, default_sidecar, GeminiVisionSidecar, OpenRouterVisionSidecar
import harness.vision

_MIN_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
    b"\x00\x00\x00\x1f\x15c4\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
    b"\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


@dataclass
class _ViewAct:
    path: str
    kind: str = "view_image"


def test_view_image_schema():
    schemas_normal = build_tools_schema(no_delegation=False)
    normal_names = [s["function"]["name"] for s in schemas_normal]
    assert "view_image" in normal_names
    
    schemas_worker = build_tools_schema(no_delegation=True)
    worker_names = [s["function"]["name"] for s in schemas_worker]
    assert "view_image" in worker_names

def test_view_image_execution(monkeypatch):
    canned_text = "This is a canned description of a 1x1 image."
    def mock_transcribe_images(paths, sidecar=None):
        return [VisionResult(text=canned_text, model="mock-vlm")]
    monkeypatch.setattr(harness.vision, "transcribe_images", mock_transcribe_images)

    with tempfile.TemporaryDirectory() as tmpdir:
        png_path = os.path.join(tmpdir, "test_image.png")
        with open(png_path, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")

        cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp(), repo=tmpdir)
        s = ConversationalSession(cfg)
        
        class _FakeImagePilot:
            name = "fake-image-pilot"
            def __init__(self):
                self.calls = 0
            def complete(self, task_prompt, *, system=None):
                from pmharness.drivers.openai_compat import DriverResponse
                return DriverResponse(text="")
            def chat(self, messages, *, tools=None, system=None):
                from pmharness.drivers.openai_compat import DriverResponse
                self.calls += 1
                if self.calls == 1:
                    tool_calls = [
                        {
                            "id": "tc_view_1",
                            "type": "function",
                            "function": {
                                "name": "view_image",
                                "arguments": json.dumps({"path": "test_image.png"})
                            }
                        }
                    ]
                    return DriverResponse(
                        text="",
                        tokens_out=15,
                        latency_ms=1.0,
                        meta={
                            "tool_calls": tool_calls,
                            "reasoning": "Checking image.",
                            "finish_reason": "tool_calls"
                        }
                    )
                else:
                    return DriverResponse(
                        text="Verified description.",
                        tokens_out=20,
                        latency_ms=1.0,
                        meta={
                            "tool_calls": [],
                            "reasoning": "Answer.",
                            "finish_reason": "stop"
                        }
                    )
        
        s.pilot = _FakeImagePilot()
        events = list(s.send("Look at the test_image.png image."))
        kinds = [e.kind for e in events]
        assert "action_start" in kinds
        assert "action_result" in kinds
        
        tool_msgs = [m for m in s._history if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        assert "test_image.png" in tool_msgs[0]["content"]
        assert canned_text in tool_msgs[0]["content"]

def test_view_image_non_image(monkeypatch):
    def mock_transcribe_images(paths, sidecar=None):
        return [VisionResult(text="should not be called", model="mock-vlm")]
    monkeypatch.setattr(harness.vision, "transcribe_images", mock_transcribe_images)

    with tempfile.TemporaryDirectory() as tmpdir:
        txt_path = os.path.join(tmpdir, "test.txt")
        with open(txt_path, "w") as f:
            f.write("hello")

        cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp(), repo=tmpdir)
        s = ConversationalSession(cfg)
        
        class _FakeImagePilot:
            name = "fake-image-pilot"
            def __init__(self):
                self.calls = 0
            def complete(self, task_prompt, *, system=None):
                from pmharness.drivers.openai_compat import DriverResponse
                return DriverResponse(text="")
            def chat(self, messages, *, tools=None, system=None):
                from pmharness.drivers.openai_compat import DriverResponse
                self.calls += 1
                if self.calls == 1:
                    tool_calls = [
                        {
                            "id": "tc_view_invalid_1",
                            "type": "function",
                            "function": {
                                "name": "view_image",
                                "arguments": json.dumps({"path": "test.txt"})
                            }
                        }
                    ]
                    return DriverResponse(
                        text="",
                        tokens_out=15,
                        latency_ms=1.0,
                        meta={
                            "tool_calls": tool_calls,
                            "reasoning": "Checking non-image.",
                            "finish_reason": "tool_calls"
                        }
                    )
                else:
                    return DriverResponse(
                        text="Done.",
                        tokens_out=20,
                        latency_ms=1.0,
                        meta={
                            "tool_calls": [],
                            "reasoning": "Answer.",
                            "finish_reason": "stop"
                        }
                    )

        s.pilot = _FakeImagePilot()
        events = list(s.send("Look at test.txt."))
        tool_msgs = [m for m in s._history if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        assert "not an image file or not found" in tool_msgs[0]["content"]

def test_view_image_confinement(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp(), repo=tmpdir)
        s = ConversationalSession(cfg)
        
        class _FakeImagePilot:
            name = "fake-image-pilot"
            def __init__(self):
                self.calls = 0
            def complete(self, task_prompt, *, system=None):
                from pmharness.drivers.openai_compat import DriverResponse
                return DriverResponse(text="")
            def chat(self, messages, *, tools=None, system=None):
                from pmharness.drivers.openai_compat import DriverResponse
                self.calls += 1
                if self.calls == 1:
                    tool_calls = [
                        {
                            "id": "tc_view_confinement_1",
                            "type": "function",
                            "function": {
                                "name": "view_image",
                                "arguments": json.dumps({"path": "../outside_image.png"})
                            }
                        }
                    ]
                    return DriverResponse(
                        text="",
                        tokens_out=15,
                        latency_ms=1.0,
                        meta={
                            "tool_calls": tool_calls,
                            "reasoning": "Checking traversal.",
                            "finish_reason": "tool_calls"
                        }
                    )
                else:
                    return DriverResponse(
                        text="Done.",
                        tokens_out=20,
                        latency_ms=1.0,
                        meta={
                            "tool_calls": [],
                            "reasoning": "Answer.",
                            "finish_reason": "stop"
                        }
                    )

        s.pilot = _FakeImagePilot()
        events = list(s.send("Look at outside image."))
        tool_msgs = [m for m in s._history if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        assert "Path traversal attempt rejected" in tool_msgs[0]["content"]

def test_view_image_rejects_path_outside_read_allowed_roots(monkeypatch):
    def mock_transcribe_images(paths, sidecar=None):
        return [VisionResult(text="should not run", model="mock-vlm")]
    monkeypatch.setattr(harness.vision, "transcribe_images", mock_transcribe_images)

    with tempfile.TemporaryDirectory() as repo:
        cfg = HarnessConfig(
            driver="stub-oracle-v2",
            state_dir=tempfile.mkdtemp(),
            repo=os.path.realpath(repo),
        )
        session = ConversationalSession(cfg)
        bad = session._do_view_image(_ViewAct(path="/etc/passwd"))
        assert bad[0] is False and bad[1] == "path_traversal"


def test_nested_workspace_view_image_allows_git_toplevel_parent(monkeypatch):
    """Nested workspace may view_image under git toplevel; escape paths stay blocked."""
    canned = "parent readme screenshot"
    monkeypatch.setattr(
        harness.vision,
        "transcribe_images",
        lambda paths, sidecar=None: [VisionResult(text=canned, model="mock-vlm")],
    )

    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as state:
        root = os.path.realpath(tmp)
        subprocess.run(
            ["git", "init"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
        nested = os.path.join(root, "Ashita", "addons", "kotoba")
        os.makedirs(nested)
        parent_png = os.path.join(root, "screenshot.png")
        with open(parent_png, "wb") as f:
            f.write(_MIN_PNG)

        cfg = HarnessConfig(
            repo=os.path.realpath(nested),
            swarm_adapter="demo",
            state_dir=os.path.realpath(state),
        )
        session = ConversationalSession(cfg)

        ok, status, val = session._do_view_image(_ViewAct(path=parent_png))
        assert ok, f"parent image under git toplevel should be viewable, got {status}: {val}"
        assert canned in val

        outside = os.path.join(os.path.dirname(root), "escape-outside.png")
        bad = session._do_view_image(_ViewAct(path=outside))
        assert bad[0] is False and bad[1] == "path_traversal"

def test_vision_default_sidecar_fallback(monkeypatch):
    from harness.vision import NullVisionSidecar
    # Clear every provider/VLM key so the fallback chain is deterministic
    # regardless of ambient environment. Also drop in-memory credential pools
    # so earlier OAuth/API-key tests cannot satisfy provider_vision_sidecar().
    try:
        from harness.credential_pool import clear_pools_for_tests
        clear_pools_for_tests()
    except Exception:
        pass
    for ev in ("HARNESS_VLM_REACH", "HARNESS_VLM_MODEL", "OPENROUTER_API_KEY",
               "ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN", "OPENAI_API_KEY",
               "GEMINI_API_KEY", "GOOGLE_API_KEY", "DEEPSEEK_API_KEY",
               "GLM_API_KEY", "ZAI_API_KEY", "Z_AI_API_KEY", "MINIMAX_API_KEY",
               "XAI_API_KEY", "XAI_OAUTH_TOKEN", "NVIDIA_API_KEY",
               "OPENAI_CODEX_TOKEN", "NOUS_API_KEY"):
        monkeypatch.delenv(ev, raising=False)

    monkeypatch.setenv("HARNESS_VLM_REACH", "openrouter")
    monkeypatch.setenv("GEMINI_API_KEY", "some_gemini_key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "some_openrouter_key")
    sc = default_sidecar()
    assert isinstance(sc, OpenRouterVisionSidecar)

    monkeypatch.delenv("HARNESS_VLM_REACH", raising=False)
    sc = default_sidecar()
    assert isinstance(sc, GeminiVisionSidecar)

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    sc = default_sidecar()
    assert isinstance(sc, OpenRouterVisionSidecar)

    # No dedicated VLM key and no other provider key -> null sidecar.
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    sc = default_sidecar()
    assert isinstance(sc, NullVisionSidecar)
