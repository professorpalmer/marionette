"""A chat link to a report outside the workspace previews read-only in-app.

Real report (2026-09-29): a pilot wrote a Markdown report to ~/Downloads and
linked it. The click raised a red header error (resolve answered 403) and fell
through to the OS opener. Documents under home now resolve and read as a
read-only preview; hidden dirs, ~/Library and other types stay out, and no
write path accepts them.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from harness.api.files import FileServices, get_file_read, get_file_resolve


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    repo = home / "Projects" / "app"
    repo.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    svc = FileServices(cfg=SimpleNamespace(repo=str(repo)), sessions=None,
                       upload_dir=str(repo / "uploads"))
    return home, svc


def _write(path, text="# Report\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_home_document_resolves_and_reads_read_only(env):
    home, svc = env
    doc = _write(home / "Downloads" / "report.md")
    for hint in (str(doc), "~/Downloads/report.md"):
        status, body = get_file_resolve(hint, svc)
        assert status == 200
        assert body == {"ok": True, "path": str(doc.resolve()), "exact": True, "read_only": True}
    status, body = get_file_read(str(doc), svc)
    assert status == 200
    assert body["content"] == "# Report\n"
    assert body["read_only"] is True


def test_workspace_files_are_not_marked_read_only(env):
    home, svc = env
    _write(home / "Projects" / "app" / "notes.md")
    status, body = get_file_read("notes.md", svc)
    assert status == 200
    assert "read_only" not in body


@pytest.mark.parametrize("rel", [
    ".ssh/config.txt",
    "Downloads/.secret/notes.md",
    "Library/Preferences/x.json",
    "Downloads/report.html",
    "Downloads/tool.py",
])
def test_sensitive_or_non_document_paths_stay_confined(env, rel):
    home, svc = env
    path = _write(home / rel)
    status, _ = get_file_resolve(str(path), svc)
    assert status == 403
    status, _ = get_file_read(str(path), svc)
    assert status == 403


def test_outside_home_and_relative_hints_stay_confined(env, tmp_path):
    _, svc = env
    outside = _write(tmp_path / "elsewhere" / "report.md")
    assert get_file_read(str(outside), svc)[0] == 403
    assert get_file_resolve("../../../elsewhere/report.md", env[1])[0] in (400, 403, 404)
