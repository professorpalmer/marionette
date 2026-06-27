from __future__ import annotations

"""Vision sidecar: decouples image input from the driver.

The research found the only vision-capable open DRIVER (Kimi) is also the
weakest driver -- so the harness must NOT require the driver to have vision.
Instead a cheap VLM sidecar transcribes an image into a TEXT description once;
that text is prepended to the driver's context as durable signal. Any text-only
driver (glm-5.2, deepseek, qwen) then "sees" the image through the transcription.

This matches the kernel philosophy: the harness owns vision as a preprocessing
capability (like CodeGraph injection); the driver only ever reasons over text.
The image is processed ONCE, never re-sent through every driver call.
"""

import base64
import json
import os
import time
import urllib.request
import urllib.error
from dataclasses import dataclass
from typing import Optional


_VLM_PROMPT = (
    "Transcribe and describe this image for a text-only coding agent. If it is a "
    "screenshot, UI, diagram, or document, capture the visible text verbatim and "
    "describe the layout/structure. Be precise and complete; the agent cannot see "
    "the image, only your text. Do not speculate beyond what is visible."
)


@dataclass
class VisionResult:
    text: str
    tokens_out: int = 0
    latency_ms: float = 0.0
    model: str = ""
    error: Optional[str] = None


def _media_type(path: str) -> str:
    p = path.lower()
    if p.endswith(".png"): return "image/png"
    if p.endswith((".jpg", ".jpeg")): return "image/jpeg"
    if p.endswith(".webp"): return "image/webp"
    if p.endswith(".gif"): return "image/gif"
    return "image/png"


class GeminiVisionSidecar:
    """VLM transcription via Gemini's OpenAI-compatible endpoint (vision-capable).
    A stand-in for an open VLM sidecar (GLM-OCR / Kimi-VL / Qwen-VL) -- same
    contract: image path -> text. Swap base_url/model/key to use an open VLM."""

    def __init__(self, *, model: str = "gemini-3.1-flash-lite-preview",
                 base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai",
                 api_key_env: str = "GEMINI_API_KEY", timeout: int = 60) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.timeout = timeout
        self.name = f"vlm:{model}"

    def _key(self) -> str:
        k = os.environ.get(self.api_key_env, "").strip()
        if not k:
            raise RuntimeError(f"missing VLM key in {self.api_key_env}")
        return k

    def transcribe(self, image_path: str) -> VisionResult:
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        data_url = f"data:{_media_type(image_path)};base64,{b64}"
        body = {
            "model": self.model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": _VLM_PROMPT},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }],
            "max_tokens": 800,
        }
        try:
            _auth = self._key()
        except Exception as e:
            return VisionResult("", error=repr(e), model=self.name)
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {_auth}"},
            method="POST")
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = json.load(r)
        except urllib.error.HTTPError as e:
            return VisionResult("", error=f"HTTP {e.code}: {e.read().decode('utf-8','replace')[:300]}",
                                latency_ms=(time.time()-t0)*1000, model=self.name)
        except Exception as e:
            return VisionResult("", error=repr(e), latency_ms=(time.time()-t0)*1000, model=self.name)
        try:
            text = raw["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            return VisionResult("", error=f"bad VLM response: {str(raw)[:200]}",
                                latency_ms=(time.time()-t0)*1000, model=self.name)
        usage = raw.get("usage", {}) or {}
        return VisionResult(text=text, tokens_out=int(usage.get("completion_tokens", 0) or 0),
                            latency_ms=(time.time()-t0)*1000, model=self.name)


def transcribe_images(paths: list, sidecar=None) -> list:
    """Transcribe a list of image paths into VisionResults. Picks the sidecar
    from env (open VLM via OpenRouter, or Gemini) if none provided."""
    sc = sidecar or default_sidecar()
    return [sc.transcribe(p) for p in paths]


class OpenRouterVisionSidecar:
    """VLM transcription via an OPEN vision model on OpenRouter -- so vision is
    open-weights too, closing the last frontier-model dependency. Default model
    is qwen3-vl-30b-a3b (Apache-2.0, pairs with the qwen3-coder driver). Same
    image->text contract as the Gemini sidecar; swap model via HARNESS_VLM_MODEL.
    """

    def __init__(self, *, model: str = "qwen/qwen3-vl-30b-a3b-instruct",
                 base_url: str = "https://openrouter.ai/api/v1",
                 api_key_env: str = "OPENROUTER_API_KEY", timeout: int = 60) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.timeout = timeout
        self.name = f"vlm:{model}"

    def _key(self) -> str:
        k = os.environ.get(self.api_key_env, "").strip()
        if not k:
            raise RuntimeError(f"missing VLM key in {self.api_key_env}")
        return k

    def transcribe(self, image_path: str) -> VisionResult:
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        data_url = f"data:{_media_type(image_path)};base64,{b64}"
        body = {
            "model": self.model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": _VLM_PROMPT},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }],
            "max_tokens": 800,
        }
        try:
            _auth = self._key()
        except Exception as e:
            return VisionResult("", error=repr(e), model=self.name)
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {_auth}"},
            method="POST")
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = json.load(r)
        except urllib.error.HTTPError as e:
            return VisionResult("", error=f"HTTP {e.code}: {e.read().decode('utf-8','replace')[:300]}",
                                latency_ms=(time.time()-t0)*1000, model=self.name)
        except Exception as e:
            return VisionResult("", error=repr(e), latency_ms=(time.time()-t0)*1000, model=self.name)
        try:
            text = raw["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            return VisionResult("", error=f"bad VLM response: {str(raw)[:200]}",
                                latency_ms=(time.time()-t0)*1000, model=self.name)
        usage = raw.get("usage", {}) or {}
        return VisionResult(text=text, tokens_out=int(usage.get("completion_tokens", 0) or 0),
                            latency_ms=(time.time()-t0)*1000, model=self.name)


def default_sidecar():
    """Pick the vision sidecar from env. HARNESS_VLM_REACH=openrouter -> open VLM
    (default model qwen3-vl, overridable via HARNESS_VLM_MODEL); else Gemini.
    If no GEMINI_API_KEY is found but OPENROUTER_API_KEY is present, fall back to OpenRouter."""
    import os as _os
    reach = _os.environ.get("HARNESS_VLM_REACH", "").lower()
    gemini_key = _os.environ.get("GEMINI_API_KEY", "").strip()
    or_key = _os.environ.get("OPENROUTER_API_KEY", "").strip()

    if reach == "openrouter" or (not gemini_key and or_key):
        model = _os.environ.get("HARNESS_VLM_MODEL", "qwen/qwen3-vl-30b-a3b-instruct")
        return OpenRouterVisionSidecar(model=model)
    return GeminiVisionSidecar()
