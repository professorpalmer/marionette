from __future__ import annotations

"""Vision: native multimodal pixels when the pilot can see, sidecar otherwise.

When the active pilot/provider accepts images natively (OpenAI-compat,
Anthropic messages, Codex Responses, Gemini), chat history carries OpenAI-shaped
multimodal content lists (text + image_url data URLs). Drivers translate that
shape to their wire format.

Text-only pilots still use a cheap VLM sidecar that transcribes image -> text
once; that text is prepended so the driver can reason without pixels. Sidecar
resolution reuses whatever provider key the user already configured (see
default_sidecar). Codex Responses cannot serve /chat/completions, so it is
never selected as a transcription sidecar.

If images were attached and neither native delivery nor transcription can
produce usable content, the turn must fail loudly — never silently answer as
text-only.
"""

import base64
import json
import os
import time
import urllib.request
import urllib.error
from dataclasses import dataclass
from typing import Optional
from pmharness.drivers import http_pool
from pmharness.drivers.metering import report_usage


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


def _read_data_url(image_path: str) -> str:
    from .seal import read_file

    b64 = base64.b64encode(read_file(image_path)).decode()
    return f"data:{_media_type(image_path)};base64,{b64}"


def _sidecar_key(api_key_env: str, *, api_key_only: bool = False) -> str:
    """Credential pool first, then process env, as the pilot drivers do.

    A key saved only in the pool (not yet mirrored to env) still reaches the
    sidecar. ``api_key_only`` skips an Anthropic OAuth token: the sidecar
    sends ``x-api-key``, which an OAuth token cannot authorize.
    """
    try:
        from .credential_pool import resolve_entry_for_env
        entry = resolve_entry_for_env(api_key_env)
        token = (getattr(entry, "runtime_token", "") or "").strip()
        if token and api_key_only:
            from .oauth_anthropic import is_anthropic_oauth_token
            if is_anthropic_oauth_token(token):
                token = ""
        if token:
            return token
    except Exception:
        pass
    k = os.environ.get(api_key_env, "").strip()
    if not k:
        raise RuntimeError(f"missing VLM key in {api_key_env}")
    return k


class OpenAICompatVisionSidecar:
    """VLM transcription via any OpenAI-compatible /chat/completions endpoint.
    One transport covers OpenRouter, OpenAI, Gemini (openai-compat), xAI, and
    friends -- only base_url, model, and the key env differ. Contract: an image
    path in, a VisionResult (text) out."""

    def __init__(self, *, model: str, base_url: str, api_key_env: str,
                 timeout: int = 60, extra_headers: Optional[dict] = None) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.timeout = timeout
        self.extra_headers = dict(extra_headers or {})
        self.name = f"vlm:{model}"

    def _key(self) -> str:
        return _sidecar_key(self.api_key_env)

    def _headers(self, auth: str, body: dict) -> dict:
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {auth}", **self.extra_headers}
        # OpenCode Go/Zen refuse a request without x-opencode-session.
        from pmharness.drivers.prompt_cache import maybe_attach_opencode_session_header
        maybe_attach_opencode_session_header(headers, base_url=self.base_url,
                                             messages=body["messages"])
        return headers

    def transcribe(self, image_path: str) -> VisionResult:
        data_url = _read_data_url(image_path)
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
            headers=self._headers(_auth, body),
            method="POST")
        t0 = time.time()
        try:
            with http_pool.urlopen(req, timeout=self.timeout) as r:
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
        report_usage(name=self.name, model=self.model, base_url=self.base_url,
                     usage=usage, started_at=t0, purpose="vision")
        return VisionResult(text=text, tokens_out=int(usage.get("completion_tokens", 0) or 0),
                            latency_ms=(time.time()-t0)*1000, model=self.name)


class GeminiVisionSidecar(OpenAICompatVisionSidecar):
    """VLM transcription via Gemini's OpenAI-compatible endpoint (vision-capable).
    A stand-in for an open VLM sidecar (GLM-OCR / Kimi-VL / Qwen-VL) -- same
    contract: image path -> text. Swap base_url/model/key to use an open VLM."""

    def __init__(self, *, model: str = "gemini-3.1-flash-lite-preview",
                 base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai",
                 api_key_env: str = "GEMINI_API_KEY", timeout: int = 60) -> None:
        super().__init__(model=model, base_url=base_url, api_key_env=api_key_env,
                         timeout=timeout)


class OpenRouterVisionSidecar(OpenAICompatVisionSidecar):
    """VLM transcription via an OPEN vision model on OpenRouter -- so vision is
    open-weights too, closing the last frontier-model dependency. Default model
    is qwen3-vl-30b-a3b (Apache-2.0, pairs with the qwen3-coder driver). Same
    image->text contract; swap model via HARNESS_VLM_MODEL.
    """

    def __init__(self, *, model: str = "qwen/qwen3-vl-30b-a3b-instruct",
                 base_url: str = "https://openrouter.ai/api/v1",
                 api_key_env: str = "OPENROUTER_API_KEY", timeout: int = 60) -> None:
        super().__init__(model=model, base_url=base_url, api_key_env=api_key_env,
                         timeout=timeout)


class AnthropicVisionSidecar:
    """VLM transcription via Anthropic's /v1/messages API (Claude is vision-capable).
    Anthropic uses a different wire format than OpenAI-compat providers -- image
    content blocks with base64 source -- so it needs its own transport. Used when
    the user's only configured provider is Anthropic (or MiniMax's anthropic-mode
    endpoint). Same image->text contract as the OpenAI-compat sidecar."""

    def __init__(self, *, model: str, base_url: str = "https://api.anthropic.com",
                 api_key_env: str = "ANTHROPIC_API_KEY", timeout: int = 60,
                 anthropic_version: str = "2023-06-01") -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.timeout = timeout
        self.anthropic_version = anthropic_version
        self.name = f"vlm:{model}"

    def _key(self) -> str:
        return _sidecar_key(self.api_key_env, api_key_only=True)

    def transcribe(self, image_path: str) -> VisionResult:
        from .seal import read_file

        b64 = base64.b64encode(read_file(image_path)).decode()
        body = {
            "model": self.model,
            "max_tokens": 800,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": _VLM_PROMPT},
                    {"type": "image", "source": {
                        "type": "base64",
                        "media_type": _media_type(image_path),
                        "data": b64,
                    }},
                ],
            }],
        }
        try:
            _auth = self._key()
        except Exception as e:
            return VisionResult("", error=repr(e), model=self.name)
        req = urllib.request.Request(
            f"{self.base_url}/v1/messages",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     "x-api-key": _auth,
                     "anthropic-version": self.anthropic_version},
            method="POST")
        t0 = time.time()
        try:
            with http_pool.urlopen(req, timeout=self.timeout) as r:
                raw = json.load(r)
        except urllib.error.HTTPError as e:
            return VisionResult("", error=f"HTTP {e.code}: {e.read().decode('utf-8','replace')[:300]}",
                                latency_ms=(time.time()-t0)*1000, model=self.name)
        except Exception as e:
            return VisionResult("", error=repr(e), latency_ms=(time.time()-t0)*1000, model=self.name)
        try:
            blocks = raw.get("content", []) or []
            text = next((b.get("text", "") for b in blocks if b.get("type") == "text"), "")
        except (AttributeError, TypeError):
            return VisionResult("", error=f"bad VLM response: {str(raw)[:200]}",
                                latency_ms=(time.time()-t0)*1000, model=self.name)
        usage = raw.get("usage", {}) or {}
        report_usage(name=self.name, model=self.model, base_url=self.base_url,
                     usage=usage, started_at=t0, purpose="vision")
        return VisionResult(text=text, tokens_out=int(usage.get("output_tokens", 0) or 0),
                            latency_ms=(time.time()-t0)*1000, model=self.name)


class NullVisionSidecar:
    """Returned when no vision-capable provider key is configured. transcribe()
    yields a clear, actionable error so the UI can tell the user exactly how to
    enable image input -- instead of a cryptic missing-key failure."""

    name = "vlm:none"

    def transcribe(self, image_path: str) -> VisionResult:
        return VisionResult(
            "", model=self.name,
            error=("no vision-capable provider configured -- add an API key for "
                   "Anthropic, OpenAI, Google Gemini, xAI, or OpenRouter to enable "
                   "image input (or set HARNESS_VLM_REACH=openrouter)"))


# Sidecar POSTs /chat/completions or Anthropic /messages — never Responses /
# CLI / Bedrock shapes that cannot serve those endpoints.
_SIDECAR_API_MODES = frozenset({"chat_completions", "anthropic_messages"})

# Pilots on these modes can carry OpenAI-shaped multimodal user content when
# the model itself is vision-capable (catalog / provider.vision_model).
_NATIVE_IMAGE_API_MODES = frozenset({
    "chat_completions",
    "anthropic_messages",
    "codex_responses",
    "responses",
    "gemini_native",
})


def resolve_provider_for_spec(spec: str):
    """Best-effort provider for a driver/pilot spec (mirrors build_pilot routing)."""
    try:
        from .providers import available_providers, get_provider
    except Exception:
        return None
    raw = (spec or "").strip()
    if not raw:
        return None
    if ":" in raw:
        pname, _model = raw.split(":", 1)
        return get_provider(pname)
    candidates = [p for p in available_providers() if raw in p.pilot_models]
    for preferred in ("openai-codex",):
        for p in candidates:
            if p.name == preferred:
                return p
    if candidates:
        return candidates[0]
    return get_provider("openrouter")


# Relays that pick the wire protocol per model, not per provider.
_PER_MODEL_API_MODES = ("opencode_go", "opencode_zen")


def wire_api_mode(provider, model_id: str) -> str:
    """The protocol *model_id* travels on through *provider*.

    OpenCode Go and Zen send each model on its own endpoint (chat
    completions, Anthropic messages or Responses). Their provider-level
    ``api_mode`` names the relay, not a protocol, and once made every Go and
    Zen pilot text-only, image-capable models too.
    """
    api_mode = getattr(provider, "api_mode", "") or ""
    if api_mode not in _PER_MODEL_API_MODES:
        return api_mode
    try:
        if api_mode == "opencode_go":
            from .opencode_go import api_mode_for_model, wire_model_id
            mode = api_mode_for_model(wire_model_id(model_id))
        else:
            from .opencode_zen import api_mode_for_model
            mode = api_mode_for_model(model_id)
    except Exception:
        return api_mode
    return "responses" if mode == "openai_responses" else mode


def _model_id_candidates(provider, model_id: str) -> list:
    """Ids under which a metadata source can list *model_id*."""
    ids = [model_id]
    if ":" in model_id:
        ids.append(model_id.split(":", 1)[1])
    ids.append(ids[-1].rsplit("/", 1)[-1])
    if getattr(provider, "name", "") == "opencode-go":
        try:
            from .opencode_go import flash_model_keys
            ids.extend(sorted(flash_model_keys(ids[-1])))
        except Exception:
            pass
    seen: list = []
    for candidate in ids:
        if candidate and candidate not in seen:
            seen.append(candidate)
    return seen


def _record_image_input(record) -> Optional[bool]:
    """Image input from a live provider catalog record, or None if it does not say."""
    if not isinstance(record, dict):
        return None
    arch = record.get("architecture")
    modalities = record.get("modalities")
    for inputs in (
        arch.get("input_modalities") if isinstance(arch, dict) else None,
        modalities.get("input") if isinstance(modalities, dict) else modalities,
    ):
        if isinstance(inputs, list):
            return "image" in inputs
    return None


def model_image_input(provider, model_id: str, *, allow_network: bool = False) -> Optional[bool]:
    """Whether *model_id* on *provider* takes image input; None when no source knows.

    One rule for every provider. Sources, first that knows wins: the
    provider's own live catalog, models.dev, then the eval catalog.
    """
    from .model_fetch import model_metadata
    from .models_dev import image_input

    name = str(getattr(provider, "name", "") or "")
    candidates = _model_id_candidates(provider, (model_id or "").strip())
    if name:
        for candidate in candidates:
            try:
                known = _record_image_input(model_metadata(name, candidate))
            except Exception:
                known = None
            if known is not None:
                return known
        for candidate in candidates:
            known = image_input(name, candidate, allow_network=allow_network)
            if known is not None:
                return known
    return _catalog_has_vision(model_id)


def _catalog_has_vision(model_id: str) -> Optional[bool]:
    """True/False when the eval catalog lists the model; None if unknown."""
    if not model_id:
        return None
    try:
        from pmharness.registry import _entry
    except Exception:
        return None
    candidates = [model_id]
    if ":" in model_id:
        bare = model_id.split(":", 1)[1]
        candidates.append(bare)
        candidates.append(bare.split("/")[-1])
    if "/" in model_id:
        candidates.append(model_id.split("/")[-1])
    seen = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            return bool(_entry(candidate).get("vision", False))
        except Exception:
            continue
    return None


def _unwrap_pilot(pilot):
    """Follow cassette / wrapper inners to the real transport driver."""
    seen = set()
    cur = pilot
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        inner = getattr(cur, "_inner", None)
        if inner is None:
            return cur
        cur = inner
    return pilot


def session_supports_native_images(session) -> bool:
    """True when the session's active pilot should receive pixels, not a sidecar.

    Sidecar transcription is for text-only pilots only. Vision-capable models
    (e.g. gpt-5.6-luna on openai-codex) must never be fed a weaker VLM paraphrase.
    """
    if session is None:
        return False
    config = getattr(session, "config", None)
    pilot = getattr(session, "pilot", None)
    provider = resolve_provider_for_spec(getattr(config, "driver", "") or "")
    model = str(getattr(pilot, "model", "") or "")
    return pilot_supports_native_images(provider, model=model, pilot=pilot, allow_network=True)


def pilot_supports_native_images(
    provider=None,
    *,
    model: str = "",
    pilot=None,
    allow_network: bool = False,
) -> bool:
    """True when the active pilot can receive image pixels natively.

    Prefer catalog ``vision`` when the model is listed (so OpenRouter text-only
    pilots stay on the sidecar). Uncatalogued closed pilots trust
    ``provider.vision_model`` + a native-capable ``api_mode``. Stub drivers
    never take pixels.

    Callers that handle attachments MUST skip ``transcribe_images`` when this
    returns True — never substitute a weaker sidecar VLM for a vision pilot.
    """
    real = _unwrap_pilot(pilot)
    if real is not None:
        cls = type(real).__name__
        if cls in ("StubDriver",) or cls.startswith("Stub"):
            return False

    model_id = (model or getattr(real, "model", "") or "").strip()
    api_mode = wire_api_mode(provider, model_id)
    if api_mode and api_mode not in _NATIVE_IMAGE_API_MODES:
        return False

    known = model_image_input(provider, model_id, allow_network=allow_network)
    if known is not None:
        return known

    if real is not None:
        mod = getattr(type(real), "__module__", "") or ""
        cls = type(real).__name__
        if "codex_responses" in mod or cls == "CodexResponsesDriver":
            return True
        if "anthropic" in mod and "Anthropic" in cls:
            return True
        if "gemini" in mod and "Gemini" in cls:
            return True

    # Closed pilots that speak a native multimodal wire format. Do NOT treat
    # "fallback provider happens to declare a vision_model" as native — that
    # incorrectly skipped the sidecar for stub/fake pilots when
    # resolve_provider_for_spec fell through to openrouter.
    if api_mode in ("codex_responses", "responses"):
        return True
    if api_mode in ("anthropic_messages", "gemini_native") and model_id:
        return True
    return False


def native_multimodal_user_content(text: str, image_paths: list) -> list:
    """OpenAI-shaped multimodal user content (text + image_url data URLs)."""
    parts: list = []
    body = text if isinstance(text, str) else ("" if text is None else str(text))
    if body:
        parts.append({"type": "text", "text": body})
    for path in image_paths or []:
        if not path:
            continue
        parts.append({
            "type": "image_url",
            "image_url": {"url": _read_data_url(str(path))},
        })
    if not parts:
        parts.append({"type": "text", "text": ""})
    return parts


def merge_user_contents(prev, new):
    """Merge adjacent user history contents (string and/or multimodal lists)."""
    if isinstance(prev, str) and isinstance(new, str):
        return prev.rstrip() + "\n\n" + new

    def _as_parts(content) -> list:
        if isinstance(content, list):
            return list(content)
        return [{"type": "text", "text": content if isinstance(content, str) else str(content or "")}]

    return _as_parts(prev) + [{"type": "text", "text": "\n\n"}] + _as_parts(new)


def provider_vision_sidecars() -> list:
    """A sidecar for each configured provider with a vision model, in order.

    Reuses the keys the user already set, so image input works with zero
    extra setup. Skips ``api_mode`` values that cannot serve chat/completions
    or Anthropic messages (notably ``codex_responses``): those pilots use
    native multimodal instead of broken sidecar POSTs. OpenCode Go serves a
    vision model on chat/completions, so a Go-only setup has a sidecar too.
    """
    try:
        from .providers import available_providers
    except Exception:
        return []
    out: list = []
    for p in available_providers():
        key_env = p.key_env()
        if not key_env:
            continue
        api_mode = getattr(p, "api_mode", "") or "chat_completions"
        if api_mode == "opencode_go":
            from . import opencode_go
            out.append(OpenAICompatVisionSidecar(
                model=opencode_go.VISION_SIDECAR_MODEL,
                base_url=opencode_go.driver_base_url(p.base_url), api_key_env=key_env,
                extra_headers={"User-Agent": opencode_go.USER_AGENT}))
            continue
        model = getattr(p, "vision_model", "") or ""
        if not model or api_mode not in _SIDECAR_API_MODES:
            continue
        if api_mode == "anthropic_messages":
            out.append(AnthropicVisionSidecar(model=model, base_url=p.base_url,
                                              api_key_env=key_env))
        else:
            out.append(OpenAICompatVisionSidecar(model=model, base_url=p.base_url,
                                                 api_key_env=key_env))
    return out


def provider_vision_sidecar():
    """The first of ``provider_vision_sidecars``, or None."""
    sidecars = provider_vision_sidecars()
    return sidecars[0] if sidecars else None


def sidecar_candidates() -> list:
    """Every configured sidecar, in ``default_sidecar`` precedence order."""
    reach = os.environ.get("HARNESS_VLM_REACH", "").lower()
    if reach == "openrouter":
        model = os.environ.get("HARNESS_VLM_MODEL", "qwen/qwen3-vl-30b-a3b-instruct")
        return [OpenRouterVisionSidecar(model=model)]
    if reach == "gemini":
        return [GeminiVisionSidecar()]
    out: list = []
    if os.environ.get("GEMINI_API_KEY", "").strip():
        out.append(GeminiVisionSidecar())
    if os.environ.get("OPENROUTER_API_KEY", "").strip():
        model = os.environ.get("HARNESS_VLM_MODEL", "qwen/qwen3-vl-30b-a3b-instruct")
        out.append(OpenRouterVisionSidecar(model=model))
    seen = {(sc.base_url, sc.model) for sc in out}
    for sc in provider_vision_sidecars():
        if (sc.base_url, sc.model) not in seen:
            seen.add((sc.base_url, sc.model))
            out.append(sc)
    return out


def transcribe_images(paths: list, sidecar=None) -> list:
    """Transcribe a list of image paths into VisionResults.

    With no ``sidecar``, each image tries every configured sidecar in order
    (see ``sidecar_candidates``) until one answers. One failing VLM (a retired
    model, a spent key) does not fail the whole image turn.
    """
    if sidecar is not None:
        return [sidecar.transcribe(p) for p in paths]
    candidates = sidecar_candidates() or [NullVisionSidecar()]
    results = []
    for path in paths:
        failures = []
        result = None
        for sc in candidates:
            result = sc.transcribe(path)
            if not result.error:
                break
            failures.append(f"{sc.name}: {result.error}")
        if result.error and len(failures) > 1:
            result = VisionResult("", error="; ".join(failures)[:800], model=result.model)
        results.append(result)
    return results


def default_sidecar():
    """Resolve the vision sidecar. Precedence:

    1. Explicit reach override (HARNESS_VLM_REACH=openrouter -> open VLM,
       model overridable via HARNESS_VLM_MODEL).
    2. Dedicated VLM keys, preferring Gemini then OpenRouter (back-compat: these
       are the historical vision keys and keep their default models).
    3. Any other configured provider that declares a vision model (Anthropic,
       OpenAI, xAI, ...) or serves one (OpenCode Go), reusing the key the user
       already has.
    4. Nothing configured -> NullVisionSidecar (actionable error, not a crash).
    """
    candidates = sidecar_candidates()
    return candidates[0] if candidates else NullVisionSidecar()
