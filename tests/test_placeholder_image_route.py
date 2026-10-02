"""GET /api/image for retained input images while the pilot is still building.

Before: opening a session in another project registers a DeferredPilotPlaceholder
as the active pilot. Any ``input:`` image request in that window raised
AttributeError (the placeholder has no prompt-queue lock), the server dropped the
socket, and the desktop app read the reset as "backend changed" and demanded a
Reconnect.
"""
from __future__ import annotations

from pathlib import Path

from harness.api.files import get_image
from harness.deferred_attach import DeferredPilotPlaceholder
from harness.input_receipts import InputReceiptStore

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")


def _placeholder(tmp_path: Path, sid: str) -> DeferredPilotPlaceholder:
    return DeferredPilotPlaceholder(session_id=sid, state_dir=str(tmp_path / "runner"),
                                    input_state_root=str(tmp_path / "sessions"))


def test_building_session_answers_unknown_image_without_crashing(tmp_path):
    status, body, _ = get_image("input:abc:def", str(tmp_path), session=_placeholder(tmp_path, "s1"))
    assert status == 403, body


def test_building_session_serves_its_own_retained_image(tmp_path):
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    shot = uploads / "shot.png"
    shot.write_bytes(PNG)
    store = InputReceiptStore(str(tmp_path / "sessions"), "s1")
    receipt = store.admit("look", images=[str(shot)], upload_root=str(uploads))
    ref = next(a["ref"] for a in receipt["attachments"] if a["kind"] == "image")
    status, body, ctype = get_image(ref, str(uploads), session=_placeholder(tmp_path, "s1"))
    assert (status, ctype) == (200, "image/png")
    assert body == PNG
