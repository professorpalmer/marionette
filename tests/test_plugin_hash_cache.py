"""Polling /api/mcp and /api/plugins must not re-read every plugin file."""
from __future__ import annotations

import hashlib
import os
import pathlib
import time

import harness.agent_plugins as ap
from harness.agent_plugins import compute_plugin_content_sha256


def _plugin(tmp_path, age_s=0):
    root = tmp_path / "plug"
    (root / "skills").mkdir(parents=True)
    (root / "plugin.json").write_text('{"name": "p"}', encoding="utf-8")
    (root / "skills" / "a.md").write_text("alpha", encoding="utf-8")
    if age_s:
        old = time.time() - age_s
        for path in root.rglob("*"):
            os.utime(path, (old, old))
    return root


def test_unchanged_plugin_is_not_re_read(tmp_path, monkeypatch):
    root = _plugin(tmp_path, age_s=60)
    reads = []
    real = pathlib.Path.read_bytes
    monkeypatch.setattr(pathlib.Path, "read_bytes", lambda self: reads.append(self.name) or real(self))

    first = compute_plugin_content_sha256(root)
    assert len(reads) == 2
    assert compute_plugin_content_sha256(root) == first
    assert len(reads) == 2  # cache hit: stat only


def test_an_edit_of_the_same_size_is_re_hashed(tmp_path):
    root = _plugin(tmp_path)
    first = compute_plugin_content_sha256(root)
    (root / "skills" / "a.md").write_text("omega", encoding="utf-8")  # same length
    second = compute_plugin_content_sha256(root)
    assert second != first
    (root / "skills" / "b.md").write_text("new", encoding="utf-8")
    assert compute_plugin_content_sha256(root) not in (first, second)


def test_a_same_size_rewrite_that_keeps_the_mtime_is_still_re_hashed(tmp_path):
    """Windows: ctime is creation time, and a rewrite can land in the same
    timestamp tick. Files touched near the hash are never trusted from cache."""
    root = _plugin(tmp_path)
    target = root / "skills" / "a.md"
    first = compute_plugin_content_sha256(root)
    st = target.stat()
    target.write_text("omega", encoding="utf-8")
    os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert compute_plugin_content_sha256(root) != first


def _reference(root):
    h = hashlib.sha256()
    paths = sorted((p for p in root.rglob('*') if p.is_file() and not p.is_symlink()),
                   key=lambda p: p.relative_to(root).as_posix())
    for p in paths:
        rel = p.relative_to(root).as_posix()
        if ap.STAMP_FILENAME in rel.split('/') or '.git' in rel.split('/'):
            continue
        h.update(rel.encode() + b'\0' + p.read_bytes() + b'\0')
    return h.hexdigest()


def test_digest_matches_reference_and_never_enters_git(tmp_path, monkeypatch):
    for rel in ['plugin.json', 'a/b/c.txt', 'a-b/x', 'a.b', 'sub/.git', ap.STAMP_FILENAME]:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(rel * 3)
    for i in range(50):
        p = tmp_path / '.git' / 'objects' / f'{i:02d}' / 'o'
        p.parent.mkdir(parents=True)
        p.write_text('x')
    os.symlink(tmp_path / 'plugin.json', tmp_path / 'link.json')
    os.symlink(tmp_path / 'a', tmp_path / 'linkdir')
    expected = _reference(tmp_path)
    scanned = []
    real = os.scandir
    monkeypatch.setattr(ap.os, 'scandir', lambda d: scanned.append(d) or real(d))
    assert ap.compute_plugin_content_sha256(tmp_path) == expected
    mine = [str(d) for d in scanned if str(d).startswith(str(tmp_path))]
    assert mine and not any('.git' in d for d in mine)
