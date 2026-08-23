"""Honest bounds / containment lock-ins for the v0.9.279 wave-3 pass."""
from __future__ import annotations

import os
import tempfile
from types import SimpleNamespace

from harness.api.files import (
    FileServices,
    check_upload_request,
    get_file_read,
    get_image,
    save_upload,
)


def test_check_upload_request_caps_content_length():
    ctype = "multipart/form-data; boundary=----bound"
    assert check_upload_request(ctype, 100) is None
    status, body = check_upload_request(ctype, 11 * 1024 * 1024)
    assert status == 413
    assert "too large" in body["error"]
    status, body = check_upload_request("text/plain", 100)
    assert status == 400


def test_get_file_read_caps_at_one_mib():
    with tempfile.TemporaryDirectory() as tmp:
        repo = os.path.realpath(tmp)
        path = os.path.join(repo, "big.txt")
        with open(path, "wb") as fh:
            fh.write(b"a" * (1024 * 1024 + 50))
        svc = FileServices(
            cfg=SimpleNamespace(repo=repo),
            sessions=SimpleNamespace(active=""),
            upload_dir=repo,
        )
        status, payload = get_file_read("big.txt", svc)
        assert status == 200
        assert payload["ok"] is True
        assert payload["truncated"] is True
        assert len(payload["content"].encode("utf-8")) <= 1024 * 1024


def test_get_image_rejects_path_outside_upload_dir():
    with tempfile.TemporaryDirectory() as tmp:
        upload = os.path.join(tmp, "uploads")
        os.makedirs(upload)
        outside = os.path.join(tmp, "secret.png")
        with open(outside, "wb") as fh:
            fh.write(b"\x89PNG")
        status, payload, _ctype = get_image(outside, upload)
        assert status == 403
        assert "outside" in payload["error"]


def test_save_upload_does_not_use_user_filename_as_path():
    with tempfile.TemporaryDirectory() as tmp:
        upload = os.path.realpath(tmp)
        body = (
            b"------bound\r\n"
            b"Content-Disposition: form-data; name=\"file\"; "
            b"filename=\"../../etc/passwd.png\"\r\n"
            b"Content-Type: image/png\r\n\r\n"
            b"fakepng\r\n"
            b"------bound--\r\n"
        )
        status, payload = save_upload(
            body, "multipart/form-data; boundary=----bound", upload
        )
        assert status == 200
        saved = payload["saved"]
        assert len(saved) == 1
        stored = os.path.realpath(saved[0]["path"])
        assert os.path.commonpath([upload, stored]) == upload
        assert saved[0]["name"] == "../../etc/passwd.png"
        assert os.path.basename(stored) != "passwd.png"
        assert ".." not in os.path.relpath(stored, upload)
