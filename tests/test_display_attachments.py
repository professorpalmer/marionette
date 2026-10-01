"""User rows in the display transcript keep their attachments across reloads.

Before: steer rows and sent messages were persisted as bare text, so a
reloaded session showed no thumbnails or file chips, and a reloaded steer
looked like an ordinary user bubble (or showed the sidecar transcription).
"""
from pathlib import Path

from harness.api.session_control import post_session_queue, post_session_steer
from harness.input_receipts import session_input_store, user_display_row
from tests.test_input_receipts_integration import services, session  # noqa: F401

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")


def _files(svc):
    image = Path(svc.upload_dir) / "shot.png"
    image.write_bytes(PNG)
    doc = Path(svc.upload_dir) / "notes.txt"
    doc.write_text("parser notes\n")
    return image, doc


def test_steer_row_keeps_steer_chrome_original_text_and_attachments(session, monkeypatch):  # noqa: F811
    svc = services(session)
    image, doc = _files(svc)
    monkeypatch.setattr("harness.vision.session_supports_native_images", lambda *_: False)
    monkeypatch.setattr("harness.vision.transcribe_images",
                        lambda paths: [type("R", (), {"text": "a login form", "error": None})()])
    code, reply = post_session_steer({"text": "look at this", "images": [str(image)],
                                      "documents": [{"path": str(doc), "name": "notes.txt"}]}, svc)
    assert code == 200, reply
    list(session._check_and_inject_steer())
    row = session._display_transcript[-1]
    assert row["role"] == "user" and row["steer"] is True
    assert row["text"] == "look at this"  # what was typed, not the sidecar transcription
    kinds = [(a["kind"], a["name"]) for a in row["attachments"]]
    assert kinds == [("image", "shot.png"), ("document", "notes.txt")]
    ref = row["attachments"][0]["ref"]
    assert ref.startswith("input:") and session_input_store(session).attachment(ref) == PNG


def test_sent_message_row_lists_its_attachments(session):  # noqa: F811
    svc = services(session)
    image, doc = _files(svc)
    code, result = post_session_queue({"text": "check these", "images": [str(image)],
                                       "documents": [{"path": str(doc), "name": "notes.txt"}]}, svc)
    assert code == 200, result
    input_id = session.input_receipts()[0]["id"]
    row = user_display_row(session, "check these", input_id)
    assert row["input_id"] == input_id and "steer" not in row
    assert [(a["kind"], a["name"]) for a in row["attachments"]] == [("image", "shot.png"), ("document", "notes.txt")]


def test_rows_without_a_receipt_stay_plain(session):  # noqa: F811
    assert user_display_row(session, "hello", None) == {"type": "message", "role": "user", "text": "hello"}
    assert user_display_row(session, "hello", "missing-id") == {
        "type": "message", "role": "user", "text": "hello", "input_id": "missing-id"}


def test_strip_images_removes_image_attachments_from_rows(session):  # noqa: F811
    session._display_transcript = [{"type": "message", "role": "user", "text": "x", "attachments": [
        {"kind": "image", "name": "a.png", "ref": "input:1"}, {"kind": "document", "name": "n.txt", "ref": "input:2"}]}]
    session._history = []
    assert session.strip_history_images() >= 1
    assert session._display_transcript[0]["attachments"] == [{"kind": "document", "name": "n.txt", "ref": "input:2"}]
