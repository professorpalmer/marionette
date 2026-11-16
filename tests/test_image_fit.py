"""Inline images stay under provider limits; estimates ignore base64 bytes."""
from __future__ import annotations

import base64
import os
import struct
import sys
import zlib
from types import SimpleNamespace

import pytest

from harness import image_fit
from harness.image_fit import (
    IMAGE_TOKEN_ESTIMATE,
    MAX_INLINE_IMAGE_BYTES,
    estimate_text_tokens,
    fit_history_images,
    fit_image_bytes,
)


def _noise_png(width: int, height: int) -> bytes:
    """A PNG that does not compress (random RGB), so its size is about w*h*3."""
    raw = b"".join(b"\x00" + os.urandom(width * 3) for _ in range(height))

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + kind + body
                + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(raw, 1)) + chunk(b"IEND", b""))


def _row(url: str) -> dict:
    return {"role": "user", "content": [
        {"type": "text", "text": "[native vision: contents of big.png]"},
        {"type": "image_url", "image_url": {"url": url}},
    ]}


def test_small_image_passes_unchanged():
    data = b"\x89PNG small"
    assert fit_image_bytes(data, "image/png") == (data, "image/png")


def test_oversized_history_image_is_fitted(monkeypatch):
    big = b"x" * (MAX_INLINE_IMAGE_BYTES + 1)
    monkeypatch.setattr(image_fit, "_fit_with_sips", lambda data: b"jpeg-bytes")
    history = [_row("data:image/png;base64," + base64.b64encode(big).decode())]
    assert fit_history_images(history) == 1
    url = history[0]["content"][1]["image_url"]["url"]
    assert url == "data:image/jpeg;base64," + base64.b64encode(b"jpeg-bytes").decode()
    # A second pass changes nothing.
    assert fit_history_images(history) == 0


def test_unfittable_history_image_becomes_text(monkeypatch):
    big = b"x" * (MAX_INLINE_IMAGE_BYTES + 1)
    monkeypatch.setattr(image_fit, "_fit_with_sips", lambda data: None)
    monkeypatch.setattr(image_fit, "_fit_with_pillow", lambda data: None)
    history = [_row("data:image/png;base64," + base64.b64encode(big).decode())]
    assert fit_history_images(history) == 1
    part = history[0]["content"][1]
    assert part["type"] == "text"
    assert "not attached" in part["text"]


def test_read_data_url_refuses_an_unfittable_image(tmp_path, monkeypatch):
    from harness.vision import _read_data_url

    path = tmp_path / "big.png"
    path.write_bytes(b"x" * (MAX_INLINE_IMAGE_BYTES + 1))
    monkeypatch.setattr(image_fit, "_fit_with_sips", lambda data: None)
    monkeypatch.setattr(image_fit, "_fit_with_pillow", lambda data: None)
    with pytest.raises(ValueError, match="not attached"):
        _read_data_url(str(path))


@pytest.mark.skipif(sys.platform != "darwin", reason="sips is macOS only")
def test_sips_fits_a_real_oversized_png(tmp_path):
    from harness.vision import _read_data_url

    path = tmp_path / "tour_sheet.png"
    path.write_bytes(_noise_png(1800, 1100))
    assert path.stat().st_size > MAX_INLINE_IMAGE_BYTES
    url = _read_data_url(str(path))
    head, b64 = url.split(",", 1)
    assert head == "data:image/jpeg;base64"
    assert len(base64.b64decode(b64)) <= MAX_INLINE_IMAGE_BYTES


def test_estimate_counts_an_image_not_its_base64():
    url = "data:image/png;base64," + "A" * 4_000_000
    assert estimate_text_tokens("hello " + url) == len("hello ") // 4 + IMAGE_TOKEN_ESTIMATE


def test_refused_image_request_does_not_inflate_the_prompt_clock(monkeypatch):
    from harness.send_loop_phases import meter_pilot_step

    session = SimpleNamespace(
        _tokens_used=0, _tokens_out=0, _turn_output_tokens=0, _tokens_in=0,
        _last_prompt_tokens=0, _last_prompt_heuristic=0, _tokens_cached=0,
        _tokens_cache_write=0, _tokens_cache_write_5m=0, _tokens_cache_write_1h=0,
        _last_turn_cache_read_tokens=0, _last_prompt_cache_activity_at=0.0,
        _plan_billing=True, _price_source="", _provider_cost_usd=0.0,
        _provider_billed_tokens_in=0, _provider_billed_tokens_out=0,
        _provider_billed_tokens_cached=0, _provider_billed_tokens_cache_write=0,
        _provider_billed_tokens_cache_write_5m=0, _provider_billed_tokens_cache_write_1h=0,
        _history=[], config=SimpleNamespace(driver="anthropic:claude-opus-5-5"),
        _accumulate_session_meters=lambda **kw: None,
    )
    resp = SimpleNamespace(tokens_out=0, tokens_in=0, meta={}, error="HTTP 400")
    prompt = "ask " + "data:image/png;base64," + "A" * 14_000_000
    meter_pilot_step(session, resp, prompt=prompt)
    # Before: len(prompt) // 4, about 3.5M tokens for a 14 MB image.
    assert session._tokens_in == len("ask ") // 4 + IMAGE_TOKEN_ESTIMATE
    assert session._last_prompt_tokens == session._tokens_in
