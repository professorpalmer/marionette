"""Voice dictation through a local whisper.cpp model.

The composer records 16 kHz mono PCM and posts it here as base64. This module
writes a WAV file, runs ``whisper-cli`` on it, and returns the text. Audio and
text stay on this machine. The macOS on-device engine runs in the Electron
shell, not here.
"""
from __future__ import annotations

import base64
import binascii
import os
import shutil
import subprocess
import tempfile
import threading
import time
import wave
from typing import Any, Optional

SAMPLE_RATE = 16_000
MAX_SECONDS = 180
_TIMEOUT_SECONDS = 60
# Best accuracy per second first. Measured on an M5 Max, 2026-11-15:
# small.en 0.0 WER at 0.38 s per 6 s clip; turbo q5 0.015 at 0.45 s.
_MODEL_PREFERENCE = (
    "ggml-small.en.bin",
    "ggml-large-v3-turbo-q5_0.bin",
    "ggml-large-v3-turbo.bin",
    "ggml-base.en.bin",
    "ggml-small.bin",
    "ggml-base.bin",
)
_lock = threading.Lock()


def models_dir() -> str:
    return os.path.join(os.path.expanduser("~/.pmharness"), "models", "whisper")


def find_binary() -> Optional[str]:
    explicit = (os.environ.get("HARNESS_WHISPER_CLI") or "").strip()
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    return shutil.which("whisper-cli")


def find_model() -> Optional[str]:
    explicit = (os.environ.get("HARNESS_WHISPER_MODEL") or "").strip()
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    root = models_dir()
    try:
        present = set(os.listdir(root))
    except OSError:
        return None
    for name in _MODEL_PREFERENCE:
        if name in present:
            return os.path.join(root, name)
    others = sorted(n for n in present if n.startswith("ggml-") and n.endswith(".bin"))
    return os.path.join(root, others[0]) if others else None


def _english_only(model: str) -> bool:
    return os.path.basename(model).split(".bin")[0].endswith(".en")


def get_voice_status() -> tuple[int, Any]:
    binary = find_binary()
    model = find_model()
    if not binary:
        reason = "Install whisper.cpp (brew install whisper-cpp) to use the local Whisper engine."
    elif not model:
        reason = f"Put a ggml Whisper model (for example ggml-small.en.bin) in {models_dir()}."
    else:
        reason = None
    return 200, {
        "whisper": {
            "available": reason is None,
            "reason": reason,
            "model": os.path.basename(model) if model else None,
            "english_only": bool(model and _english_only(model)),
            "models_dir": models_dir(),
        },
    }


def _language(value: Any) -> str:
    text = str(value or "en").strip().lower()
    base = text.split("-")[0].split("_")[0]
    return base if base.isalpha() and 2 <= len(base) <= 3 else "en"


def post_voice_transcribe(body: dict) -> tuple[int, Any]:
    if not isinstance(body, dict):
        return 400, {"code": "invalid_request", "error": "Body must be a JSON object."}
    try:
        pcm = base64.b64decode(str(body.get("pcm16") or ""), validate=True)
    except (binascii.Error, ValueError):
        return 400, {"code": "invalid_audio", "error": "pcm16 must be base64 PCM."}
    if not pcm or len(pcm) % 2:
        return 400, {"code": "invalid_audio", "error": "pcm16 must hold 16-bit samples."}
    if len(pcm) > SAMPLE_RATE * 2 * MAX_SECONDS:
        return 413, {"code": "audio_too_long", "error": f"Dictation is limited to {MAX_SECONDS} seconds."}
    binary, model = find_binary(), find_model()
    if not binary or not model:
        _status, payload = get_voice_status()
        return 503, {"code": "whisper_unavailable", "error": payload["whisper"]["reason"]}
    language = _language(body.get("language"))
    if language != "en" and _english_only(model):
        return 400, {
            "code": "unsupported_language",
            "error": f"The model {os.path.basename(model)} is English only.",
        }
    with tempfile.TemporaryDirectory(prefix="marionette-voice-") as tmp:
        path = os.path.join(tmp, "dictation.wav")
        with wave.open(path, "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(SAMPLE_RATE)
            out.writeframes(pcm)
        started = time.monotonic()
        with _lock:
            try:
                done = subprocess.run(
                    [binary, "-m", model, "-f", path, "-nt", "-np", "-l", language],
                    capture_output=True, text=True, timeout=_TIMEOUT_SECONDS, check=False,
                )
            except subprocess.TimeoutExpired:
                return 504, {"code": "whisper_timeout", "error": "whisper.cpp did not finish in time."}
            except OSError as exc:
                return 503, {"code": "whisper_unavailable", "error": str(exc)}
    if done.returncode != 0:
        detail = (done.stderr or "").strip().splitlines()[-1:] or ["no output"]
        return 502, {"code": "whisper_failed", "error": f"whisper.cpp failed: {detail[0][:300]}"}
    text = " ".join(line.strip() for line in done.stdout.splitlines() if line.strip())
    return 200, {
        "text": text,
        "engine": "whisper",
        "model": os.path.basename(model),
        "seconds": round(len(pcm) / 2 / SAMPLE_RATE, 3),
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }
