"""A chat link to a file outside the workspace previews read-only in-app.

Real report (2026-09-29): a pilot wrote a Markdown report to ~/Downloads and
linked it. The click raised a red header error (resolve answered 403) and fell
through to the OS opener. Any file the editor can render (text, or a binary
with a viewer) under home or the temp dir now resolves, reads and serves raw
as a read-only preview. Hidden dirs, ~/Library and unrenderable binaries stay
confined; no write path accepts them.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import harness.api.files as files
from harness.api.files import FileServices, get_file_raw, get_file_read, get_file_resolve


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    repo = home / "Projects" / "app"
    repo.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(files, "_external_preview_roots",
                        lambda: [str(home.resolve()), str(scratch.resolve())])
    svc = FileServices(cfg=SimpleNamespace(repo=str(repo)), sessions=None,
                       upload_dir=str(repo / "uploads"))
    return home, scratch, svc


def _write(path, data: bytes = b"# Report\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_home_document_resolves_and_reads_read_only(env):
    home, _, svc = env
    doc = _write(home / "Downloads" / "report.md")
    for hint in (str(doc), "~/Downloads/report.md"):
        status, body = get_file_resolve(hint, svc)
        assert status == 200
        assert body == {"ok": True, "path": str(doc.resolve()), "exact": True, "read_only": True}
    status, body = get_file_read(str(doc), svc)
    assert status == 200
    assert body["content"] == "# Report\n"
    assert body["read_only"] is True


@pytest.mark.parametrize("rel,data", [
    ("Downloads/tool.py", b"print(1)\n"),
    ("Downloads/page.html", b"<h1>x</h1>"),
    ("Documents/Makefile", b"all:\n"),
    ("Downloads/scan.pdf", b"%PDF-1.4\x00\x01"),
    ("Pictures/shot.png", b"\x89PNG\x00\x00"),
])
def test_everything_the_editor_renders_previews_in_harness(env, rel, data):
    home, _, svc = env
    path = _write(home / rel, data)
    status, body = get_file_resolve(str(path), svc)
    assert status == 200 and body["read_only"] is True
    assert get_file_read(str(path), svc)[0] == 200
    status, raw, _ = get_file_raw(str(path), svc)
    assert status == 200 and raw == data


def test_temp_dir_files_preview(env):
    _, scratch, svc = env
    path = _write(scratch / "out" / "log.txt", b"done\n")
    assert get_file_resolve(str(path), svc)[1]["read_only"] is True


def test_workspace_files_are_not_marked_read_only(env):
    home, _, svc = env
    _write(home / "Projects" / "app" / "notes.md")
    status, body = get_file_read("notes.md", svc)
    assert status == 200
    assert "read_only" not in body


@pytest.mark.parametrize("rel,data", [
    (".ssh/config", b"Host x\n"),
    (".aws/credentials", b"[default]\n"),
    ("Downloads/.secret/notes.md", b"x"),
    ("Library/Preferences/x.json", b"{}"),
    ("Downloads/deck.docx", b"PK\x03\x04\x00\x00"),
])
def test_sensitive_paths_and_unrenderable_binaries_stay_confined(env, rel, data):
    home, _, svc = env
    path = _write(home / rel, data)
    assert get_file_resolve(str(path), svc)[0] == 403
    assert get_file_read(str(path), svc)[0] == 403
    assert get_file_raw(str(path), svc)[0] == 403


def test_outside_allowed_roots_and_relative_hints_stay_confined(env, tmp_path):
    _, _, svc = env
    outside = _write(tmp_path / "elsewhere" / "report.md")
    assert get_file_read(str(outside), svc)[0] == 403
    assert get_file_resolve("../../../elsewhere/report.md", svc)[0] in (400, 403, 404)
