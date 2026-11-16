"""Fit inline images under provider size limits.

Anthropic refuses a base64 image over 10 MB, and a refused image in history
makes every later turn fail. A view_image of a 10.6 MB PNG screenshot did
that. Images over ``MAX_INLINE_IMAGE_BYTES`` are re-encoded (JPEG) and
downscaled until they fit. macOS ``sips`` does the work; Pillow is used when
it is installed. Without either, the caller gets ``None`` and must not send
the original bytes.
"""
from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Optional

# Raw bytes. Base64 adds a third (6.7 MB), under Anthropic's 10 MB and the
# 20 MB of OpenAI and Gemini inline images.
MAX_INLINE_IMAGE_BYTES = 5 * 1024 * 1024
# (long edge or None for full size, JPEG quality), tried in order.
_FIT_STEPS = ((None, 85), (2048, 85), (1568, 80), (1024, 75))
# Rough provider cost of one image, for estimates only (Anthropic: about
# width * height / 750 tokens, near 1600 at the 1568 px long edge).
IMAGE_TOKEN_ESTIMATE = 1600

_DATA_URL_RE = re.compile(r"data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=]+")


def _fit_with_sips(data: bytes) -> Optional[bytes]:
    if sys.platform != "darwin" or not shutil.which("sips"):
        return None
    with tempfile.TemporaryDirectory(prefix="pm-image-fit-") as tmp:
        src = os.path.join(tmp, "in")
        with open(src, "wb") as fh:
            fh.write(data)
        for step, (edge, quality) in enumerate(_FIT_STEPS):
            out = os.path.join(tmp, f"out{step}.jpg")
            cmd = ["sips", "-s", "format", "jpeg", "-s", "formatOptions", str(quality)]
            if edge:
                cmd += ["-Z", str(edge)]
            cmd += [src, "--out", out]
            try:
                subprocess.run(cmd, capture_output=True, timeout=30, check=True)
                with open(out, "rb") as fh:
                    fitted = fh.read()
            except (OSError, subprocess.SubprocessError):
                return None
            if 0 < len(fitted) <= MAX_INLINE_IMAGE_BYTES:
                return fitted
    return None


def _fit_with_pillow(data: bytes) -> Optional[bytes]:
    try:
        import io

        from PIL import Image  # type: ignore[import-not-found]
    except Exception:
        return None
    try:
        img = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception:
        return None
    for edge, quality in _FIT_STEPS:
        candidate = img.copy()
        if edge:
            candidate.thumbnail((edge, edge))
        buf = io.BytesIO()
        candidate.save(buf, format="JPEG", quality=quality)
        fitted = buf.getvalue()
        if len(fitted) <= MAX_INLINE_IMAGE_BYTES:
            return fitted
    return None


def fit_image_bytes(data: bytes, media_type: str) -> Optional[tuple[bytes, str]]:
    """Return ``(bytes, media_type)`` at most ``MAX_INLINE_IMAGE_BYTES``.

    Small images pass through unchanged. ``None`` means the image does not
    fit and no resize tool is available.
    """
    if len(data) <= MAX_INLINE_IMAGE_BYTES:
        return data, media_type
    fitted = _fit_with_sips(data) or _fit_with_pillow(data)
    if fitted is None:
        return None
    return fitted, "image/jpeg"


def image_too_large_text(size_bytes: int, label: str = "") -> str:
    where = f" {label}" if label else ""
    return (
        f"[image{where} not attached: {size_bytes / 1_048_576:.1f} MB is over the "
        f"{MAX_INLINE_IMAGE_BYTES // 1_048_576} MB inline limit and no resize tool "
        "is available. Crop or downscale it, then view it again.]"
    )


def fit_history_images(messages: Any) -> int:
    """Fit every oversized ``image_url`` data URL in ``messages`` in place.

    A part that cannot be fitted becomes a text part. Returns the number of
    parts changed. Small images cost one length check each.
    """
    changed = 0
    if not isinstance(messages, list):
        return 0
    limit = (MAX_INLINE_IMAGE_BYTES * 4) // 3
    for msg in messages:
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for idx, part in enumerate(content):
            if not isinstance(part, dict) or part.get("type") != "image_url":
                continue
            image = part.get("image_url")
            url = image.get("url") if isinstance(image, dict) else image
            if not isinstance(url, str) or len(url) <= limit:
                continue
            if not url.startswith("data:image/") or ";base64," not in url:
                continue
            head, b64 = url.split(",", 1)
            if len(b64) * 3 // 4 - (len(b64) - len(b64.rstrip("="))) <= MAX_INLINE_IMAGE_BYTES:
                continue
            media_type = head[len("data:"):].split(";", 1)[0]
            try:
                raw = base64.b64decode(b64)
            except Exception:
                content[idx] = {"type": "text", "text": image_too_large_text(len(b64) * 3 // 4)}
                changed += 1
                continue
            fitted = fit_image_bytes(raw, media_type)
            if fitted is None:
                content[idx] = {"type": "text", "text": image_too_large_text(len(raw))}
            else:
                data, kind = fitted
                new_url = f"data:{kind};base64,{base64.b64encode(data).decode()}"
                if isinstance(image, dict):
                    content[idx] = {**part, "image_url": {**image, "url": new_url}}
                else:
                    content[idx] = {**part, "image_url": new_url}
            changed += 1
    return changed


def estimate_text_tokens(text: str) -> int:
    """chars // 4, with each inline base64 image counted as one image."""
    if not text:
        return 0
    images = 0

    def _drop(_match: "re.Match[str]") -> str:
        nonlocal images
        images += 1
        return ""

    rest = _DATA_URL_RE.sub(_drop, text) if "data:image/" in text else text
    return len(rest) // 4 + images * IMAGE_TOKEN_ESTIMATE
